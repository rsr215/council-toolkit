from pathlib import Path
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from council.conversation import talk
from council.memory import compact_one, select_summaries
from council.demo import demo_roster
from council.runner import agent_call, drive
from council.store import Store
from council.workflow import CouncilError, fingerprint, workflow_config


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "project"
        self.work.mkdir()
        (self.work / "brief.txt").write_text("A plan without Markdown files.\n")
        self.project = {"version": 1, "name": "plain", "workspace": str(self.work),
                        "git": False, "include": ["*.txt"], "rules": "The owner decides architecture.",
                        "timeout_seconds": 10}
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)

    def test_reviewer_can_inspect_existing_project_without_implementation(self):
        workflow = workflow_config(Path(__file__).resolve().parents[1] / "council/workflows/review.toml")
        revision, files = fingerprint(self.project)
        task_id = self.store.create(self.project, workflow, demo_roster(), "Review the existing plan", revision, files)
        task = drive(self.store, task_id, notify=lambda _: None)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(set(task["data"]["outputs"]), {"review", "synthesize"})
        self.assertEqual((self.work / "brief.txt").read_text(), "A plan without Markdown files.\n")

    def test_coordinator_remembers_and_dispatches_requested_review(self):
        adapter = self.root / "fake.py"
        adapter.write_text('''import json,sys
prompt=sys.stdin.read()
packet=json.loads(prompt.split("TASK PACKET:\\n",1)[1].split("\\n\\nReturn ONLY JSON",1)[0])
if "owner_message" in packet:
    review="review" in packet["owner_message"].lower()
    print(json.dumps({"action":"review" if review else "reply",
        "message":"Review requested" if review else "Memory: " + packet["memory"],
        "memory":"Owner requested a review of the existing plan",
        "review_objective":"Review the current plan" if review else ""}))
else:
    print(json.dumps({"outcome":"clear" if packet["phase"]=="review" else "ready",
        "summary":"Simulated " + packet["phase"],"report":"No findings in simulation","question":""}))
''')
        roster = self.root / "roster.toml"
        roster.write_text(f'''[executors.fake]
kind="command"
trusted=true
argv=[{json.dumps(sys.executable)}, {json.dumps(str(adapter))}]
[workflow_roles.coordinator]
executor="fake"
model="test-model"
[workflow_roles.reviewer]
executor="fake"
model="test-reviewer"
''')
        with self.assertRaises(CouncilError):
            talk(self.store, self.project, roster, "Please have the reviewer review this plan")
        self.assertEqual(self.store.listing(), [])
        first = talk(self.store, self.project, roster, "Please have the reviewer review this plan", allow_review=True)
        self.assertEqual(first["action"], "review")
        self.assertEqual(first["task"]["status"], "completed")
        self.assertEqual(set(first["task"]["data"]["outputs"]), {"review", "synthesize"})
        second = talk(self.store, self.project, roster, "What did you remember?")
        self.assertEqual(second["action"], "reply")
        self.assertIn("Owner requested a review", second["message"])
        history = self.store.conversation("plain")
        self.assertEqual(len(history["turns"]), 2)
        self.assertEqual(history["turns"][0]["task_id"], first["task_id"])
        self.assertEqual((self.work / "brief.txt").read_text(), "A plan without Markdown files.\n")

    def test_same_conversation_rejects_overlapping_turns(self):
        with self.store.conversation_lock("plain", "main"):
            with self.assertRaises(CouncilError):
                with self.store.conversation_lock("plain", "main"):
                    pass

    def test_existing_version_one_ledger_migrates_without_losing_tasks(self):
        old = self.root / "old"
        old.mkdir(mode=0o700)
        with sqlite3.connect(old / "council.sqlite3") as db:
            db.executescript("""CREATE TABLE tasks (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, closed REAL, data TEXT NOT NULL);
                CREATE TABLE project_context (project TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE events (seq INTEGER PRIMARY KEY, task TEXT NOT NULL, at REAL NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL);
                PRAGMA user_version=1;""")
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                       ("old-task", "plain", "completed", "done", 3, 1.0, 2.0, 2.0,
                        json.dumps({"objective": "Legacy objective", "outputs": {}})))
        store = Store(old)
        try:
            self.assertEqual(store.get("old-task")["data"]["objective"], "Legacy objective")
            store.append_exchange("plain", "main", "Question", "Answer", "Decision", "reply")
            self.assertEqual(store.conversation("plain")["turns"][0]["assistant_text"], "Answer")
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        finally:
            store.close()
        store = Store(old)
        try:
            self.assertEqual(store.get("old-task")["revision"], 3)
            self.assertEqual(len(store.conversation("plain")["turns"]), 1)
        finally:
            store.close()

    def test_version_two_conversation_migrates_with_raw_turns(self):
        old = self.root / "version-two"
        old.mkdir(mode=0o700)
        with sqlite3.connect(old / "council.sqlite3") as db:
            db.executescript("""CREATE TABLE conversations (
                project TEXT NOT NULL, thread TEXT NOT NULL, memory TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(project, thread));
                CREATE TABLE conversation_turns (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
                at REAL NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
                action TEXT NOT NULL, task_id TEXT);
                PRAGMA user_version=2;""")
            db.execute("INSERT INTO conversations VALUES (?,?,?,?)", ("plain", "main", "older memory", 1.0))
            db.execute("INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
                       (1, "plain", "main", 1.0, "owner", "coordinator", "reply", None))
        store = Store(old)
        try:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(store.conversation("plain")["memory"], "older memory")
            self.assertEqual(store.all_conversation_turns("plain")[0]["user_text"], "owner")
        finally:
            store.close()

    def test_failed_schema_migration_rolls_back(self):
        old = self.root / "broken"
        old.mkdir(mode=0o700)
        with sqlite3.connect(old / "council.sqlite3") as db:
            db.executescript("""CREATE TABLE conversation_turns (wrong_column TEXT);
                PRAGMA user_version=1;""")
        with self.assertRaises(sqlite3.OperationalError):
            Store(old)
        with sqlite3.connect(old / "council.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='conversations'").fetchone())

    def test_old_turns_are_archived_only_after_successful_summary(self):
        for number in range(32):
            self.store.append_exchange("plain", "main", str(number), "answer " * 10, str(number), "reply")
        candidate = self.store.compaction_candidate("plain", "main")
        self.assertEqual(candidate["kind"], "turns")
        self.assertEqual(len(candidate["turns"]), 16)
        with mock.patch("council.memory.agent_call", return_value={"summary": "Owner discussed a plan."}):
            result = compact_one(self.store, "plain", "main", {"model": "fake", "runtime": {}}, 10)
        self.assertEqual(result["level"], 1)
        recent = self.store.conversation("plain", limit=200)
        self.assertEqual(len(recent["turns"]), 16)
        self.assertEqual(recent["turns"][0]["user_text"], "16")
        self.assertEqual(recent["memory"], "31")
        self.assertEqual(recent["summaries"][0]["summary"], "Owner discussed a plan.")
        all_turns = self.store.all_conversation_turns("plain")
        self.assertEqual([turn["user_text"] for turn in all_turns], [str(n) for n in range(32)])

    def test_large_turns_trigger_compaction_before_count_threshold(self):
        for number in range(16):
            self.store.append_exchange("plain", "main", str(number), "x" * 6000, "memory", "reply")
        candidate = self.store.compaction_candidate("plain", "main")
        self.assertEqual(candidate["kind"], "turns")
        self.assertLess(candidate["last_turn"], self.store.conversation("plain")["turns"][-1]["id"])
        self.assertLessEqual(len(json.dumps(candidate["turns"])), 49_000)

    def test_thirty_one_turns_compact_before_the_next_reply(self):
        for number in range(31):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        self.assertEqual(self.store.compaction_candidate("plain", "main")["kind"], "turns")
        assignment = {"model": "fake", "runtime": {"kind": "command", "trusted": True,
                                                    "argv": [sys.executable]}}
        answer = {"action": "reply", "message": "Answer", "memory": "working", "review_objective": ""}
        with mock.patch("council.conversation.coordinator_assignment", return_value=assignment), \
             mock.patch("council.conversation.preflight"), \
             mock.patch("council.conversation.agent_call", return_value=answer), \
             mock.patch("council.memory.agent_call", return_value={"summary": "Older context"}):
            result = talk(self.store, self.project, self.root / "unused.toml", "Next question")
        self.assertEqual(result["compactions"][0]["kind"], "turns")
        self.assertIsNone(self.store.compaction_candidate("plain", "main"))
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 32)

    def test_chat_compacts_automatically_without_losing_reply(self):
        for number in range(32):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        answer = {"action": "reply", "message": "Here is the answer", "memory": "durable context",
                  "review_objective": ""}
        assignment = {"model": "fake", "runtime": {"kind": "command", "trusted": True,
                                                    "argv": [sys.executable]}}
        with mock.patch("council.conversation.coordinator_assignment", return_value=assignment), \
             mock.patch("council.conversation.preflight"), \
             mock.patch("council.conversation.agent_call", return_value=answer), \
             mock.patch("council.memory.agent_call", return_value={"summary": "Older decisions"}):
            result = talk(self.store, self.project, self.root / "unused.toml", "New question")
        self.assertEqual(result["message"], "Here is the answer")
        self.assertEqual(result["compactions"][0]["level"], 1)
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 33)

    def test_failed_summarizer_preserves_raw_and_does_not_commit_reply(self):
        for number in range(32):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        assignment = {"model": "fake", "runtime": {"kind": "command", "trusted": True,
                                                    "argv": [sys.executable]}}
        with mock.patch("council.conversation.coordinator_assignment", return_value=assignment), \
             mock.patch("council.conversation.preflight"), \
             mock.patch("council.memory.agent_call", side_effect=CouncilError("model unavailable")):
            with self.assertRaisesRegex(CouncilError, "model unavailable"):
                talk(self.store, self.project, self.root / "unused.toml", "New question")
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 32)
        self.assertEqual(self.store.conversation("plain")["summaries"], [])

    def test_summary_levels_merge_without_losing_archive(self):
        for number in range(80):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
            candidate = self.store.compaction_candidate("plain", "main")
            if candidate and candidate["kind"] == "turns":
                self.store.save_compaction("plain", "main", candidate, f"Span {candidate['first_turn']}")
        candidate = self.store.compaction_candidate("plain", "main")
        self.assertEqual(candidate["kind"], "summaries")
        self.assertEqual(candidate["level"], 2)
        self.store.save_compaction("plain", "main", candidate, "Merged older discussion")
        self.assertEqual([entry["level"] for entry in self.store.conversation("plain")["summaries"]], [2])
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 80)

    def test_summary_budget_merges_contiguous_spans_before_chat(self):
        for number, level in enumerate((1, 2, 1, 2)):
            self.store.db.execute("""INSERT INTO conversation_segments
                (project,thread,level,first_turn,last_turn,summary,active,created)
                VALUES (?,?,?,?,?,?,1,0)""",
                ("plain", "main", level, number * 10 + 1, number * 10 + 10,
                 f"Span {number} " + "x" * 2410))
        self.assertGreater(self.store.summary_context_gap("plain", "main"), 0)
        candidate = self.store.compaction_candidate("plain", "main")
        self.assertEqual(candidate["kind"], "summaries")
        self.assertEqual(candidate["level"], 3)
        self.assertEqual([source["first_turn"] for source in candidate["sources"]], [1, 11, 21, 31])
        assignment = {"model": "fake", "runtime": {"kind": "command", "trusted": True,
                                                    "argv": [sys.executable]}}
        answer = {"action": "reply", "message": "Answer", "memory": "working", "review_objective": ""}
        with mock.patch("council.conversation.coordinator_assignment", return_value=assignment), \
             mock.patch("council.conversation.preflight"), \
             mock.patch("council.conversation.agent_call", return_value=answer) as reply_call, \
             mock.patch("council.memory.agent_call", return_value={"summary": "All four spans"}):
            result = talk(self.store, self.project, self.root / "unused.toml", "Question")
        self.assertEqual(result["compactions"][0]["kind"], "summaries")
        self.assertEqual(self.store.summary_context_gap("plain", "main"), 0)
        self.assertEqual(self.store.conversation("plain")["summaries"][0]["summary"], "All four spans")
        self.assertIn('"All four spans"', reply_call.call_args.args[2])

    def test_same_level_merge_never_crosses_an_active_summary(self):
        for number, level in enumerate((1, 2, 1, 1, 1)):
            self.store.db.execute("""INSERT INTO conversation_segments
                (project,thread,level,first_turn,last_turn,summary,active,created)
                VALUES (?,?,?,?,?,?,1,0)""",
                ("plain", "main", level, number * 10 + 1, number * 10 + 10, "short",))
        self.assertIsNone(self.store.compaction_candidate("plain", "main"))

    def test_failed_archive_transaction_keeps_raw_turns(self):
        for number in range(32):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        candidate = self.store.compaction_candidate("plain", "main")
        self.store.db.execute("""CREATE TRIGGER prevent_compaction BEFORE DELETE ON conversation_turns
            BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_compaction("plain", "main", candidate, "Summary")
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 32)
        self.assertEqual(self.store.conversation("plain")["summaries"], [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM conversation_archives").fetchone()[0], 0)

    def test_large_backlog_fails_closed_then_can_catch_up(self):
        for number in range(16):
            self.store.append_exchange("plain", "main", "u" * 8000 + str(number), "x" * 8000,
                                       "memory", "reply")
        assignment = {"model": "fake", "runtime": {"kind": "command", "trusted": True,
                                                    "argv": [sys.executable]}}
        answer = {"action": "reply", "message": "Answer", "memory": "working", "review_objective": ""}
        with mock.patch("council.conversation.coordinator_assignment", return_value=assignment), \
             mock.patch("council.conversation.preflight"), \
             mock.patch("council.conversation.agent_call", return_value=answer) as reply_call, \
             mock.patch("council.memory.agent_call", return_value={"summary": "Older context"}):
            with self.assertRaisesRegex(CouncilError, "run council compact"):
                talk(self.store, self.project, self.root / "unused.toml", "New question")
            reply_call.assert_not_called()
            while self.store.context_gap("plain", "main"):
                compact_one(self.store, "plain", "main", assignment, 10)
            result = talk(self.store, self.project, self.root / "unused.toml", "New question")
        self.assertEqual(result["message"], "Answer")
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 17)

    def test_existing_insecure_home_is_rejected(self):
        public = self.root / "public-ledger"
        public.mkdir()
        os.chmod(public, 0o755)
        with self.assertRaisesRegex(CouncilError, "chmod 700"):
            Store(public)
        os.chmod(public, 0o700)
        store = Store(public)
        try:
            self.assertEqual(public.stat().st_mode & 0o777, 0o700)
            self.assertEqual((public / "council.sqlite3").stat().st_mode & 0o777, 0o600)
        finally:
            store.close()

    def test_prompt_summary_selection_includes_all_or_refuses(self):
        summaries = [dict(id=n, level=1, first_turn=n*10, last_turn=n*10+9,
                          summary=f"Span {n} " + "x" * 1000)
                     for n in range(4)]
        self.assertEqual([item["id"] for item in select_summaries(summaries, max_chars=4500)],
                         [0, 1, 2, 3])
        with self.assertRaisesRegex(CouncilError, "prompt budget"):
            select_summaries(summaries, max_chars=4000)

    def test_review_task_and_conversation_receipt_commit_together(self):
        workflow = workflow_config(Path(__file__).resolve().parents[1] / "council/workflows/review.toml")
        revision, files = fingerprint(self.project)
        exchange = {"thread": "main", "user_text": "Review this", "assistant_text": "Dispatching review",
                    "memory": "Owner requested a review"}
        with mock.patch.object(self.store, "_append_exchange", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.create(self.project, workflow, demo_roster(), "Review", revision, files, exchange)
        self.assertEqual(self.store.listing(), [])
        self.assertEqual(self.store.conversation("plain")["turns"], [])

    def test_provider_failure_does_not_save_raw_diagnostics(self):
        assignment = {"model": "model", "runtime": {"kind": "copilot-cli", "binary": "copilot"}}
        with mock.patch("council.runner.execute", return_value=(1, "secret prompt fragment", "token=private")):
            with self.assertRaises(CouncilError) as raised:
                agent_call(assignment, {"read_only": True, "routes": {"ready": "@done"}},
                           "prompt", self.work, 10, lambda: None)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_structured_provider_errors_do_not_expose_raw_diagnostics(self):
        assignment = {"model": "model", "runtime": {"kind": "claude-cli", "binary": "claude"}}
        envelope = json.dumps({"is_error": True, "result": "token=private"})
        with mock.patch("council.runner.execute", return_value=(0, envelope, "")):
            with self.assertRaises(CouncilError) as raised:
                agent_call(assignment, {"read_only": True, "routes": {"ready": "@done"}},
                           "prompt", self.work, 10, lambda: None)
        self.assertNotIn("private", str(raised.exception))

        from council.antigravity import parse_response
        stream = json.dumps({"event": "result", "result": {"status": "ERROR", "error": "token=private"}})
        with self.assertRaises(CouncilError) as raised:
            parse_response(stream, "model")
        self.assertNotIn("private", str(raised.exception))

    def test_command_adapter_requires_explicit_host_trust(self):
        assignment = {"model": "fake", "runtime": {"kind": "command", "argv": [sys.executable]}}
        with self.assertRaises(CouncilError):
            agent_call(assignment, {"read_only": True, "routes": {"ready": "@done"}},
                       "prompt", self.work, 10, lambda: None)

    def test_copilot_runtime_exposes_only_phase_tools(self):
        assignment = {"model": "gpt-6-sol", "effort": "high",
                      "runtime": {"kind": "copilot-cli", "binary": "copilot"}}
        reply = {"outcome": "ready", "summary": "done", "report": "ok", "question": ""}
        with mock.patch("council.runner.execute", return_value=(0, json.dumps(reply), "")) as execute:
            agent_call(assignment, {"read_only": True, "routes": {"ready": "@done"}},
                       "Sensitive prompt", self.work, 10, lambda: None)
        argv = execute.call_args.args[0]
        self.assertIn("--available-tools=view,glob,grep", argv)
        self.assertIn("--allow-tool=read", argv)
        self.assertNotIn("Sensitive prompt", argv)
        self.assertNotIn("--allow-all-tools", argv)
        self.assertIn("--no-custom-instructions", argv)


if __name__ == "__main__":
    unittest.main()
