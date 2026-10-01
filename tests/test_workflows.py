from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from council.demo import demo_roster
from council.runner import decide, drive, execute, resume
from council.store import Store
from council.workflow import CouncilError, fingerprint, snapshot, workflow_config


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "project"
        self.work.mkdir()
        (self.work / "brief.txt").write_text("initial\n")
        self.project = {"version": 1, "name": "test", "workspace": str(self.work), "git": False,
                        "include": ["*.txt"], "checks": [[sys.executable, "-c", "print('verified')"]],
                        "timeout_seconds": 10}
        self.cfg = workflow_config(Path(__file__).resolve().parents[1] / "council/workflows/delivery.toml")
        self.store = Store(self.root / "state")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, behavior="repair"):
        revision, files = fingerprint(self.project)
        return self.store.create(self.project, self.cfg, demo_roster(behavior), "Approved objective", revision, files)

    def drive(self, task_id):
        return drive(self.store, task_id, notify=lambda text: None)

    def test_review_repair_auto_completes(self):
        task_id = self.create()
        task = self.drive(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["data"]["visits"]["implement"], 2)
        self.assertIsNone(task["data"].get("gate"))

    def test_owner_question_survives_restart(self):
        task_id = self.create("ask")
        task = self.drive(task_id)
        self.assertEqual(task["data"]["visits"], {"plan": 1})
        request = task["data"]["gate"]["id"]
        self.store.close()
        self.store = Store(self.root / "state")
        with self.assertRaises(CouncilError):
            decide(self.store, task_id, request, "respond", "")
        decide(self.store, task_id, request, "respond", "Use the approved small design")
        self.assertEqual(self.drive(task_id)["status"], "completed")

    def test_stale_owner_decision_rejected_and_review_invalidated(self):
        task_id = self.create("ask")
        task = self.drive(task_id)
        (self.work / "brief.txt").write_text("changed after decision request\n")
        with self.assertRaises(CouncilError):
            decide(self.store, task_id, task["data"]["gate"]["id"], "respond", "Use the approved small design")
        resume(self.store, task_id, "Inspected the manual change; rerun validation")
        task = self.store.get(task_id)
        self.assertEqual(task["phase"], "plan")
        self.assertEqual(task["data"]["outputs"], {})

    def test_malformed_response_does_not_advance(self):
        task_id = self.create("malformed")
        task = self.drive(task_id)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["phase"], "plan")
        self.assertEqual(task["data"]["steps"], 1)

    def test_crashed_worker_not_replayed(self):
        task_id = self.create()
        task = self.store.get(task_id)
        task["status"] = "running"
        self.store.save(task, "started", "Simulated crash")
        task = self.drive(task_id)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["data"]["steps"], 0)

    def test_failed_checks_cannot_be_overridden_by_model(self):
        self.project["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        task = self.drive(self.create("ignore_failure"))
        self.assertEqual(task["status"], "interrupted")
        self.assertIn("Required evidence", task["data"]["summary"])

    def test_repair_loop_budget(self):
        self.project["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        task = self.drive(self.create())
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(task["data"]["gate"]["kind"], "limit")
        self.assertEqual(task["data"]["visits"]["implement"], 3)

    def test_protected_change_stops_before_checks(self):
        self.project["protected_paths"] = ["brief.txt"]
        task = self.drive(self.create("normal"))
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(task["data"]["gate"]["kind"], "protected_files")
        self.assertNotIn("verify", task["data"]["outputs"])

    def test_pause_preserves_pending_gate(self):
        task_id = self.create("ask")
        task = self.drive(task_id)
        request = task["data"]["gate"]["id"]
        self.store.control(task_id, "pause")
        resume(self.store, task_id, "Continue with the same pending decision")
        self.assertEqual(self.store.get(task_id)["data"]["gate"]["id"], request)
        self.assertEqual(self.store.get(task_id)["status"], "waiting")

    def test_concurrent_worker_lock(self):
        second = Store(self.root / "state")
        try:
            with self.store.runner_lock(), self.assertRaises(CouncilError):
                with second.runner_lock():
                    self.fail("Must not acquire")
        finally:
            second.close()

    def test_optimistic_revision_rejects_lost_update(self):
        task_id = self.create()
        one, two = self.store.get(task_id), self.store.get(task_id)
        self.store.save(one, "test", "first")
        with self.assertRaises(CouncilError):
            self.store.save(two, "test", "second")

    def test_gc_only_compacts_closed_tasks_and_keeps_decisions(self):
        closed_id = self.create("ask")
        task = self.drive(closed_id)
        decide(self.store, closed_id, task["data"]["gate"]["id"], "respond", "keep this decision")
        self.assertEqual(self.drive(closed_id)["status"], "completed")
        open_id = self.create("ask")
        self.drive(open_id)
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET closed=?", (time.time() - 100 * 86400,))
        self.assertEqual(self.store.gc(30, False), [closed_id])
        self.assertTrue(self.store.get(closed_id)["data"]["outputs"])
        self.store.gc(30, True)
        self.assertEqual(self.store.get(closed_id)["data"]["outputs"], {})
        self.assertEqual(self.store.get(closed_id)["data"]["decisions"][0]["note"], "keep this decision")
        self.assertIsNotNone(self.store.get(open_id)["data"]["gate"])

    def test_snapshot_excludes_mailbox_and_secrets(self):
        (self.work / ".council").mkdir()
        (self.work / ".council" / "private.txt").write_text("private")
        (self.work / ".env").write_text("secret")
        (self.work / ".envrc").write_text("secret")
        (self.work / ".env_local").write_text("secret")
        self.project["include"] = ["**/*"]
        target = self.root / "snapshot"
        target.mkdir()
        snapshot(self.project, target)
        self.assertFalse((target / ".council").exists())
        self.assertFalse((target / ".env").exists())
        self.assertFalse((target / ".envrc").exists())
        self.assertFalse((target / ".env_local").exists())
        self.assertTrue((target / "brief.txt").exists())

    def test_symlink_escape_rejected(self):
        (self.work / "escape.txt").symlink_to(self.root / "outside")
        (self.root / "outside").write_text("private")
        with self.assertRaises(CouncilError):
            fingerprint(self.project)

    def test_cancellation_and_timeout_kill_process(self):
        with self.assertRaises(CouncilError):
            execute([sys.executable, "-c", "import time; time.sleep(10)"], self.work, "", 1, lambda: None)
        with self.assertRaises(CouncilError):
            execute([sys.executable, "-c", "import time; time.sleep(10)"], self.work, "", 5, lambda: "cancel")

    def test_sigterm_cleans_active_child_and_temporary_files(self):
        pid_file = self.root / "child.pid"
        temp_file = self.root / "temporary.path"
        child = ("import os,time,pathlib; "
                 f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                 "time.sleep(30)")
        parent = ("import pathlib,sys,tempfile\n"
                  "from council.__main__ import termination_as_error\n"
                  "from council.runner import execute\n"
                  "with termination_as_error():\n"
                  f" with tempfile.TemporaryDirectory(dir={str(self.root)!r}) as directory:\n"
                  f"  pathlib.Path({str(temp_file)!r}).write_text(directory)\n"
                  f"  execute([sys.executable, '-c', {child!r}], pathlib.Path(directory), '', 30, lambda: None)\n")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        process = subprocess.Popen([sys.executable, "-c", parent], cwd=self.work,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), process.communicate(timeout=2)[1].decode() if process.poll() is not None else "child did not start")
            os.kill(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertFalse(Path(temp_file.read_text()).exists())
            child_pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_generic_workflow_needs_no_code_roles_or_git(self):
        self.cfg = {"version": 1, "entry": "draft", "max_steps": 1,
                    "phases": {"draft": {"kind": "agent", "role": "editor", "read_only": True,
                                            "instructions": "Write an outline", "routes": {"ready": "@done"}}}}
        revision, files = fingerprint(self.project)
        roster = {"editor": demo_roster("normal")["coordinator"]}
        task_id = self.store.create(self.project, self.cfg, roster, "Editorial project", revision, files)
        self.assertEqual(self.drive(task_id)["status"], "completed")

    def test_assignment_snapshot_independent_of_source(self):
        roster = demo_roster()
        revision, files = fingerprint(self.project)
        task_id = self.store.create(self.project, self.cfg, roster, "Approved objective", revision, files)
        roster["coordinator"]["model"] = "different-model"
        self.assertEqual(self.store.get(task_id)["data"]["roster"]["coordinator"]["model"], "explicit-simulation")

    def test_external_changes_before_dispatch_stop_work(self):
        task_id = self.create()
        (self.work / "brief.txt").write_text("external change")
        self.assertEqual(self.drive(task_id)["status"], "interrupted")

    def test_automatic_retention_respects_each_project(self):
        self.project["retention_days"] = 365
        long_id = self.create("normal")
        self.assertEqual(self.drive(long_id)["status"], "completed")
        self.project["retention_days"] = 7
        short_id = self.create("normal")
        self.assertEqual(self.drive(short_id)["status"], "completed")
        with self.store.db:
            self.store.db.execute("UPDATE tasks SET closed=?", (time.time() - 30 * 86400,))
        self.assertEqual(self.store.gc(None, False), [short_id])
        self.store.gc(None, True)
        self.assertTrue(self.store.get(long_id)["data"]["outputs"])
        self.assertTrue(self.store.get(short_id)["data"]["phase_summaries"])


if __name__ == "__main__":
    unittest.main()
