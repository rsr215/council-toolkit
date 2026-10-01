from pathlib import Path
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from council.conversation import coordinator_assignment, talk
from council.memory import compact_one
from council.runner import Interrupted
from council.store import Store
from council.workflow import CouncilError

# Stands in for the Claude CLI: records each call, answers chat, workflow and summary prompts,
# and returns an envelope with a session id and usage like `claude --print --output-format json`.
FAKE_CLAUDE = '''import json, os, sys, time
from pathlib import Path
here = Path(sys.argv[0]).parent
control = json.loads((here / "control.json").read_text()) if (here / "control.json").exists() else {}
argv = sys.argv[1:]
prompt = sys.stdin.read()
def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "files": sorted(os.listdir(".")),
                          "memory_off": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "prompt": prompt}) + "\\n")
fail = control.get("fail", "")
if fail == "resume" and "--resume" in argv:
    sys.exit(1)
if fail == "sleep":
    time.sleep(30)
if fail == "invalid":
    print("not json")
    sys.exit(0)
if fail == "touch":
    Path(control["touch"]).write_text("changed during the call\\n")
if "SOURCE MATERIAL" in prompt:
    answer = {"summary": "Older discussion"}
else:
    packet = json.loads(prompt.split("TASK PACKET:\\n", 1)[1].split("\\n\\nReturn ONLY JSON", 1)[0])
    if "owner_message" in packet:
        review = control.get("review", False)
        answer = {"action": "review" if review else "reply", "message": "Answer to " + packet["owner_message"],
                  "memory": "working memory", "review_objective": "Review the plan" if review else ""}
    else:
        answer = {"outcome": "clear" if packet["phase"] == "review" else "ready",
                  "summary": "Simulated " + packet["phase"], "report": "ok", "question": ""}
session = value("--resume") or ("returned-" + value("--session-id") if "--session-id" in argv else "ephemeral")
envelope = {"type": "result", "is_error": fail == "error", "session_id": session,
            "structured_output": answer, "duration_ms": 7, "total_cost_usd": 0.01}
if not control.get("no_usage"):
    envelope["usage"] = control.get("usage") or {"input_tokens": 11, "output_tokens": 5,
                                                 "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}
print(json.dumps(envelope))
'''


class NativeChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "project"
        self.work.mkdir()
        (self.work / "brief.txt").write_text("A plan without Markdown files.\n")
        self.project = {"version": 1, "name": "plain", "workspace": str(self.work),
                        "git": False, "include": ["*.txt"], "rules": "The owner decides architecture.",
                        "timeout_seconds": 10}
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.fake = self.root / "bin" / "claude"
        self.fake.parent.mkdir()
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CLAUDE)
        self.fake.chmod(0o755)
        self.roster = self.root / "roster.toml"
        self.write_roster()
        for target in ("council.conversation.preflight", "council.runner.preflight"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_roster(self, model: str = "test-model", extra: str = ""):
        self.roster.write_text(f'''[executors.claude]
kind = "claude-cli"
binary = {json.dumps(str(self.fake))}

[workflow_roles.coordinator]
executor = "claude"
model = {json.dumps(model)}
chat_session = "native"
{extra}

[workflow_roles.reviewer]
executor = "claude"
model = "test-reviewer"
''')

    def control(self, **values):
        (self.fake.parent / "control.json").write_text(json.dumps(values))

    def calls(self) -> list[dict]:
        log = self.fake.parent / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    @staticmethod
    def packet(call: dict) -> dict:
        return json.loads(call["prompt"].split("TASK PACKET:\n", 1)[1].split("\n\nReturn ONLY JSON", 1)[0])

    @staticmethod
    def flag(call: dict, name: str) -> str | None:
        return call["argv"][call["argv"].index(name) + 1] if name in call["argv"] else None

    def talk(self, message: str = "Question", **kwargs) -> dict:
        return talk(self.store, self.project, self.roster, message, **kwargs)

    def test_native_turns_resume_one_session_in_a_stable_directory(self):
        first = self.talk("First question")
        second = self.talk("Second question")
        one, two = self.calls()
        self.assertNotIn("--no-session-persistence", one["argv"] + two["argv"])
        self.assertEqual(self.flag(two, "--resume"), "returned-" + self.flag(one, "--session-id"))
        self.assertIsNone(self.flag(two, "--session-id"))
        for call in (one, two):
            self.assertIn("--restricted", call["argv"])
            self.assertEqual(self.flag(call, "--tools"), "Read,Glob,Grep")
            self.assertEqual(call["memory_off"], "1")
            self.assertIn("brief.txt", call["files"])
            self.assertIn("This turn is read-only", call["prompt"])
        self.assertEqual(one["cwd"], two["cwd"])
        self.assertEqual(Path(one["cwd"]).parent, self.store.home / "chat-cwd")
        self.assertFalse(Path(one["cwd"]).exists())
        self.assertIn("recent_conversation", self.packet(one))
        for omitted in ("recent_conversation", "older_summaries", "imported_summary", "rules"):
            self.assertNotIn(omitted, self.packet(two))
        self.assertEqual(self.packet(two)["owner_message"], "Second question")
        self.assertEqual((first["session_mode"], second["session_mode"]), ("native-fresh", "native-resume"))
        self.assertEqual(first["session_rotated"], "no native session yet")
        self.assertIsNone(second["session_rotated"])
        binding = self.store.chat_session("plain", "main")
        self.assertEqual((binding["session_turns"], binding["pending"]), (2, 0))
        self.assertEqual(binding["last_turn_id"], self.store.last_turn_id("plain", "main"))
        self.assertEqual([row["input_tokens"] for row in self.store.chat_metrics("plain", "main")], [11, 11])
        self.assertEqual((self.work / "brief.txt").read_text(), "A plan without Markdown files.\n")

    def test_workflow_phases_reviews_and_compaction_stay_ephemeral(self):
        self.control(review=True)
        result = self.talk("Please have the reviewer review this plan", allow_review=True)
        self.assertEqual(result["task"]["status"], "completed")
        chat, *phases = self.calls()
        self.assertIn("--session-id", chat["argv"])
        self.assertEqual(len(phases), 2)
        for call in phases:
            self.assertIn("--no-session-persistence", call["argv"])
            self.assertNotIn("--session-id", call["argv"])
            self.assertNotIn("--resume", call["argv"])
        self.assertEqual(self.store.chat_session("plain", "main")["last_turn_id"],
                         self.store.last_turn_id("plain", "main"))
        for number in range(32):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        compact_one(self.store, "plain", "main", coordinator_assignment(self.roster), 10)
        summary = self.calls()[-1]
        self.assertIn("--no-session-persistence", summary["argv"])
        self.assertNotIn("--resume", summary["argv"])

    def test_unsupported_runtime_keeps_ledger_context_fallback(self):
        self.roster.write_text(f'''[executors.fake]
kind = "command"
trusted = true
argv = [{json.dumps(sys.executable)}]
[workflow_roles.coordinator]
executor = "fake"
model = "test-model"
chat_session = "native"
''')
        answer = {"action": "reply", "message": "Answer", "memory": "working", "review_objective": ""}
        with mock.patch("council.conversation.agent_call", return_value=answer) as call:
            self.talk("First question")
            second = self.talk("Second question")
        for made in call.call_args_list:
            self.assertNotIn("session", made.kwargs)
        self.assertIn('"First question"', call.call_args.args[2])
        self.assertEqual((second["session_mode"], second["session_rotated"]), ("ledger", None))
        self.assertIsNone(self.store.chat_session("plain", "main"))
        self.assertEqual([row["mode"] for row in self.store.chat_metrics("plain", "main")], ["ledger", "ledger"])

    def test_context_changes_rotate_and_workspace_edits_resume_with_notice(self):
        moved = self.root / "moved"
        shutil.copytree(self.work, moved)
        changes = [lambda: self.write_roster(model="other-model"),
                   lambda: self.write_roster(model="other-model", extra='effort = "high"'),
                   lambda: self.project.update(rules="A new owner rule"),
                   lambda: self.store.save_context("plain", {"summary": "Imported history"}),
                   lambda: self.project.update(workspace=str(moved))]
        self.assertEqual(self.talk()["session_mode"], "native-fresh")
        for change in changes:
            change()
            rotated = self.talk()
            self.assertEqual(rotated["session_mode"], "native-fresh")
            self.assertIn("changed", rotated["session_rotated"])
            self.assertEqual(self.talk()["session_mode"], "native-resume")
        self.assertNotIn("workspace_changed", self.packet(self.calls()[-1]))
        (moved / "brief.txt").write_text("Edited plan\n")
        edited = self.talk("What changed?")
        self.assertEqual(edited["session_mode"], "native-resume")
        self.assertTrue(self.packet(self.calls()[-1])["workspace_changed"])
        self.assertIn("re-read any file", self.calls()[-1]["prompt"])
        self.assertEqual(self.talk()["session_mode"], "native-resume")
        self.assertNotIn("workspace_changed", self.packet(self.calls()[-1]))

    def test_ledger_turn_the_session_has_not_seen_starts_fresh(self):
        self.talk("First question")
        self.store.append_exchange("plain", "main", "Written by another path", "Answer", "memory", "reply")
        result = self.talk("Second question")
        self.assertEqual(result["session_rotated"], "ledger has turns this session has not seen")
        call = self.calls()[-1]
        self.assertIn("--session-id", call["argv"])
        self.assertIn("Written by another path", json.dumps(self.packet(call)["recent_conversation"]))

    def test_failed_turns_never_resume_an_unrecorded_reply(self):
        self.talk("First question")
        failures = [{"fail": "invalid"}, {"fail": "error"}, {"review": True},
                    {"fail": "touch", "touch": str(self.work / "brief.txt")}]
        for failure in failures:
            with self.subTest(failure=failure):
                self.control(**failure)
                turns = len(self.store.all_conversation_turns("plain"))
                with self.assertRaises(CouncilError):
                    self.talk("This turn fails")
                self.assertEqual(len(self.store.all_conversation_turns("plain")), turns)
                self.assertEqual(self.store.chat_session("plain", "main")["pending"], 1)
                self.control()
                recovered = self.talk("Recover")
                self.assertEqual(recovered["session_mode"], "native-fresh")
                self.assertEqual(recovered["session_rotated"], "previous turn did not commit to the ledger")
        self.assertEqual(self.store.listing(), [])
        with mock.patch("council.conversation.agent_call", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.talk("Interrupted by Ctrl-C")
        self.assertEqual(self.store.chat_session("plain", "main")["pending"], 1)
        self.assertEqual(list((self.store.home / "chat-cwd").iterdir()), [])
        self.assertEqual(self.talk("After the crash")["session_mode"], "native-fresh")

    def test_stale_resume_retries_once_with_a_fresh_session(self):
        self.talk("First question")
        old = self.store.chat_session("plain", "main")["session_id"]
        self.control(fail="resume")
        result = self.talk("Second question")
        failed, retried = self.calls()[-2:]
        self.assertEqual(self.flag(failed, "--resume"), old)
        self.assertIn("--session-id", retried["argv"])
        self.assertEqual(len(self.packet(retried)["recent_conversation"]), 1)
        self.assertEqual((result["session_mode"], result["session_rotated"]),
                         ("native-fresh", "previous native session could not be resumed"))
        binding = self.store.chat_session("plain", "main")
        self.assertNotEqual(binding["session_id"], old)
        self.assertEqual((binding["session_turns"], binding["pending"]), (1, 0))

    def test_timeouts_and_interruptions_are_not_retried(self):
        self.talk("First question")
        self.control(fail="sleep")
        self.project["timeout_seconds"] = 1
        count = len(self.calls())
        with self.assertRaisesRegex(CouncilError, "exceeded"):
            self.talk("Slow question")
        self.assertEqual(len(self.calls()), count + 1)
        self.assertEqual(self.store.chat_session("plain", "main")["pending"], 1)
        self.control()
        self.project["timeout_seconds"] = 10
        self.talk("Second question")
        with mock.patch("council.conversation.agent_call", side_effect=Interrupted("Owner requested pause")) as call:
            with self.assertRaises(Interrupted):
                self.talk("Interrupted question")
        self.assertEqual(call.call_count, 1)
        self.assertTrue(call.call_args.kwargs["session"]["resume"])

    def test_turn_limit_and_automatic_compaction_rotate(self):
        self.write_roster(extra="chat_session_turns = 2")
        self.assertEqual([self.talk(str(n))["session_mode"] for n in range(3)],
                         ["native-fresh", "native-resume", "native-fresh"])
        self.assertEqual(self.store.chat_metrics("plain", "main")[-1]["rotation_reason"], "session turn limit reached")
        self.write_roster()
        for number in range(30):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        with self.store.db:
            self.store.db.execute("UPDATE chat_sessions SET last_turn_id=?", (self.store.last_turn_id("plain", "main"),))
        result = self.talk("After many turns")
        self.assertEqual(result["compactions"][0]["kind"], "turns")
        self.assertEqual(result["session_rotated"], "conversation was compacted this turn")
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 34)
        self.assertEqual(self.store.conversation("plain")["summaries"][0]["summary"], "Older discussion")

    def test_previous_turn_context_usage_rotates_at_the_threshold(self):
        self.write_roster(extra="chat_session_context_tokens = 1000")
        # Output tokens are not counted: 400 + 500 + 99 stays under the threshold.
        self.control(usage={"input_tokens": 400, "output_tokens": 5000,
                            "cache_read_input_tokens": 500, "cache_creation_input_tokens": 99})
        self.assertEqual([self.talk(str(n))["session_mode"] for n in range(2)], ["native-fresh", "native-resume"])
        self.control(usage={"input_tokens": 1, "cache_read_input_tokens": 999, "cache_creation_input_tokens": 0})
        under = self.talk("Still under")
        self.assertEqual((under["session_mode"], under["session_rotated"]), ("native-resume", None))
        self.assertIn("--resume", self.calls()[-1]["argv"])
        over = self.talk("After a large turn")
        reason = "provider context usage reached chat_session_context_tokens (1000 >= 1000)"
        self.assertEqual((over["session_mode"], over["session_rotated"]), ("native-fresh", reason))
        call = self.calls()[-1]
        self.assertIn("--session-id", call["argv"])
        self.assertNotIn("--resume", call["argv"])
        self.assertEqual(len(self.packet(call)["recent_conversation"]), 3)
        self.assertEqual(self.store.chat_metrics("plain", "main")[-1]["rotation_reason"], reason)
        self.assertEqual(self.store.chat_session("plain", "main")["session_turns"], 1)

    def test_partial_usage_counts_the_reported_figures(self):
        self.write_roster(extra="chat_session_context_tokens = 1000")
        self.control(usage={"cache_read_input_tokens": 1500})
        self.talk("First question")
        latest = self.store.chat_metrics("plain", "main")[-1]
        self.assertIsNone(latest["input_tokens"])
        self.assertEqual(self.store.chat_turn_usage("plain", "main", latest["turn_id"]), 1500)
        result = self.talk("Second question")
        self.assertEqual(result["session_mode"], "native-fresh")
        self.assertIn("(1500 >= 1000)", result["session_rotated"])

    def test_missing_usage_falls_back_to_the_turn_limit(self):
        self.write_roster(extra="chat_session_turns = 3\nchat_session_context_tokens = 1000")
        self.control(no_usage=True)
        self.assertEqual([self.talk(str(n))["session_mode"] for n in range(4)],
                         ["native-fresh", "native-resume", "native-resume", "native-fresh"])
        self.assertEqual(self.store.chat_metrics("plain", "main")[-1]["rotation_reason"], "session turn limit reached")
        self.assertIsNone(self.store.chat_turn_usage("plain", "main", self.store.last_turn_id("plain", "main")))

    def test_context_token_threshold_is_validated(self):
        for valid in ("1000", "2000000"):
            self.write_roster(extra=f"chat_session_context_tokens = {valid}")
            self.assertEqual(coordinator_assignment(self.roster)["chat_session_context_tokens"], int(valid))
        for invalid in ("0", "999", "2000001", "true", "1500.5", '"100000"'):
            with self.subTest(value=invalid):
                self.write_roster(extra=f"chat_session_context_tokens = {invalid}")
                with self.assertRaisesRegex(CouncilError, "chat_session_context_tokens must be an integer"):
                    coordinator_assignment(self.roster)
                with self.assertRaisesRegex(CouncilError, "chat_session_context_tokens"):
                    self.talk("Question")
        self.assertEqual(self.calls(), [])

    def test_separate_compaction_starts_a_fresh_session_with_the_new_summaries(self):
        self.talk("First question")
        for number in range(30):
            self.store.append_exchange("plain", "main", str(number), "answer", "memory", "reply")
        with self.store.db:
            self.store.db.execute("UPDATE chat_sessions SET last_turn_id=?", (self.store.last_turn_id("plain", "main"),))
        # The `council compact` command runs this under the conversation lock.
        with self.store.conversation_lock("plain", "main"):
            compact_one(self.store, "plain", "main", coordinator_assignment(self.roster), 10)
        self.assertIsNone(self.store.chat_session("plain", "main"))
        result = self.talk("After compaction")
        self.assertEqual(result["compactions"], [])
        self.assertEqual(result["session_mode"], "native-fresh")
        call = self.calls()[-1]
        self.assertIn("--session-id", call["argv"])
        self.assertEqual(self.packet(call)["older_summaries"][0]["summary"], "Older discussion")
        self.assertEqual(self.talk("Next question")["session_mode"], "native-resume")

    def test_new_session_keeps_history_and_missing_usage_is_null(self):
        self.talk("First question")
        self.talk("Second question")
        result = self.talk("Third question", new_session=True)
        self.assertEqual(result["session_rotated"], "owner requested a new session")
        self.assertEqual(len(self.packet(self.calls()[-1])["recent_conversation"]), 2)
        self.assertEqual(len(self.store.all_conversation_turns("plain")), 3)
        self.control(no_usage=True)
        self.talk("Fourth question")
        latest = self.store.chat_metrics("plain", "main")[-1]
        self.assertEqual(latest["mode"], "native-resume")
        self.assertIsNone(latest["input_tokens"])
        self.assertIsNone(latest["cache_read_tokens"])
        self.assertIsNotNone(latest["wall_ms"])

    def test_threads_have_separate_sessions_and_overlapping_turns_are_refused(self):
        self.talk("Main question", thread="main")
        self.talk("Other question", thread="other")
        main, other = self.calls()
        self.assertNotEqual(self.flag(main, "--session-id"), self.flag(other, "--session-id"))
        self.assertNotEqual(main["cwd"], other["cwd"])
        with self.store.conversation_lock("plain", "main"):
            with self.assertRaisesRegex(CouncilError, "already active"):
                self.talk("Overlapping question")
        self.assertEqual(len(self.calls()), 2)

    def test_version_three_ledger_reads_and_migrates(self):
        home = self.root / "v3"
        store = Store(home)
        store.append_exchange("plain", "main", "Question", "Answer", "memory", "reply")
        store.close()
        db = sqlite3.connect(home / "council.sqlite3")
        db.executescript("DROP TABLE chat_sessions; DROP TABLE chat_metrics; PRAGMA user_version=3;")
        db.close()
        reader = Store(home, read_only=True)
        try:
            self.assertIsNone(reader.chat_session("plain", "main"))
            self.assertEqual(reader.chat_metrics("plain", "main"), [])
            self.assertIsNone(reader.chat_turn_usage("plain", "main", 1))
            self.assertEqual(len(reader.conversation("plain")["turns"]), 1)
        finally:
            reader.close()
        store = Store(home)
        try:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(store.all_conversation_turns("plain")[0]["assistant_text"], "Answer")
            self.assertIsNone(store.chat_session("plain", "main"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
