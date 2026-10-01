"""Opt-in CLI integration check. Makes bounded live model calls; never a unit test."""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from council.runner import agent_call
from council.workflow import load_toml, response_schema


def coordinator_session():
    """Three chat turns with ledger context, then three in a native session; prints only measured values."""
    from council.conversation import talk
    from council.store import Store
    source = (ROOT / "council/roster.toml").read_text()
    questions = ["Remember the code word ORCHID for this conversation. Reply in one sentence.",
                 "What is the marker in brief.txt? Reply in one sentence.",
                 "What code word did I ask you to remember? Reply in one sentence."]
    with tempfile.TemporaryDirectory(prefix="council-session-smoke-") as directory:
        root = Path(directory)
        (root / "project").mkdir()
        (root / "project/brief.txt").write_text("Marker: " + uuid.uuid4().hex + "\n")
        project = {"version": 1, "name": "smoke", "workspace": str(root / "project"), "git": False,
                   "include": ["*.txt"], "rules": "Live smoke check only.", "timeout_seconds": 120}
        store = Store(root / "state")
        try:
            for mode in ("ledger", "native"):
                roster = root / f"{mode}.toml"
                roster.write_text(re.sub(r'(?m)^chat_session = .*$', f'chat_session = "{mode}"', source))
                for question in questions:
                    result = talk(store, project, roster, question, thread=mode)
                    print(f"{mode} · {result['session_mode']} · {result['message'][:120]!r}")
                if "ORCHID" not in result["message"].upper():
                    raise SystemExit(f"FAILED: {mode} chat did not recall the code word")
                for row in store.chat_metrics("smoke", mode):
                    print(json.dumps(row))
        finally:
            store.close()
    print("PASS: coordinator-session · rows above are the measured wall time and provider-reported usage")


parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["coordinator", "implementer", "reviewer", "researcher", "coordinator-session"])
args = parser.parse_args()
if args.role == "coordinator-session":
    coordinator_session()
    raise SystemExit(0)
cfg = load_toml(ROOT / "council/roster.toml")
assignment = cfg["workflow_roles"][args.role]
assignment = {**assignment, "runtime": cfg["executors"][assignment["executor"]]}
phase = {"read_only": True, "routes": {"ready": "@done"}}
with tempfile.TemporaryDirectory(prefix="council-live-smoke-") as directory:
    marker = uuid.uuid4().hex
    (Path(directory) / "marker.txt").write_text(marker)
    prompt = ("Integration check. Read marker.txt in the current working directory. Do not edit any file. "
              "Return outcome ready, summary 'Adapter verified', report containing the exact contents of marker.txt, and an empty question. "
              "Return only JSON conforming to: " + json.dumps(response_schema(["ready"])))
    result = agent_call(assignment, phase, prompt, Path(directory), 120, lambda: None)
    if marker not in result["report"]:
        raise SystemExit("FAILED: response did not contain the file's contents")
    print(f"PASS: {args.role} · {assignment['runtime']['kind']} · structured response and file read verified")
