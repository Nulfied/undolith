"""``undolith ui``: a local web console for the ledger, outbox, undo and the kill switch.

Standard library only. Security model:

* It binds to 127.0.0.1 by default, and requests whose ``Host`` header is not
  localhost are refused, which blocks DNS-rebinding attacks.
* Every state-changing request needs a random token that is embedded in the page
  at launch, and a foreign ``Origin`` header is rejected. Another website open in
  the same browser therefore cannot release a held email or undo anything.
* Ledger content (which includes agent-controlled arguments) is only ever
  inserted into the page with ``textContent``, never as HTML.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .core import Undolith
from .lifecycle import first
from .model import UndolithError

ACTIONS = ("undo", "rollback", "release", "discard", "kill", "resume")
MAX_BODY = 1 << 20


def _sessions(guard: Undolith) -> list:
    out: Dict[str, Dict[str, Any]] = {}
    for e in guard.ledger.entries():
        sid = e.get("session")
        if not sid:
            continue
        s = out.setdefault(sid, {"session": sid, "agent": e.get("agent"), "first": e["ts"], "last": e["ts"],
                                 "actions": 0, "task": None, "answer": None, "halted": False, "_seen": set()})
        s["last"] = e["ts"]
        kind = e["kind"]
        if kind == "started":
            s["task"] = e["data"].get("task")
        elif kind == "finished":
            s["answer"] = e["data"].get("answer")
        elif kind in ("halted", "resumed"):
            s["halted"] = kind == "halted"
        if e.get("action") and e["action"] not in s["_seen"]:
            s["_seen"].add(e["action"])
            s["actions"] += 1
    rows = []
    for s in out.values():
        s.pop("_seen")
        rows.append(s)
    return sorted(rows, key=lambda r: r["last"], reverse=True)


def state(guard: Undolith, session: Optional[str] = None) -> Dict[str, Any]:
    pk = guard.signer.public_key
    return {
        "version": __version__, "home": str(guard.home.resolve()),
        "signer": {"algorithm": guard.signer.algorithm, "key_id": guard.signer.key_id,
                   "public_key": pk.hex() if pk else None},
        "global_halted": guard.kill_file.exists(),
        "sessions": _sessions(guard),
        "actions": list(reversed(guard.actions(session=session)))[:500],
        "held": guard.held(),
    }


def action_detail(guard: Undolith, action_id: str) -> Dict[str, Any]:
    entries = guard.ledger.for_action(action_id)
    if not entries:
        raise KeyError(action_id)
    sim = first(entries, "simulated")
    return {"action": action_id, "status": guard.status(action_id), "entries": entries,
            "diff": (sim or {}).get("data", {}).get("diff_excerpt", ""),
            "undoable": guard.status(action_id) in ("committed", "failed", "in_flight")}


def perform(guard: Undolith, op: str, body: Dict[str, Any]) -> str:
    target = body.get("id") or body.get("session")
    if op == "undo":
        return "undone" if guard.undo(target, by="ui", force=bool(body.get("force"))) else "already undone"
    if op == "rollback":
        r = guard.rollback(target, by="ui")
        return f"rolled back {len(r.undone)}; skipped {len(r.skipped)}; failed {len(r.failed)}"
    if op == "release":
        guard.release(target, by="ui")
        return "released and committed"
    if op == "discard":
        guard.discard(target, by="ui", reason=body.get("reason", ""))
        return "discarded"
    if op == "kill":
        r = guard.kill(body.get("session"), reason=body.get("reason") or "kill switch (ui)", by="ui")
        return "global kill switch engaged" if r is None else f"session halted; rolled back {len(r.undone)}"
    if op == "resume":
        guard.resume(body.get("session"), by="ui")
        return "resumed"
    raise ValueError(op)


def make_handler(guard: Undolith, token: str, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"undolith/{__version__}"

        def log_message(self, *a):  # quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1")

        def _send(self, code: int, body: Any, ctype: str = "application/json", extra: Optional[Dict] = None):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                with lock:
                    if url.path == "/":
                        return self._send(200, PAGE.replace("__TOKEN__", token), "text/html")
                    if url.path == "/api/state":
                        return self._send(200, state(guard, q.get("session") or None))
                    if url.path.startswith("/api/action/"):
                        return self._send(200, action_detail(guard, url.path.rsplit("/", 1)[-1]))
                    if url.path == "/api/verify":
                        r = guard.verify_chain()
                        return self._send(200, {"ok": r.ok, "length": r.length, "head": r.head,
                                                "problems": r.problems[:50]})
                    if url.path.startswith("/api/proof/"):
                        aid = url.path.rsplit("/", 1)[-1]
                        proof = guard.verify(aid, segment=q.get("segment") == "1")
                        return self._send(200, json.dumps(proof, indent=2, sort_keys=True), "application/json",
                                          {"Content-Disposition": f'attachment; filename="proof-{aid}.json"'})
            except KeyError:
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # pragma: no cover - surfaced to the page
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            # Always drain the request body before replying, even to refuse it: closing a socket
            # with unread data makes Windows reset the connection instead of delivering our 403.
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                return self._send(413, {"error": "request body too large"})
            raw = self.rfile.read(n) if n else b""
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
                return self._send(403, {"error": "cross-origin request refused"})
            if not secrets.compare_digest(self.headers.get("X-Undolith-Token", ""), token):
                return self._send(403, {"error": "missing or bad token"})
            op = urlsplit(self.path).path.rsplit("/", 1)[-1]
            if op not in ACTIONS:
                return self._send(404, {"error": "unknown action"})
            try:
                body = json.loads(raw or b"{}")
                with lock:
                    msg = perform(guard, op, body)
                return self._send(200, {"ok": True, "message": msg})
            except (UndolithError, KeyError, ValueError) as exc:
                return self._send(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:
                return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return Handler


def make_server(guard: Undolith, host: str = "127.0.0.1", port: int = 8765,
                token: Optional[str] = None) -> Tuple[HTTPServer, str]:
    token = token or secrets.token_urlsafe(24)
    server = HTTPServer((host, port), make_handler(guard, token, threading.Lock()))
    return server, token


def serve(guard: Undolith, host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> None:
    server, _ = make_server(guard, host, port)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{server.server_address[1]}/"
    print(f"undolith ui: {url}  (ledger {guard.home.resolve()}; Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="undolith-token" content="__TOKEN__">
<title>Undolith console</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--accent:#3b5bdb;
--ok:#2b8a3e;--warn:#e67700;--bad:#c92a2a;--chip:#f0efea;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--muted:#9b9a93;--line:#2e2d2a;
--accent:#91a7ff;--ok:#69db7c;--warn:#ffa94d;--bad:#ff8787;--chip:#262522}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2;flex-wrap:wrap}
h1{font-size:16px;margin:0 8px 0 0;letter-spacing:.2px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:0 0 10px}
.pill{padding:3px 10px;border-radius:999px;background:var(--chip);font-size:12px;white-space:nowrap}
.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad);font-weight:600}.grow{flex:1}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:5px 11px;cursor:pointer}
button:hover{border-color:var(--accent)}button.danger{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,var(--line))}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
main{display:grid;grid-template-columns:300px 1fr;gap:16px;padding:16px 20px;max-width:1500px;margin:0 auto}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:16px;min-width:0}
.session{padding:9px 10px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.session:hover{background:var(--chip)}.session.sel{border-color:var(--accent);background:var(--chip)}
.session .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.small{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse}th{text-align:left;font-weight:500;color:var(--muted);font-size:12px;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}tr.row{cursor:pointer}tr.row:hover td{background:var(--chip)}
tr.sel td{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.mono{font-family:var(--mono);font-size:12.5px}.st{font-weight:600;font-size:12px}
.st.committed{color:var(--ok)}.st.undone,.st.discarded{color:var(--muted)}.st.denied,.st.rejected,.st.failed{color:var(--bad)}.st.held,.st.in_flight{color:var(--warn)}
.risk{font-size:11px;padding:1px 7px;border-radius:999px;background:var(--chip)}.risk.destructive,.risk.irreversible{color:var(--bad)}
pre{background:var(--chip);border-radius:8px;padding:10px;overflow:auto;max-height:340px;margin:8px 0;font:12px/1.4 var(--mono);white-space:pre}
.tl{border-left:2px solid var(--line);margin:8px 0 8px 6px;padding-left:14px}.tl div{margin:0 0 8px}.tl b{font-size:12px}
.held{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}.held:last-child{border:0}
.held .grow{min-width:0}.btns{display:flex;gap:6px;flex-wrap:wrap}
#toast{position:fixed;bottom:18px;right:18px;background:var(--ink);color:var(--bg);padding:9px 14px;border-radius:8px;opacity:0;transition:.2s;max-width:420px}
#toast.show{opacity:1}.empty{color:var(--muted);padding:12px 0}
</style>
</head>
<body>
<header>
  <h1>Undolith</h1>
  <span id="chain" class="pill">chain: not checked</span>
  <span id="signer" class="pill mono"></span>
  <span class="grow"></span>
  <button id="verify">Verify chain</button>
  <button id="refresh">Refresh</button>
  <button id="kill" class="danger">Kill switch</button>
</header>
<main>
  <aside>
    <div class="card"><h2>Sessions</h2><div id="sessions"></div></div>
  </aside>
  <section>
    <div class="card" id="outboxCard"><h2>Outbox: held for approval</h2><div id="outbox"></div></div>
    <div class="card">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <h2 style="margin:0" id="actionsTitle">All actions</h2><span class="grow"></span>
        <span id="sessionBtns" class="btns"></span>
      </div>
      <div style="overflow:auto"><table><thead><tr><th>When</th><th>Action</th><th>Risk</th><th>Status</th><th>Id</th></tr></thead>
      <tbody id="actions"></tbody></table></div>
    </div>
    <div class="card" id="detailCard" hidden><h2>Action detail</h2><div id="detail"></div></div>
  </section>
</main>
<div id="toast"></div>
<script>
"use strict";
const TOKEN = document.querySelector('meta[name="undolith-token"]').content;
let selSession = null, selAction = null, last = null;
function el(tag, props, ...kids){const n=document.createElement(tag);for(const[k,v]of Object.entries(props||{})){
  if(k==="class")n.className=v;else if(k.startsWith("on"))n.addEventListener(k.slice(2),v);else if(v!==undefined&&v!==null)n.setAttribute(k,v);}
  for(const k of kids.flat()){if(k===null||k===undefined||k===false)continue;n.append(k instanceof Node?k:document.createTextNode(String(k)));}return n;}
const $=id=>document.getElementById(id);
function toast(msg,bad){const t=$("toast");t.textContent=msg;t.style.background=bad?"var(--bad)":"";t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),3500);}
async function get(p){const r=await fetch(p);if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json();}
async function post(op,body,confirmMsg){if(confirmMsg&&!confirm(confirmMsg))return;
  const r=await fetch("/api/"+op,{method:"POST",headers:{"Content-Type":"application/json","X-Undolith-Token":TOKEN},body:JSON.stringify(body||{})});
  const j=await r.json();toast(j.ok?j.message:j.error,!j.ok);await load();if(selAction)await showAction(selAction);}
const when=ts=>ts?ts.replace("T"," ").slice(0,19):"";
const short=(s,n)=>{s=String(s??"");return s.length>n?s.slice(0,n-1)+"…":s;};
function renderSessions(ss){const box=$("sessions");box.replaceChildren();
  box.append(el("div",{class:"session"+(selSession===null?" sel":""),onclick:()=>{selSession=null;selAction=null;load();}},el("div",{class:"t"},"All sessions")));
  if(!ss.length)box.append(el("div",{class:"empty"},"No sessions yet."));
  for(const s of ss){box.append(el("div",{class:"session"+(s.session===selSession?" sel":""),onclick:()=>{selSession=s.session;selAction=null;load();}},
    el("div",{class:"t"},s.task?short(s.task,60):s.session),
    el("div",{class:"small"},`${s.agent||"agent"} · ${s.actions} action(s) · ${when(s.last)}`),
    s.halted?el("span",{class:"pill bad"},"halted"):null));}}
function renderOutbox(held){const box=$("outbox");box.replaceChildren();$("outboxCard").hidden=!held.length;
  for(const h of held){box.append(el("div",{class:"held"},
    el("div",{class:"grow"},el("div",{},el("b",{},h.qualname)," ",el("span",{class:"small"},h.agent||"")),
      el("div",{class:"small"},short(h.summary||h.reason,200)),el("div",{class:"small mono"},h.action)),
    el("div",{class:"btns"},el("button",{onclick:()=>showAction(h.action)},"Review"),
      el("button",{class:"primary",onclick:()=>post("release",{id:h.action},"Release and COMMIT this action now?\n\n"+h.qualname+"\n"+(h.summary||""))},"Release"),
      el("button",{class:"danger",onclick:()=>post("discard",{id:h.action},"Discard this held action?")},"Discard"))));}}
function renderActions(rows){const tb=$("actions");tb.replaceChildren();
  $("actionsTitle").textContent=selSession?"Session actions":"All actions";
  const sb=$("sessionBtns");sb.replaceChildren();
  if(selSession){const s=(last.sessions||[]).find(x=>x.session===selSession)||{};
    if(s.task)sb.append(el("span",{class:"small"},"task: "+short(s.task,80)));
    sb.append(el("button",{onclick:()=>post("rollback",{session:selSession},"Undo every reversible action in this session, newest first?")},"Roll back session"));
    sb.append(s.halted?el("button",{onclick:()=>post("resume",{session:selSession})},"Resume session")
      :el("button",{class:"danger",onclick:()=>post("kill",{session:selSession},"Halt this session and roll it back?")},"Kill session"));}
  if(!rows.length){tb.append(el("tr",{},el("td",{colspan:"5",class:"empty"},"No actions.")));return;}
  for(const r of rows){tb.append(el("tr",{class:"row"+(r.action===selAction?" sel":""),onclick:()=>showAction(r.action)},
    el("td",{class:"small"},when(r.ts)),el("td",{class:"mono"},r.qualname),el("td",{},el("span",{class:"risk "+r.risk},r.risk)),
    el("td",{},el("span",{class:"st "+r.status},r.status),r.deviated?el("span",{class:"pill bad",style:"margin-left:6px"},"deviated"):null),
    el("td",{class:"small mono"},r.action)));}}
async function showAction(id){selAction=id;let d;try{d=await get("/api/action/"+encodeURIComponent(id));}catch(e){toast(e.message,true);return;}
  const box=$("detail");box.replaceChildren();$("detailCard").hidden=false;
  const head=d.entries[0];const proposed=d.entries.find(e=>e.kind==="proposed"||e.kind==="read");
  box.append(el("div",{style:"display:flex;gap:8px;align-items:center;flex-wrap:wrap"},
    el("b",{class:"mono"},head.tool+"."+head.op),el("span",{class:"st "+d.status},d.status),el("span",{class:"grow"}),
    d.undoable?el("button",{class:"danger",onclick:()=>post("undo",{id},"Undo this action?")},"Undo"):null,
    d.status==="held"?el("button",{class:"primary",onclick:()=>post("release",{id},"Release and COMMIT this action now?")},"Release"):null,
    el("a",{href:"/api/proof/"+encodeURIComponent(id)+"?segment=1"},el("button",{},"Download proof"))));
  box.append(el("div",{class:"small mono"},id+" · session "+head.session+" · agent "+(head.agent||"")));
  if(proposed&&proposed.data&&proposed.data.args){box.append(el("h2",{style:"margin-top:14px"},"Arguments (redacted)"),el("pre",{},JSON.stringify(proposed.data.args,null,2)));}
  if(d.diff){box.append(el("h2",{style:"margin-top:14px"},"Simulated diff"),el("pre",{},d.diff));}
  const tl=el("div",{class:"tl"});
  for(const e of d.entries){const x=e.data||{};const note=x.summary||x.reason||x.error||x.note||x.result_preview||(x.by?("by "+x.by):"")||"";
    tl.append(el("div",{},el("b",{},"#"+e.seq+" "+e.kind)," ",el("span",{class:"small"},when(e.ts)),note?el("div",{class:"small"},short(note,300)):null));}
  box.append(el("h2",{style:"margin-top:14px"},"Ledger entries (signed, hash-chained)"),tl);
  renderActions(last?last.actions:[]);}
async function load(){try{last=await get("/api/state"+(selSession?"?session="+encodeURIComponent(selSession):""));}catch(e){toast(e.message,true);return;}
  $("signer").textContent=last.signer.key_id;$("signer").title="public key: "+(last.signer.public_key||"(hmac)");
  const k=$("kill");k.textContent=last.global_halted?"Lift kill switch":"Kill switch";k.className=last.global_halted?"primary":"danger";
  document.title=(last.global_halted?"[HALTED] ":"")+"Undolith console";
  renderSessions(last.sessions);renderOutbox(last.held);renderActions(last.actions);}
$("refresh").onclick=load;
$("verify").onclick=async()=>{const c=$("chain");c.textContent="verifying…";try{const r=await get("/api/verify");
  c.textContent=r.ok?`chain intact · ${r.length} entries`:`CHAIN BROKEN · ${r.problems.length} problem(s)`;c.className="pill "+(r.ok?"ok":"bad");
  if(!r.ok)toast(r.problems.slice(0,3).map(p=>"seq "+p.seq+": "+p.problem).join("\n"),true);}catch(e){toast(e.message,true);}};
$("kill").onclick=()=>last&&last.global_halted?post("resume",{}):post("kill",{},"Engage the GLOBAL kill switch? Every agent using this ledger stops immediately.");
load();$("verify").click();setInterval(()=>{if(!document.hidden)load();},5000);
</script>
</body>
</html>
"""
