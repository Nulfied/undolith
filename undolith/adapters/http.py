"""HTTP adapter with a per-endpoint inverse registry (stdlib ``urllib``).

Simulation is a mock: it describes the request without sending it. Undo comes
from inverses you register, e.g. "a POST to /orders is undone by DELETE
/orders/{id}". PUT/PATCH/DELETE can snapshot the resource with a GET first so a
generic inverse can put it back.

    api = HTTP("https://api.example.com")
    api.inverse("POST", "/orders", lambda req, resp, snap: {"method": "DELETE", "url": f"/orders/{resp['body']['id']}"})
"""

from __future__ import annotations

import fnmatch
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..classify import IRREVERSIBLE_VERBS
from ..model import Preview, Risk
from ..ops import Adapter, Operation

Inverse = Callable[[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]], Optional[Dict[str, Any]]]
_WORDS = re.compile(r"[a-z]+")


class HTTP(Adapter):
    tool = "http"

    def __init__(self, base_url: str = "", *, headers: Optional[Dict[str, str]] = None, timeout: float = 30,
                 snapshot_before: Tuple[str, ...] = ("PUT", "PATCH", "DELETE")):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.snapshot_before = tuple(m.upper() for m in snapshot_before)
        self._inverses: List[Tuple[str, str, Inverse]] = []

    def inverse(self, method: str, url_glob: str, fn: Inverse) -> "HTTP":
        """Register how to undo ``method`` on URLs matching ``url_glob`` (path or full URL)."""
        self._inverses.append((method.upper(), url_glob, fn))
        return self

    def _find_inverse(self, method: str, url: str) -> Optional[Inverse]:
        path = urllib.parse.urlsplit(self._url(url)).path
        for m, glob, fn in self._inverses:
            if m == method.upper() and (fnmatch.fnmatchcase(path, glob) or fnmatch.fnmatchcase(self._url(url), glob)):
                return fn
        return None

    def _url(self, url: str) -> str:
        return url if re.match(r"^[a-z]+://", url) else f"{self.base_url}/{url.lstrip('/')}"

    def _send(self, method: str, url: str, json_body: Any = None, data: Optional[str] = None,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        hdrs = {**self.headers, **(headers or {})}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = data.encode() if isinstance(data, str) else data
        req = urllib.request.Request(self._url(url), data=body, method=method.upper(), headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw, rh = resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            status, raw, rh = err.code, err.read(), dict(err.headers or {})
        text = raw.decode("utf-8", "replace")
        try:
            parsed: Any = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = text
        return {"status": status, "headers": rh, "body": parsed}

    # -- operations --------------------------------------------------------------
    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self._send("GET", url, headers=headers)

    def request(self, method: str, url: str, json: Any = None, data: Optional[str] = None,
                headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        resp = self._send(method, url, json, data, headers)
        if resp["status"] >= 400:
            raise RuntimeError(f"{method.upper()} {url} -> HTTP {resp['status']}: {str(resp['body'])[:200]}")
        return resp

    def risk(self, args: Dict[str, Any]) -> Risk:
        method = str(args.get("method", "GET")).upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return Risk.READ
        words = set(_WORDS.findall(urllib.parse.urlsplit(self._url(args.get("url", ""))).path.lower()))
        if words & IRREVERSIBLE_VERBS or words & {"payments", "charges", "messages", "emails", "transfers", "orders"}:
            if method == "POST":
                return Risk.IRREVERSIBLE
        return Risk.DESTRUCTIVE if method == "DELETE" else Risk.WRITE

    def simulate(self, method: str, url: str, json: Any = None, data: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> Preview:
        body = json if json is not None else data
        inv = "inverse registered" if self._find_inverse(method, url) else "no inverse registered"
        return Preview(
            summary=f"{method.upper()} {self._url(url)} ({inv})",
            diff=f"{method.upper()} {self._url(url)}\n" + (f"\n{_dumps(body)}\n" if body is not None else ""),
            predicted={"ok": True},
            details={"mock": True, "method": method.upper(), "url": self._url(url)},
        )

    def snapshot(self, method: str, url: str, **_: Any) -> Optional[Dict[str, Any]]:
        if method.upper() not in self.snapshot_before:
            return None
        before = self._send("GET", url)
        return {"status": before["status"], "body": before["body"]} if before["status"] < 400 else None

    def undo(self, args: Dict[str, Any], snap: Optional[Dict[str, Any]], result: Any) -> None:
        fn = self._find_inverse(args["method"], args["url"])
        if fn is not None:
            req = fn(args, result or {}, snap)
        elif snap is not None and args["method"].upper() in ("PUT", "PATCH"):
            req = {"method": "PUT", "url": args["url"], "json": snap["body"]}
        else:
            req = None
        if not req:
            raise RuntimeError(f"no inverse for {args['method']} {args['url']}")
        resp = self._send(req["method"], req["url"], req.get("json"), req.get("data"), req.get("headers"))
        if resp["status"] >= 400:
            raise RuntimeError(f"inverse {req['method']} {req['url']} -> HTTP {resp['status']}")

    def reversible(self, args: Dict[str, Any]) -> bool:
        method = str(args.get("method", "")).upper()
        return self._find_inverse(method, args.get("url", "")) is not None or method in ("PUT", "PATCH")

    def operations(self) -> List[Operation]:
        return [
            self.op("get", self.get, risk=Risk.READ),
            self.op("request", self.request, risk=self.risk, simulate=self.simulate, snapshot=self.snapshot,
                    undo=self.undo, reversible=self.reversible,
                    observe=lambda args, r: {"ok": 200 <= (r or {}).get("status", 0) < 400}),
        ]


def _dumps(body: Any) -> str:
    return body if isinstance(body, str) else json.dumps(body, indent=2, sort_keys=True, default=str)
