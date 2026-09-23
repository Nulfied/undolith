"""A scripted "agent" goes wrong, and Undolith catches, explains and undoes it.

    python examples/rogue_agent.py

No API keys, no network: the agent's tool calls are hard-coded so the demo is
reproducible. Swap ``PLAN`` for your model's tool calls and nothing else changes.
"""

import json
import shutil
import tempfile
from pathlib import Path

from undolith import Approval, Policy, Rule, Undolith, verify_proof
from undolith.adapters import Email, FileSystem, file_transport
from undolith.integrations.function_calls import dispatch

root = Path(tempfile.mkdtemp(prefix="undolith-demo-"))
project = root / "project"
project.mkdir()
for name, text in {"README.md": "# Payroll service\n", "config.yaml": "workers: 4\nregion: eu-west-1\n",
                   "data/employees.csv": "id,name\n1,Ada\n2,Grace\n", "data/salaries.csv": "id,eur\n1,90\n2,95\n",
                   "data/bonus.csv": "id,eur\n1,5\n"}.items():
    (project / name).parent.mkdir(parents=True, exist_ok=True)
    (project / name).write_text(text)


def human(action, preview):
    """Stand-in for a person reviewing the diff: rejects more deletions, parks emails for later."""
    if action.qualname == "email.send":
        print(f"    ? approval asked for {action.qualname}: {preview.summary} -> DEFER")
        return None  # not now: hold it in the outbox
    print(f"    ? approval asked for {action.qualname} {action.args.get('path', '')} -> REJECT")
    return Approval(False, by="ops-oncall", note="stop deleting data")


policy = Policy(max_destructive_per_session=2)
policy.add(Rule(verdict="deny", match="fs.*", args={"path": "*.env"}, reason="secrets are off-limits"))
guard = Undolith(root / ".undolith", policy=policy, approver=human)
guard.register(FileSystem(project), Email(file_transport(root / "outbox"), sender="agent@payroll.local"))

PLAN = [  # what a confused model decided to do
    ("fs.write", {"path": "config.yaml", "content": "workers: 64\nregion: us-east-1\n"}),
    ("fs.write", {"path": ".env", "content": "DB_PASSWORD=hunter2\n"}),
    ("fs.delete", {"path": "data/bonus.csv"}),
    ("fs.delete", {"path": "data/salaries.csv"}),
    ("fs.delete", {"path": "data/employees.csv"}),
    ("email.send", {"to": ["all-staff@payroll.local"], "subject": "Salaries reset", "body": "Oops."}),
]

print(f"\nworkspace: {project}\n")
with guard.session(agent="payroll-bot") as session:
    for name, args in PLAN:
        out = dispatch(guard, name, args)
        print(f"  {name:<11} {args.get('path') or args.get('subject'):<22} -> {str(out)[:95]}")

    print("\nfiles now:", sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()))
    print("outbox:   ", [h["summary"] for h in guard.held()])

    print("\n>>> an operator hits the kill switch for this session")
    report = session.kill("bot misbehaving")
    print(f"    rolled back {len(report.undone)} action(s); skipped {len(report.skipped)}")

print("files now:", sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()))
print("config.yaml:", (project / "config.yaml").read_text().splitlines())
for held in guard.held():
    guard.discard(held["action"], by="ops-oncall", reason="never send this")
print("outbox discarded; emails actually sent:", len(list((root / "outbox").glob("*.eml"))) if (root / "outbox").exists() else 0)

chain = guard.verify_chain()
print(f"\nledger: {chain.length} signed entries, chain intact={chain.ok}")
delete_id = next(a["action"] for a in guard.actions() if a["qualname"] == "fs.delete")
proof = json.loads(json.dumps(guard.verify(delete_id, segment=True)))
check = verify_proof(proof, public_key=guard.signer.public_key.hex())
print(f"proof for {delete_id}: valid={check.valid} status={check.status} "
      f"happened={check.happened} authorized={check.authorized}")

shutil.rmtree(root, ignore_errors=True)
