import http.client
import json
import threading

import pytest

from undolith import Undolith
from undolith.adapters import FileSystem
from undolith import Operation, Risk
from undolith.ui import make_server


@pytest.fixture
def console(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    guard = Undolith(tmp_path / "u").register(FileSystem(ws))
    guard.register(Operation("mail", "send", run=lambda to: {"sent": to}, risk=Risk.IRREVERSIBLE))
    server, token = make_server(guard, port=0, token="t0ken")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield guard, ws, server.server_address[1], token
    server.shutdown()
    server.server_close()


def req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"Content-Type": "application/json", **(headers or {})})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data), dict(r.getheaders())
    except ValueError:
        return r.status, data.decode(), dict(r.getheaders())


def test_page_embeds_token_and_security_headers(console):
    guard, ws, port, token = console
    status, page, headers = req(port, "GET", "/")
    assert status == 200 and 'content="t0ken"' in page
    assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    assert ".innerHTML" not in page  # ledger data is only ever inserted as text


def test_state_detail_and_undo_via_api(console):
    guard, ws, port, token = console
    with guard.session(task="write a note") as s:
        s.call("fs.write", path="n.txt", content="<script>alert(1)</script>")
    status, st, _ = req(port, "GET", "/api/state")
    assert status == 200 and st["sessions"][0]["task"] == "write a note"
    aid = st["actions"][0]["action"]
    status, detail, _ = req(port, "GET", f"/api/action/{aid}")
    assert detail["undoable"] and "+<script>" in detail["diff"]
    assert req(port, "GET", "/api/action/nope")[0] == 404
    status, proof, headers = req(port, "GET", f"/api/proof/{aid}?segment=1")
    assert status == 200 and "attachment" in headers["Content-Disposition"] and proof["action"] == aid
    assert req(port, "POST", "/api/undo", {"id": aid}, {"X-Undolith-Token": token})[1]["ok"]
    assert not (ws / "n.txt").exists()
    assert req(port, "GET", "/api/verify")[1]["ok"]


def test_posts_need_token_same_origin_and_local_host(console):
    guard, ws, port, token = console
    held = guard.call("mail.send", to="all@example.com")
    body = {"id": held.action_id}
    assert req(port, "POST", "/api/release", body)[0] == 403  # no token
    assert req(port, "POST", "/api/release", body, {"X-Undolith-Token": "wrong"})[0] == 403
    assert req(port, "POST", "/api/release", body,
               {"X-Undolith-Token": token, "Origin": "https://evil.example"})[0] == 403
    assert req(port, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403  # DNS rebinding
    assert guard.status(held.action_id) == "held"
    status, out, _ = req(port, "POST", "/api/release", body,
                         {"X-Undolith-Token": token, "Origin": f"http://127.0.0.1:{port}"})
    assert status == 200 and out["ok"] and guard.status(held.action_id) == "committed"


def test_kill_resume_and_errors(console):
    guard, ws, port, token = console
    h = {"X-Undolith-Token": token}
    assert req(port, "POST", "/api/kill", {}, h)[1]["ok"] and guard.kill_file.exists()
    assert req(port, "GET", "/api/state")[1]["global_halted"]
    assert req(port, "POST", "/api/resume", {}, h)[1]["ok"] and not guard.kill_file.exists()
    status, out, _ = req(port, "POST", "/api/undo", {"id": "act_missing"}, h)
    assert status == 400 and not out["ok"]
    assert req(port, "POST", "/api/explode", {}, h)[0] == 404
