"""Email adapter: you cannot unsend, so Undolith holds and flags instead.

``email.send`` is IRREVERSIBLE, so the default policy parks it in the outbox
(``undolith held``) with a rendered preview until someone releases it.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

from .._canon import new_id
from ..model import Preview, Risk
from ..ops import Adapter, Operation

Transport = Callable[[EmailMessage], object]


def smtp_transport(host: str, port: int = 587, *, username: Optional[str] = None,
                   password_env: Optional[str] = None, starttls: bool = True) -> Transport:
    """SMTP transport. The password is read from an environment variable, never passed as an argument."""
    import os

    def send(msg: EmailMessage):
        with smtplib.SMTP(host, port, timeout=30) as s:
            if starttls:
                s.starttls()
            if username:
                s.login(username, os.environ[password_env] if password_env else "")
            s.send_message(msg)
        return {"sent": True, "message_id": msg["Message-ID"]}

    return send


def file_transport(outbox: Union[str, Path]) -> Transport:
    """Write .eml files to a folder instead of sending. Good for demos and tests."""
    outbox = Path(outbox)

    def send(msg: EmailMessage):
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{new_id('mail')}.eml"
        path.write_bytes(bytes(msg))
        return {"sent": True, "file": str(path), "message_id": msg["Message-ID"]}

    return send


class Email(Adapter):
    tool = "email"

    def __init__(self, transport: Transport, *, sender: str = "agent@localhost"):
        self.transport = transport
        self.sender = sender

    def _message(self, to: Union[str, Sequence[str]], subject: str, body: str,
                 cc: Union[str, Sequence[str], None] = None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to if isinstance(to, str) else ", ".join(to)
        if cc:
            msg["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{new_id('msg')}@undolith>"
        msg.set_content(body)
        return msg

    def send(self, to, subject: str, body: str, cc=None):
        return self.transport(self._message(to, subject, body, cc))

    def simulate(self, to, subject: str, body: str, cc=None) -> Preview:
        msg = self._message(to, subject, body, cc)
        rendered = "".join(f"{k}: {v}\n" for k, v in msg.items() if k != "Message-ID") + "\n" + body
        n = len([a for a in str(msg["To"]).split(",") if a.strip()]) + (len(str(cc).split(",")) if cc else 0)
        return Preview(summary=f"send email to {msg['To']} ({n} recipient(s)): {subject!r}",
                       diff=rendered, predicted={"sent": True},
                       details={"irreversible": True, "recipients": n})

    def operations(self) -> List[Operation]:
        return [self.op("send", self.send, risk=Risk.IRREVERSIBLE, simulate=self.simulate,
                        observe=lambda args, r: {"sent": bool((r or {}).get("sent"))})]
