"""Explicit simulation for demonstrations and tests; never a provider fallback."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


def adapter(behavior: str):
    prompt = sys.stdin.read()
    packet = json.loads(prompt.split("TASK PACKET:\n", 1)[1].split("\n\nReturn ONLY JSON", 1)[0])
    phase = packet["phase"]
    result = {"outcome": "ready", "summary": "Simulated " + phase,
              "report": "Simulation only; no model was called.", "question": ""}
    if behavior == "malformed":
        print("Not JSON")
        return
    if behavior == "ask" and phase == "plan" and not packet["owner_decisions"]:
        result.update(outcome="needs_owner", question="Simulated architecture choice: use the approved small design?")
    elif phase == "implement":
        repaired = "disposition" in packet["inputs"]
        Path("brief.txt").write_text("corrected\n" if repaired or behavior != "repair" else "draft\n")
        result["summary"] = "Simulated deliverable updated"
    elif phase == "review":
        result["outcome"] = "findings" if Path("brief.txt").read_text().strip() == "draft" else "clear"
    elif phase == "disposition":
        review = packet["inputs"].get("review", {})
        check = packet["inputs"].get("verify", {})
        result["outcome"] = "repair" if review.get("outcome") == "findings" or check.get("outcome") == "fail" else "ready"
        if behavior == "ignore_failure":
            result["outcome"] = "ready"
    print(json.dumps(result))


def demo_roster(behavior: str = "repair") -> dict:
    runtime = {"kind": "command", "trusted": True, "argv": [sys.executable, str(Path(__file__).resolve()), "--adapter", behavior]}
    return {role: {"model": "explicit-simulation", "executor": "demo", "runtime": runtime}
            for role in ("coordinator", "implementer", "reviewer", "researcher")}


def run_demo() -> int:
    from .runner import decide, drive, summary
    from .store import Store
    from .workflow import fingerprint, workflow_config
    with tempfile.TemporaryDirectory(prefix="council-demo-") as directory:
        root = Path(directory)
        workspace = root / "project"
        workspace.mkdir()
        (workspace / "brief.txt").write_text("initial\n")
        project = {"version": 1, "name": "demo", "workspace": str(workspace), "git": False,
                   "include": ["*.txt"], "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('brief.txt').exists(); print('demo check passed')"]],
                   "rules": "Simulation only. No real project or account access.", "timeout_seconds": 10}
        cfg = workflow_config(Path(__file__).parent / "workflows" / "delivery.toml")
        store = Store(root / "state")
        try:
            revision, files = fingerprint(project)
            task_id = store.create(project, cfg, demo_roster(), "Exercise implementation, review and repair", revision, files)
            task = drive(store, task_id)
            print(summary(task))
            assert task["status"] == "completed" and task["data"]["visits"]["implement"] == 2
            revision, files = fingerprint(project)
            owner_id = store.create(project, cfg, demo_roster("ask"), "Exercise an architecture decision", revision, files)
            owner_task = drive(store, owner_id)
            assert owner_task["status"] == "waiting"
            decide(store, owner_id, owner_task["data"]["gate"]["id"], "respond", "Use the approved small design")
            assert drive(store, owner_id)["status"] == "completed"
            print("\nSimulation passed: automatic routine completion, one repair, and a revision-bound owner decision.")
            print("Demo files are removed on exit. No model calls or messages sent.")
        finally:
            store.close()
    return 0


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--adapter":
    adapter(sys.argv[2])
