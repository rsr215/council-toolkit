import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from council.__main__ import BUNDLED_ROSTER
from council.workflow import load_toml

ROOT = Path(__file__).resolve().parent.parent

FAKE = '''import json, sys
model, schema, log = sys.argv[1], sys.argv[2], sys.argv[3]
sys.stdin.read()
with open(log, "a") as handle:
    handle.write(model + "\\n")
outcome = json.load(open(schema))["properties"]["outcome"]["enum"][0]
print(json.dumps({"outcome": outcome, "summary": "ok", "report": "ok", "question": ""}))
'''


class ProjectRosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE)
        self.log = self.root / "models.log"
        self.base = [sys.executable, "-m", "council", "--home", str(self.home)]

    def tearDown(self):
        self.temp.cleanup()

    def council(self, *args, cwd=None, council_roster=None):
        # Ignore the developer's own roster variables; run from any directory.
        env = {k: v for k, v in os.environ.items() if k not in {"COUNCIL_ROSTER", "COUNCIL_HOME"}}
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
        if council_roster is not None:
            env["COUNCIL_ROSTER"] = str(council_roster)
        return subprocess.run([*self.base, *args], capture_output=True, text=True,
                              cwd=cwd or self.elsewhere, env=env)

    def write_roster(self, path: Path, reviewer: str, coordinator: str = "coord"):
        path.parent.mkdir(parents=True, exist_ok=True)
        argv = json.dumps([sys.executable, str(self.fake), "{model}", "{schema}", str(self.log)])
        path.write_text(f'[executors.fake]\nkind = "command"\ntrusted = true\nargv = {argv}\n\n'
                        f'[workflow_roles.coordinator]\nexecutor = "fake"\nmodel = {json.dumps(coordinator)}\n\n'
                        '[workflow_roles.implementer]\nexecutor = "fake"\nmodel = "implement"\n\n'
                        f'[workflow_roles.reviewer]\nexecutor = "fake"\nmodel = {json.dumps(reviewer)}\n')
        return path

    def project(self, name: str, roster: str | None = None) -> Path:
        workspace = self.root / name
        workspace.mkdir()
        (workspace / "notes.md").write_text(f"{name} notes")
        result = self.council("init", str(workspace), "--name", name, "--files")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.home / "projects" / f"{name}.toml"
        text = path.read_text().replace("include = []", 'include = ["*.md"]')
        if roster is not None:
            text += f"roster = {json.dumps(roster)}\n"
        path.write_text(text)
        return path

    def reviewer_model(self, project: str, pre=(), **kwargs) -> str:
        result = self.council(*pre, "review", project, "Review notes", "--plan-only", "--json", **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["data"]["roster"]["reviewer"]["model"]

    def test_two_projects_use_their_own_relative_rosters(self):
        self.home.mkdir(mode=0o700)
        self.write_roster(self.home / "roster.toml", "personal")
        alpha = self.project("alpha", "alpha.roster.toml")
        self.project("beta", "beta.roster.toml")
        self.write_roster(self.home / "projects/alpha.roster.toml", "alpha-model")
        self.write_roster(self.home / "projects/beta.roster.toml", "beta-model")
        self.assertEqual(self.reviewer_model("alpha"), "alpha-model")
        self.assertEqual(self.reviewer_model("beta"), "beta-model")
        self.assertEqual(self.reviewer_model(str(alpha)), "alpha-model")
        doctor = self.council("doctor", "beta")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn(f"Roster: {self.home / 'projects/beta.roster.toml'} (project beta)", doctor.stdout)

    def test_relative_roster_resolves_against_project_toml_not_current_directory(self):
        (self.root / "gamma").mkdir()
        (self.root / "gamma/notes.md").write_text("notes")
        config = self.root / "configs/gamma.toml"
        config.parent.mkdir()
        config.write_text(f'version = 1\nname = "gamma"\nworkspace = {json.dumps(str(self.root / "gamma"))}\n'
                          'git = false\ninclude = ["*.md"]\nroster = "rosters/gamma.toml"\n')
        self.write_roster(self.root / "configs/rosters/gamma.toml", "beside-config")
        self.write_roster(self.elsewhere / "rosters/gamma.toml", "current-directory")
        self.assertEqual(self.reviewer_model(str(config), cwd=self.elsewhere), "beside-config")

    def test_roster_precedence_one_level_at_a_time(self):
        self.home.mkdir(mode=0o700)
        path = self.project("alpha", "alpha.roster.toml")
        self.write_roster(self.home / "projects/alpha.roster.toml", "project")
        personal = self.write_roster(self.home / "roster.toml", "personal")
        environment = self.write_roster(self.root / "env.toml", "environment")
        explicit = self.write_roster(self.root / "explicit.toml", "explicit")
        self.assertEqual(self.reviewer_model("alpha", pre=["--roster", str(explicit)], council_roster=environment), "explicit")
        self.assertEqual(self.reviewer_model("alpha", council_roster=environment), "environment")
        self.assertEqual(self.reviewer_model("alpha"), "project")
        path.write_text(path.read_text().replace('\nroster = "alpha.roster.toml"\n', "\n"))
        self.assertEqual(self.reviewer_model("alpha"), "personal")
        personal.unlink()
        bundled = load_toml(BUNDLED_ROSTER)["workflow_roles"]["reviewer"]["model"]
        self.assertEqual(self.reviewer_model("alpha"), bundled)
        doctor = self.council("doctor", "alpha")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn(f"Roster: {BUNDLED_ROSTER} (bundled)", doctor.stdout)

    def test_missing_project_roster_fails_closed(self):
        self.home.mkdir(mode=0o700)
        self.write_roster(self.home / "roster.toml", "personal")
        self.project("alpha", "missing.roster.toml")
        missing = str(self.home / "projects/missing.roster.toml")
        for args in (["doctor", "alpha"], ["review", "alpha", "Review", "--plan-only"],
                     ["run", "alpha", "Edit", "--plan-only", "--workspace", str(self.root / "alpha")],
                     ["chat", "alpha", "Hello"], ["compact", "alpha"]):
            result = self.council(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("Project roster not found: " + missing, result.stderr, args)
        self.assertEqual(self.council("chat", "alpha", "--history").returncode, 0)
        self.assertEqual(self.council("compact", "alpha", "--dry-run").returncode, 0)
        override = self.write_roster(self.root / "override.toml", "override")
        self.assertEqual(self.reviewer_model("alpha", pre=["--roster", str(override)]), "override")
        self.assertEqual(self.reviewer_model("alpha", council_roster=override), "override")

    def test_invalid_roster_setting_is_rejected(self):
        path = self.project("alpha")
        original = path.read_text()
        for value in ("3", '""'):
            path.write_text(original + f"roster = {value}\n")
            result = self.council("doctor", "alpha")
            self.assertEqual(result.returncode, 2)
            self.assertIn("roster must be a nonempty path", result.stderr)

    def test_malformed_roster_entries_give_clear_errors(self):
        self.project("alpha", "alpha.roster.toml")
        roster = self.home / "projects/alpha.roster.toml"
        cases = (('[executors.fake]\nkind = "command"\n\n[workflow_roles]\nreviewer = 3\n', "workflow_roles.reviewer"),
                 ('[executors]\nfake = "command"\n\n[workflow_roles.reviewer]\nexecutor = "fake"\nmodel = "m"\n',
                  "executors.fake"),
                 ('workflow_roles = 3\n', "workflow_roles must be a table"))
        for text, key in cases:
            roster.write_text(text)
            for args in (["doctor", "alpha"], ["review", "alpha", "Review", "--plan-only"],
                         ["assign", "reviewer", "--project", "alpha", "--executor", "fake", "--model", "m"]):
                result = self.council(*args)
                self.assertEqual(result.returncode, 2, (args, result.stderr))
                self.assertNotIn("Traceback", result.stderr, args)
                self.assertIn(f"{roster}: {key}", result.stderr, args)
            self.assertEqual(roster.read_text(), text)

    def test_queued_task_keeps_saved_roster_after_edit_and_deletion(self):
        self.project("alpha", "alpha.roster.toml")
        roster = self.write_roster(self.home / "projects/alpha.roster.toml", "original", "coord-original")
        queued = self.council("review", "alpha", "Review notes", "--plan-only", "--json")
        self.assertEqual(queued.returncode, 0, queued.stderr)
        task_id = json.loads(queued.stdout)["id"]
        edited = self.council("assign", "reviewer", "--project", "alpha", "--executor", "fake", "--model", "changed")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        self.assertEqual(load_toml(roster)["workflow_roles"]["reviewer"]["model"], "changed")
        roster.unlink()
        result = self.council("work", task_id, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        task = json.loads(result.stdout)
        self.assertEqual(task["status"], "completed", task["data"]["summary"])
        self.assertEqual(task["data"]["roster"]["reviewer"]["model"], "original")
        self.assertEqual(self.log.read_text().split(), ["original", "coord-original"])

    def test_assign_project_edits_only_the_declared_roster(self):
        self.home.mkdir(mode=0o700)
        personal = self.write_roster(self.home / "roster.toml", "personal")
        path = self.project("alpha")
        bundled_before, personal_before = BUNDLED_ROSTER.read_bytes(), personal.read_bytes()
        assign = ["assign", "reviewer", "--project", "alpha", "--executor", "fake", "--model", "project-only"]
        result = self.council(*assign)
        self.assertEqual(result.returncode, 2)
        self.assertIn("declares no roster", result.stderr)
        path.write_text(path.read_text() + 'roster = "alpha.roster.toml"\n')
        project_before = path.read_bytes()
        result = self.council("--roster", str(personal), *assign)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not both", result.stderr)
        result = self.council(*assign)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Created", result.stdout)
        created = load_toml(self.home / "projects/alpha.roster.toml")
        self.assertEqual(created["workflow_roles"]["reviewer"]["model"], "project-only")
        self.assertEqual(created["workflow_roles"]["coordinator"]["model"], "coord")
        self.assertEqual(path.read_bytes(), project_before)
        self.assertEqual(personal.read_bytes(), personal_before)
        self.assertEqual(BUNDLED_ROSTER.read_bytes(), bundled_before)
        result = self.council(*assign, council_roster=personal)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COUNCIL_ROSTER is set", result.stderr)
        path.write_text(path.read_text().replace('\nroster = "alpha.roster.toml"',
                                                 "\nroster = " + json.dumps(str(BUNDLED_ROSTER))))
        result = self.council("assign", "reviewer", "--project", "alpha", "--executor", "codex", "--model", "x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not an editable roster", result.stderr)
        self.assertEqual(BUNDLED_ROSTER.read_bytes(), bundled_before)

    def test_assign_project_keeps_roster_permissions(self):
        self.project("alpha", "alpha.roster.toml")
        roster = self.write_roster(self.home / "projects/alpha.roster.toml", "original")
        for mode in (0o600, 0o644):
            roster.chmod(mode)
            model = f"mode-{mode:o}"
            result = self.council("assign", "reviewer", "--project", "alpha", "--executor", "fake", "--model", model)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(roster.stat().st_mode & 0o777, mode)
            self.assertEqual(load_toml(roster)["workflow_roles"]["reviewer"]["model"], model)
        self.assertFalse(roster.with_name(roster.name + ".tmp").exists())

    def test_assign_project_refuses_symlink_to_personal_roster(self):
        self.home.mkdir(mode=0o700)
        personal = self.write_roster(self.home / "roster.toml", "personal")
        alpha = self.project("alpha", "alpha.roster.toml")
        link = self.home / "projects/alpha.roster.toml"
        link.symlink_to(personal)

        def refused(project, owner, shared, *others):
            files = (shared, link, alpha, *others)
            state = lambda: [(f.is_symlink(), f.read_bytes() if f.exists() else None) for f in files]
            before = state()
            result = self.council("assign", "reviewer", "--project", project, "--executor", "fake", "--model", "changed")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn(f"shared with {owner} ({shared})", result.stderr)
            self.assertEqual(state(), before)
            for f in files:
                self.assertFalse(f.with_name(f.name + ".tmp").exists(), f)

        with self.subTest("symlink to the personal roster"):
            refused("alpha", "the personal roster", personal)
            self.assertTrue(link.is_symlink())
        with self.subTest("dangling symlink creates no personal roster"):
            original = personal.read_bytes()
            personal.unlink()
            refused("alpha", "the personal roster", personal)
            self.assertFalse(personal.exists())
            personal.write_bytes(original)
        with self.subTest("relative path to the personal roster"):
            alpha.write_text(alpha.read_text().replace('roster = "alpha.roster.toml"', 'roster = "../roster.toml"'))
            refused("alpha", "the personal roster", personal)
        with self.subTest("another project's roster"):
            link.unlink()
            own = self.write_roster(link, "alpha-model")
            alpha.write_text(alpha.read_text().replace('roster = "../roster.toml"', 'roster = "alpha.roster.toml"'))
            beta = self.project("beta", "alpha.roster.toml")
            refused("beta", "project alpha", own, beta)
        with self.subTest("hard link to the bundled roster"):
            gamma = self.project("gamma", "gamma.roster.toml")
            hard = self.home / "projects/gamma.roster.toml"
            try:
                os.link(BUNDLED_ROSTER, hard)
            except OSError as exc:
                self.skipTest(f"cannot hard-link the bundled roster here: {exc}")
            refused("gamma", "the bundled roster", BUNDLED_ROSTER, gamma, hard)
            self.assertTrue(os.path.samefile(hard, BUNDLED_ROSTER))
        with self.subTest("explicit --roster still edits a shared file"):
            result = self.council("--roster", str(personal), "assign", "reviewer", "--executor", "fake", "--model", "changed")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_toml(personal)["workflow_roles"]["reviewer"]["model"], "changed")


if __name__ == "__main__":
    unittest.main()
