import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from council.__main__ import assign_role
from council.store import Store
from council.workflow import load_toml, workflow_config, CouncilError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "documents"
        self.workspace.mkdir()
        (self.workspace / "notes.md").write_text("Research material")
        self.base = [sys.executable, "-m", "council", "--home", str(self.root / "home")]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run([*self.base, *args], capture_output=True, text=True)

    def test_init_keeps_project_clean_and_refuses_overwrite(self):
        result = self.call("init", str(self.workspace), "--name", "notes", "--files")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in self.workspace.iterdir()], ["notes.md"])
        self.assertEqual(load_toml(self.root / "home/projects/notes.toml")["include"], [])
        result = self.call("init", str(self.workspace), "--name", "notes", "--files")
        self.assertEqual(result.returncode, 2)

    def test_default_roster_is_bundled_and_assign_creates_personal_copy(self):
        from council.__main__ import BUNDLED_ROSTER
        original = BUNDLED_ROSTER.read_text()
        result = self.call("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("coordinator:", result.stdout)
        result = self.call("assign", "coordinator", "--executor", "codex", "--model", "test-model", "--effort", "high")
        self.assertEqual(result.returncode, 0, result.stderr)
        personal = self.root / "home/roster.toml"
        self.assertEqual(load_toml(personal)["workflow_roles"]["coordinator"]["model"], "test-model")
        self.assertEqual(BUNDLED_ROSTER.read_text(), original)

    def test_review_command_queues_without_implementer_or_checks(self):
        self.call("init", str(self.workspace), "--name", "notes", "--files")
        project_path = self.root / "home/projects/notes.toml"
        project_path.write_text(project_path.read_text().replace("include = []", 'include = ["*.md"]'))
        roster = self.root / "roster.toml"
        roster.write_text(f'[executors.fake]\nkind="command"\ntrusted=true\nargv=[{json.dumps(sys.executable)}, "fake.py"]\n'
                          '[workflow_roles.coordinator]\nexecutor="fake"\nmodel="one"\n'
                          '[workflow_roles.reviewer]\nexecutor="fake"\nmodel="two"\n')
        result = subprocess.run([*self.base, "--roster", str(roster), "review", "notes",
                                 "Review existing notes", "--plan-only", "--json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        task = json.loads(result.stdout)
        self.assertEqual(task["status"], "ready")
        self.assertEqual(set(task["data"]["workflow"]["phases"]), {"review", "synthesize"})

    def test_history_all_recovers_archived_full_turns(self):
        self.assertEqual(self.call("init", str(self.workspace), "--name", "notes", "--files").returncode, 0)
        store = Store(self.root / "home")
        try:
            for number in range(32):
                store.append_exchange("notes", "main", str(number), "answer", "memory", "reply")
            store.save_compaction("notes", "main", store.compaction_candidate("notes", "main"), "Older context")
        finally:
            store.close()
        result = self.call("chat", "notes", "--history", "--all", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        history = json.loads(result.stdout)
        self.assertEqual(len(history["turns"]), 32)
        self.assertEqual(history["turns"][0]["user_text"], "0")
        self.assertEqual(history["summaries"][0]["summary"], "Older context")

    def test_history_shows_native_session_and_metrics_on_new_and_old_ledgers(self):
        self.assertEqual(self.call("init", str(self.workspace), "--name", "notes", "--files").returncode, 0)
        store = Store(self.root / "home")
        try:
            binding = {"runtime": "claude-cli", "session_id": "session-1", "context_key": "key",
                       "workspace_revision": "rev", "last_turn_id": None, "session_turns": 1,
                       "rotation_reason": "no native session yet", "created": 1.0}
            store.append_exchange("notes", "main", "Question", "Answer", "memory", "reply",
                                  chat={"binding": binding, "metrics": {"mode": "native-fresh", "wall_ms": 12,
                                                                        "rotation_reason": "no native session yet"}})
        finally:
            store.close()
        result = self.call("chat", "notes", "--history", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        history = json.loads(result.stdout)
        self.assertEqual((history["session"]["session_turns"], history["session"]["pending"]), (1, 0))
        self.assertEqual(history["metrics"][0]["mode"], "native-fresh")
        self.assertIsNone(history["metrics"][0]["input_tokens"])
        text = self.call("chat", "notes", "--history")
        self.assertIn("Native session: claude-cli · 1 turns · started because: no native session yet", text.stdout)
        self.assertIn("Last turn: native-fresh · wall_ms=12", text.stdout)
        self.assertEqual(self.call("chat", "notes", "--history", "--new-session").returncode, 2)
        db = sqlite3.connect(self.root / "home/council.sqlite3")
        db.executescript("DROP TABLE chat_sessions; DROP TABLE chat_metrics; PRAGMA user_version=3;")
        db.close()
        result = self.call("chat", "notes", "--history", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        history = json.loads(result.stdout)
        self.assertEqual((history["session"], history["metrics"]), (None, []))
        self.assertEqual(history["turns"][0]["assistant_text"], "Answer")

    def test_doctor_reports_chat_session_mode_and_fallback(self):
        result = self.call("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Chat session: native; resumes up to 20 turns or until the previous turn "
                      "reported >= 100000 input/cache tokens", result.stdout)
        roster = self.root / "roster.toml"
        native = ('[executors.claude]\nkind="claude-cli"\nbinary="claude"\n'
                  '[workflow_roles.coordinator]\nexecutor="claude"\nmodel="one"\nchat_session="native"\n')
        roster.write_text(native + "chat_session_context_tokens = 5000\n")
        result = self.call("--roster", str(roster), "doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reported >= 5000 input/cache tokens", result.stdout)
        for invalid in ("0", "999", "2000001", "true", "1500.5", '"100000"'):
            with self.subTest(value=invalid):
                roster.write_text(native + f"chat_session_context_tokens = {invalid}\n")
                result = self.call("--roster", str(roster), "doctor")
                self.assertEqual(result.returncode, 2)
                self.assertIn("chat_session_context_tokens must be an integer between 1000 and 2000000", result.stderr)
        roster.write_text('[executors.codex]\nkind="codex-cli"\nbinary="codex"\n'
                          '[workflow_roles.coordinator]\nexecutor="codex"\nmodel="one"\nchat_session="native"\n')
        result = self.call("--roster", str(roster), "doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("native requested; codex-cli unsupported, ledger fallback", result.stdout)
        roster.write_text(roster.read_text() + "chat_session_turns = 0\n")
        result = self.call("--roster", str(roster), "doctor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("chat_session_turns", result.stderr)

    def test_assign_keeps_chat_session_settings(self):
        path = self.root / "roster.toml"
        path.write_text('[executors.claude]\nkind="claude-cli"\nbinary="claude"\n'
                        '[executors.codex]\nkind="codex-cli"\nbinary="codex"\n\n'
                        '[workflow_roles.coordinator]\nexecutor="claude"\nmodel="old"\n'
                        'chat_session = "native"\nchat_session_turns = 5\nchat_session_context_tokens = 50000\n')
        assign_role(path, "coordinator", "codex", "new", "high")
        role = load_toml(path)["workflow_roles"]["coordinator"]
        self.assertEqual((role["executor"], role["chat_session"], role["chat_session_turns"],
                          role["chat_session_context_tokens"]), ("codex", "native", 5, 50000))

    def test_compact_dry_run_reports_due_work_without_model_call(self):
        self.assertEqual(self.call("init", str(self.workspace), "--name", "notes", "--files").returncode, 0)
        store = Store(self.root / "home")
        try:
            for number in range(32):
                store.append_exchange("notes", "main", str(number), "answer", "memory", "reply")
        finally:
            store.close()
        result = self.call("compact", "notes", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["next_level"], 1)

    def test_git_project_without_markdown_can_queue_review(self):
        code = self.root / "code"
        code.mkdir()
        (code / "app.py").write_text("value = 1\n")
        subprocess.run(["git", "-C", str(code), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(code), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(code), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "Initial"], check=True)
        self.assertEqual(self.call("init", str(code), "--name", "code", "--workflow", "review").returncode, 0)
        result = self.call("review", "code", "Review app.py", "--plan-only", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        self.assertEqual([p.name for p in code.iterdir() if p.name != ".git"], ["app.py"])

    def test_editing_run_requires_explicit_workspace(self):
        self.call("init", str(self.workspace), "--name", "notes", "--files")
        result = self.call("run", "notes", "Update notes", "--plan-only")
        self.assertEqual(result.returncode, 2)
        self.assertIn("isolated working copy", result.stderr)

    def test_role_reassignment_only_changes_configuration(self):
        path = self.root / "roster.toml"
        path.write_text('[executors.test]\nkind="command"\nargv=["tool"]\n\n[workflow_roles.editor]\nexecutor="test"\nmodel="old"\n\n[unrelated]\nkeep=true\n')
        assign_role(path, "editor", "test", 'new-model$()`literal')
        cfg = load_toml(path)
        self.assertEqual(cfg["workflow_roles"]["editor"]["model"], 'new-model$()`literal')
        self.assertTrue(cfg["unrelated"]["keep"])
        assign_role(path, "fact-checker", "test", "new")
        self.assertIn("fact-checker", load_toml(path)["workflow_roles"])

    def test_assign_preserves_optional_inline_and_nested_toml(self):
        path = self.root / "roster.toml"
        path.write_text('''[executors.test]
kind="codex-cli"
binary="codex"

[workflow_roles.editor]
executor="test"
model="old"
effort="high"
options = { temperature = 0.1 }

[workflow_roles.editor.metadata]
source = "custom"
''')
        assign_role(path, "editor", "test", "new")
        cfg = load_toml(path)
        self.assertEqual(cfg["workflow_roles"]["editor"]["model"], "new")
        self.assertEqual(cfg["workflow_roles"]["editor"]["options"], {"temperature": 0.1})
        self.assertEqual(cfg["workflow_roles"]["editor"]["metadata"], {"source": "custom"})

    def test_assign_does_not_delete_someone_elses_temporary_file(self):
        path = self.root / "roster.toml"
        path.write_text('[executors.test]\nkind="command"\nargv=["tool"]\n')
        temp = path.with_name(path.name + ".tmp")
        temp.write_text("someone else's write")
        with self.assertRaises(FileExistsError):
            assign_role(path, "editor", "test", "new")
        self.assertEqual(temp.read_text(), "someone else's write")

    def test_invalid_workflow_route_rejected(self):
        path = self.root / "bad.toml"
        path.write_text('version=1\nentry="start"\nmax_steps=2\n[phases.start]\nkind="human"\n[phases.start.routes]\napprove="missing"\n')
        with self.assertRaises(CouncilError):
            workflow_config(path)

    def test_status_json_is_machine_readable(self):
        result = self.call("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertFalse((self.root / "home").exists())



if __name__ == "__main__":
    unittest.main()
