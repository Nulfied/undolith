import json
import shutil
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from undolith import Held, Policy, Risk, Undolith
from undolith.adapters import HTTP, Email, Shell, SQLiteDB, dry_run_argv, file_transport
from undolith.adapters.shell import shell_risk
from undolith.adapters.sqlite import sql_risk


def last_action(guard):
    return guard.actions()[-1]["action"]


# ---------------------------------------------------------------- sqlite
@pytest.fixture
def db(tmp_path):
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);"
                       "INSERT INTO users(name) VALUES ('ada'), ('grace'), ('linus');")
    conn.commit()
    conn.close()
    return path


def names(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM users ORDER BY id")]
    finally:
        conn.close()


def test_sql_risk():
    assert sql_risk("SELECT 1") is Risk.READ
    assert sql_risk("insert into t values (1)") is Risk.WRITE
    assert sql_risk("  DELETE FROM t") is Risk.DESTRUCTIVE
    assert sql_risk("DROP TABLE t") is Risk.DESTRUCTIVE


def test_sqlite_simulate_is_side_effect_free_and_undo_restores(tmp_path, db):
    adapter = SQLiteDB(db)
    preview = adapter.simulate("DELETE FROM users WHERE name != 'ada'")
    assert preview.predicted == {"changes": 2} and names(db) == ["ada", "grace", "linus"]
    assert preview.details["row_count_deltas"] == {"users": -2}

    guard = Undolith(tmp_path / "u").register(adapter)
    guard.call("db.execute", sql="DELETE FROM users WHERE name != ?", params=["ada"])
    assert names(db) == ["ada"]
    assert guard.call("db.query", sql="SELECT count(*) AS n FROM users") == [{"n": 1}]
    guard.undo(guard.actions()[-2]["action"])
    assert names(db) == ["ada", "grace", "linus"]


def test_sqlite_undo_conflict(tmp_path, db):
    guard = Undolith(tmp_path / "u").register(SQLiteDB(db))
    guard.call("db.execute", sql="INSERT INTO users(name) VALUES ('alan')")
    aid = last_action(guard)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO users(name) VALUES ('someone else')")
    conn.commit()
    conn.close()
    from undolith import UndoConflict

    with pytest.raises(UndoConflict):
        guard.undo(aid)


# ---------------------------------------------------------------- http
class _API(BaseHTTPRequestHandler):
    items = {}
    next_id = [1]

    def _json(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")

    def do_GET(self):
        key = self.path.rsplit("/", 1)[-1]
        if self.path == "/items":
            return self._json(200, list(self.items.values()))
        return self._json(200, self.items[key]) if key in self.items else self._json(404, {"error": "nope"})

    def do_POST(self):
        item = {**self._body(), "id": str(self.next_id[0])}
        self.next_id[0] += 1
        self.items[item["id"]] = item
        self._json(201, item)

    def do_PUT(self):
        key = self.path.rsplit("/", 1)[-1]
        self.items[key] = self._body()
        self._json(200, self.items[key])

    def do_DELETE(self):
        self.items.pop(self.path.rsplit("/", 1)[-1], None)
        self._json(204)

    def log_message(self, *a):
        pass


@pytest.fixture
def api():
    _API.items = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_post_undone_by_registered_inverse(tmp_path, api):
    http = HTTP(api).inverse("POST", "/items",
                             lambda req, resp, snap: {"method": "DELETE", "url": f"/items/{resp['body']['id']}"})
    guard = Undolith(tmp_path / "u").register(http)
    resp = guard.call("http.request", method="POST", url="/items", json={"name": "widget"})
    assert resp["status"] == 201
    assert len(guard.call("http.get", url="/items")["body"]) == 1
    guard.undo(guard.actions()[-2]["action"])
    assert guard.call("http.get", url="/items")["body"] == []


def test_http_put_generic_inverse_from_snapshot(tmp_path, api):
    _API.items["7"] = {"id": "7", "name": "old"}
    guard = Undolith(tmp_path / "u").register(HTTP(api))
    guard.call("http.request", method="PUT", url="/items/7", json={"id": "7", "name": "new"})
    guard.undo(last_action(guard))
    assert _API.items["7"]["name"] == "old"


def test_http_risk_and_preview_do_not_send(tmp_path, api):
    http = HTTP(api)
    assert http.risk({"method": "GET", "url": "/x"}) is Risk.READ
    assert http.risk({"method": "POST", "url": "/v1/payments"}) is Risk.IRREVERSIBLE
    assert http.risk({"method": "DELETE", "url": "/items/1"}) is Risk.DESTRUCTIVE
    preview = http.simulate("POST", "/items", json={"a": 1})
    assert "no inverse" in preview.summary and _API.items == {}
    guard = Undolith(tmp_path / "u").register(http)
    held = guard.call("http.request", method="POST", url="/v1/payments", json={"amount": 100})
    assert isinstance(held, Held) and _API.items == {}


# ---------------------------------------------------------------- email
def test_email_is_held_until_released(tmp_path):
    outbox = tmp_path / "outbox"
    guard = Undolith(tmp_path / "u").register(Email(file_transport(outbox), sender="bot@example.com"))
    held = guard.call("email.send", to=["a@example.com", "b@example.com"], subject="Q3", body="numbers")
    assert isinstance(held, Held) and not outbox.exists()
    assert "2 recipient(s)" in held.preview.summary and "Subject: Q3" in held.preview.diff
    result = guard.release(held.action_id, by="manager")
    assert result["sent"] and len(list(outbox.glob("*.eml"))) == 1


# ---------------------------------------------------------------- shell / git
def test_dry_run_table():
    assert dry_run_argv(["git", "clean", "-fd"]) == ["git", "clean", "-n", "-d"] or \
        dry_run_argv(["git", "clean", "-fd"])[:3] == ["git", "clean", "-n"]
    assert dry_run_argv(["git", "push", "origin", "main"]) == ["git", "push", "--dry-run", "origin", "main"]
    assert dry_run_argv(["terraform", "apply", "-auto-approve"]) == ["terraform", "plan"]
    assert dry_run_argv(["kubectl", "apply", "-f", "x.yaml"])[-1] == "--dry-run=client"
    assert dry_run_argv(["aws", "s3", "rm", "s3://b/k"])[-1] == "--dryrun"
    assert dry_run_argv(["make", "deploy"]) == ["make", "-n", "deploy"]
    assert dry_run_argv(["python", "x.py"]) is None
    assert shell_risk(["git", "status"]) is Risk.READ
    assert shell_risk(["git", "push"]) is Risk.IRREVERSIBLE
    assert shell_risk(["rm", "-rf", "x"]) is Risk.DESTRUCTIVE


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_commit_is_simulated_and_undoable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout  # noqa
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    (repo / "a.txt").write_text("1")
    git("add", ".")
    git("commit", "-qm", "first")
    first = git("rev-parse", "HEAD").strip()
    (repo / "a.txt").write_text("2")
    git("add", ".")

    guard = Undolith(tmp_path / "u").register(Shell(cwd=str(repo)))
    guard.call("sh.run", cmd=["git", "commit", "-m", "second"])
    aid = last_action(guard)
    sim = guard.ledger.for_action(aid)[1]["data"]
    assert sim["details"]["dry_run_argv"][:3] == ["git", "commit", "--dry-run"]
    assert git("rev-parse", "HEAD").strip() != first
    guard.undo(aid)
    assert git("rev-parse", "HEAD").strip() == first
    assert "a.txt" in git("diff", "--cached", "--name-only")  # soft reset keeps the work staged


def test_shell_without_dry_run_is_flagged(tmp_path):
    guard = Undolith(tmp_path / "u", policy=Policy(require_simulator=True)).register(Shell())
    preview = Shell().simulate(["python", "-c", "print(1)"])
    assert not preview.simulated and "no native dry-run" in preview.summary
    held = guard.call("sh.run", cmd=["python", "-c", "print(1)"])
    assert isinstance(held, Held)  # no inverse and unknown effects -> needs a human
