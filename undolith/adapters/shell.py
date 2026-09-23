"""Shell adapter: use each tool's *native* dry-run where one exists.

    git clean  -> git clean -n          terraform apply -> terraform plan
    git push   -> git push --dry-run    kubectl apply   -> kubectl apply --dry-run=client
    rsync      -> rsync --dry-run       aws s3 cp       -> aws s3 cp ... --dryrun
    make       -> make -n               pip install     -> pip install --dry-run

Commands run without a shell (argv lists only), so there is no injection surface
from string interpolation. ``git commit`` is undoable (soft reset to the previous HEAD).
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Union

from ..model import Preview, Risk
from ..ops import Adapter, Operation

Argv = Union[str, Sequence[str]]

_READ = {("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"), ("git", "branch"),
         ("git", "rev-parse"), ("ls", None), ("cat", None), ("echo", None), ("pwd", None),
         ("terraform", "plan"), ("kubectl", "get"), ("kubectl", "describe")}
_DESTRUCTIVE = {("git", "clean"), ("git", "reset"), ("git", "rm"), ("git", "checkout"), ("git", "restore"),
                ("rm", None), ("rmdir", None), ("kubectl", "delete"), ("terraform", "destroy"), ("aws", "s3")}
_IRREVERSIBLE = {("git", "push"), ("terraform", "apply"), ("kubectl", "apply"), ("npm", "publish"),
                 ("twine", "upload"), ("docker", "push")}


def argv_of(cmd: Argv) -> List[str]:
    return shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]


def _key(argv: List[str]):
    prog = argv[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe") if argv else ""
    sub = next((a for a in argv[1:] if not a.startswith("-")), None)
    return prog, sub


def shell_risk(argv: List[str]) -> Risk:
    prog, sub = _key(argv)
    for table, risk in ((_READ, Risk.READ), (_IRREVERSIBLE, Risk.IRREVERSIBLE), (_DESTRUCTIVE, Risk.DESTRUCTIVE)):
        if (prog, sub) in table or (prog, None) in table:
            return risk
    return Risk.WRITE


def dry_run_argv(argv: List[str]) -> Optional[List[str]]:
    """The native dry-run equivalent of a command, or None if the tool has none."""
    prog, sub = _key(argv)
    if not argv:
        return None
    i = argv.index(sub) + 1 if sub in argv else 1
    if prog == "git" and sub in ("clean",):
        return argv[:i] + ["-n"] + [a for a in argv[i:] if a not in ("-f", "--force")]
    if prog == "git" and sub in ("push", "add", "rm", "commit", "fetch", "mv"):
        return argv[:i] + ["--dry-run"] + argv[i:]
    if prog == "terraform" and sub == "apply":
        return [argv[0], "plan"] + [a for a in argv[i:] if a not in ("-auto-approve", "--auto-approve")]
    if prog == "kubectl" and sub in ("apply", "delete", "create", "replace", "patch"):
        return argv + ["--dry-run=client"]
    if prog == "aws" and sub == "s3":
        return argv + ["--dryrun"]
    if prog == "aws" and sub == "ec2":
        return argv + ["--dry-run"]
    if prog == "rsync":
        return argv[:1] + ["--dry-run"] + argv[1:]
    if prog == "make":
        return argv[:1] + ["-n"] + argv[1:]
    if prog in ("pip", "pip3") and sub == "install":
        return argv[:i] + ["--dry-run"] + argv[i:]
    return None


class Shell(Adapter):
    tool = "sh"

    def __init__(self, cwd: Optional[str] = None, *, timeout: float = 300, env: Optional[Dict[str, str]] = None):
        self.cwd, self.timeout, self.env = cwd, timeout, env

    def _run(self, argv: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
        p = subprocess.run(argv, cwd=cwd or self.cwd, capture_output=True, text=True, timeout=self.timeout,
                           env=self.env)
        return {"argv": argv, "returncode": p.returncode, "stdout": p.stdout[-20000:], "stderr": p.stderr[-20000:]}

    def run(self, cmd: Argv, cwd: Optional[str] = None, check: bool = True) -> Dict[str, Any]:
        out = self._run(argv_of(cmd), cwd)
        if check and out["returncode"] != 0:
            raise RuntimeError(f"{shlex.join(out['argv'])} exited {out['returncode']}: {out['stderr'][-500:]}")
        return out

    def simulate(self, cmd: Argv, cwd: Optional[str] = None, check: bool = True) -> Preview:
        argv = argv_of(cmd)
        dry = dry_run_argv(argv)
        if dry is None:
            return Preview(summary=f"$ {shlex.join(argv)} (no native dry-run; effects unknown)", simulated=False)
        out = self._run(dry, cwd)
        return Preview(summary=f"dry run: $ {shlex.join(dry)} -> exit {out['returncode']}",
                       diff=(out["stdout"] + out["stderr"]).strip() + "\n",
                       predicted={"returncode": out["returncode"]} if check else {},
                       details={"dry_run_argv": dry})

    def _git_head(self, cwd: Optional[str]) -> Optional[str]:
        out = self._run(["git", "rev-parse", "HEAD"], cwd)
        return out["stdout"].strip() if out["returncode"] == 0 else None

    def snapshot(self, cmd: Argv, cwd: Optional[str] = None, check: bool = True) -> Optional[Dict[str, Any]]:
        if _key(argv_of(cmd)) == ("git", "commit"):
            return {"git_head": self._git_head(cwd), "cwd": cwd or self.cwd}
        return None

    def undo(self, args: Dict[str, Any], snap: Optional[Dict[str, Any]], result: Any) -> None:
        if not snap or not snap.get("git_head"):
            raise RuntimeError("nothing to undo (first commit or no snapshot)")
        self.run(["git", "reset", "--soft", snap["git_head"]], cwd=snap.get("cwd"))

    def reversible(self, args: Dict[str, Any]) -> bool:
        return _key(argv_of(args.get("cmd", []))) == ("git", "commit")

    def observe(self, args: Dict[str, Any], result: Any) -> Dict[str, Any]:
        obs = {"returncode": (result or {}).get("returncode")}
        if self.reversible(args):
            obs["git_head"] = self._git_head(args.get("cwd"))
        return obs

    def operations(self) -> List[Operation]:
        return [self.op("run", self.run, risk=lambda a: shell_risk(argv_of(a.get("cmd", []))),
                        simulate=self.simulate, snapshot=self.snapshot, undo=self.undo,
                        reversible=self.reversible, observe=self.observe)]
