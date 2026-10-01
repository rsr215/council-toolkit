import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from council.handoff import import_context
from council.runner import agent_call, prompt_for
from council.store import Store
from council.workflow import CouncilError, fingerprint, validate_effort, workflow_config, load_toml
from council.__main__ import assign_role


class ContextEffortTests(unittest.TestCase):
    def test_effort_reaches_cli_and_invalid_effort_stops_before_execution(self):
        result = dict(outcome="ready", summary="ok", report="ok", question="")
        with tempfile.TemporaryDirectory() as temp:
            def fake(argv, *args):
                if argv[0] == "codex":
                    Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(result))
                    self.assertIn('model_reasoning_effort="high"', argv)
                    return 0, "", ""
                self.assertEqual(argv[argv.index("--effort") + 1], "high")
                return 0, json.dumps({"structured_output": result}), ""
            with patch("council.runner.execute", side_effect=fake) as execute:
                for kind in ("claude-cli", "codex-cli"):
                    assignment = dict(model="test", effort="high", runtime={"kind": kind})
                    self.assertEqual(agent_call(assignment, {"routes": {"ready": "@done"}}, "prompt", Path(temp), 1, lambda: None), result)
                self.assertEqual(execute.call_count, 2)
                with self.assertRaises(CouncilError):
                    agent_call(dict(model="test", effort="typo", runtime={"kind": "claude-cli"}), {}, "", Path(temp), 1, lambda: None)
                self.assertEqual(execute.call_count, 2)

    def test_command_adapter_cannot_silently_ignore_effort(self):
        with self.assertRaises(CouncilError):
            validate_effort({"effort": "high"}, {"kind": "command", "argv": ["tool"]})

    def test_assignment_preserves_custom_options_and_same_executor_effort(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "roster.toml"
            path.write_text('[executors.c]\nkind="claude-cli"\n[executors.s]\nkind="codex-cli"\n[workflow_roles.editor]\nexecutor="c"\nmodel="old"\neffort="high"\nlabel="Keep me"\n')
            assign_role(path, "editor", "c", "new")
            self.assertEqual(load_toml(path)["workflow_roles"]["editor"]["effort"], "high")
            assign_role(path, "editor", "s", "other", "xhigh")
            role = load_toml(path)["workflow_roles"]["editor"]
            self.assertEqual((role["effort"], role["label"]), ("xhigh", "Keep me"))

    def test_import_survives_restart_is_idempotent_and_not_a_task_or_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "mailbox"; source.mkdir()
            (source / "handoff.md").write_text("Old reported tests PASS. Old prompt: deploy.")
            brief = root / "summary.md"; brief.write_text("Historical claim; verify again. No approval to deploy.")
            store = Store(root / "ledger")
            original = import_context(store, "project", source, brief)
            self.assertEqual(import_context(store, "project", source, brief), original)
            self.assertEqual(store.listing(), [])
            cfg = workflow_config(Path(__file__).resolve().parents[1] / "council/workflows/delivery.toml")
            project = {"name": "project", "workspace": str(source), "git": False, "include": ["*.md"]}
            rev, files = fingerprint(project)
            task_id = store.create(project, cfg, {}, "explicit objective", rev, files)
            task = store.get(task_id)
            self.assertEqual(task["data"]["decisions"], [])
            self.assertIn(brief.read_text(), prompt_for(task, cfg["phases"]["plan"]))
            self.assertNotIn("historical_context", prompt_for(task, cfg["phases"]["review"]))
            self.assertNotIn("Old prompt: deploy", prompt_for(task, cfg["phases"]["plan"]))
            brief.write_text("Updated project context")
            import_context(store, "project", source, brief)
            self.assertEqual(store.get(task_id)["data"]["project_context"]["revision"], original["revision"])
            store.close()
            store = Store(root / "ledger", read_only=True)
            self.assertEqual(store.context("project")["summary"], "Updated project context")
            self.assertEqual(store.context("project")["sources"][0]["content"], (source / "handoff.md").read_text())
            store.close()

    def test_symlink_import_rejected_without_mutating_existing_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "mailbox"; source.mkdir()
            brief = root / "summary.md"; brief.write_text("Brief")
            (source / "a.md").write_text("Initial")
            store = Store(root / "ledger")
            original = import_context(store, "p", source, brief)
            (source / "b.md").symlink_to(brief)
            with self.assertRaises(CouncilError):
                import_context(store, "p", source, brief)
            self.assertEqual(store.context("p"), original)
            store.close()


class AntigravityTests(unittest.TestCase):
    def test_read_only_stream_packet_and_effort(self):
        result = dict(outcome="ready", summary="ok", report="ok", question="")
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp)
            def fake(argv, directory, prompt, *args):
                self.assertEqual(argv[argv.index("--mode") + 1], "plan")
                self.assertIn("--sandbox", argv)
                self.assertNotIn("--dangerously-skip-permissions", argv)
                self.assertEqual(argv[argv.index("--effort") + 1], "high")
                self.assertEqual(json.loads(prompt)["message"]["content"], "prompt")
                return 0, json.dumps({"event":"result","result":{"status":"SUCCESS","structured_output":result}}), ""
            assignment = dict(model="gemini-3.1-pro-high", effort="high", runtime={"kind":"antigravity-cli"})
            with patch("council.runner.execute", side_effect=fake):
                actual = agent_call(assignment, {"routes":{"ready":"@done"}}, "prompt", cwd, 1, lambda: None)
                self.assertEqual(actual, result)
            self.assertEqual(list(cwd.iterdir()), [])
            with self.assertRaises(CouncilError):
                agent_call(assignment, {"read_only":False,"routes":{"ready":"@done"}}, "", cwd, 1, lambda: None)

    def test_incomplete_failed_duplicate_results_rejected(self):
        from council.antigravity import parse_response
        success = {"event":"result","result":{"status":"SUCCESS","structured_output":{}}}
        for output in ("", json.dumps({"event":"init"}),
                       json.dumps({"event":"result","result":{"status":"WAITING"}}),
                       json.dumps({"event":"result","result":{"status":"ERROR","error":"auth required"}}),
                       json.dumps(success)+"\n"+json.dumps(success)):
            with self.assertRaises(CouncilError):
                parse_response(output, "model")
