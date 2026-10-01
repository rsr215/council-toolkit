"""Intuitive local control surface; usable by a person, Codex Remote, or a gateway."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import threading

from . import __version__
from .runner import decide, drive, resume, summary
from .store import Store
from .workflow import (CouncilError, chat_session_mode, declared_roster, fingerprint, load_toml,
                       project_config, roster_config, roster_tables, workflow_config, validate_chat_session,
                       validate_effort)


BUNDLED_ROSTER = Path(__file__).resolve().parent / "roster.toml"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="council", description="Run a reusable council. Models, workflows, and project rules are configuration.")
    p.add_argument("--version", action="version", version=f"Council {__version__}")
    p.add_argument("--home", type=Path, default=Path(os.environ.get("COUNCIL_HOME", "~/.local/share/llm-council")).expanduser(), help="local ledger and project registry")
    p.add_argument("--roster", type=Path, help="model and executor configuration; defaults to COUNCIL_ROSTER, the project's roster, a personal roster, or the bundled example")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="register a project without changing its files")
    init.add_argument("workspace", type=Path)
    init.add_argument("--name", required=True)
    init.add_argument("--files", action="store_true", help="non-Git project; configure explicit include patterns")
    init.add_argument("--workflow", choices=["delivery", "research", "review"], default="delivery")
    run = sub.add_parser("run", help="start an approved objective in an explicitly selected workspace")
    run.add_argument("project", help="registered name or project TOML path")
    run.add_argument("objective")
    run.add_argument("--workflow", type=Path, help="override with a workflow TOML path")
    run.add_argument("--plan-only", action="store_true", help="save the task without launching any agent")
    run.add_argument("--workspace", type=Path, help="isolated working copy for this run")
    run.add_argument("--json", action="store_true")
    review = sub.add_parser("review", help="independently review existing project material without implementation")
    review.add_argument("project", help="registered name or project TOML path")
    review.add_argument("objective", help="what the reviewer should inspect")
    review.add_argument("--workspace", type=Path, help="alternate read-only working copy")
    review.add_argument("--plan-only", action="store_true", help="queue without model calls")
    review.add_argument("--json", action="store_true")
    chat = sub.add_parser("chat", help="talk with the project's coordinator; explicit review requests can dispatch a reviewer")
    chat.add_argument("project", help="registered name or project TOML path")
    chat.add_argument("message", nargs="?", help="message to coordinator; omit for an interactive terminal")
    chat.add_argument("--thread", default="main", help="conversation name within this project")
    chat.add_argument("--allow-review", action="store_true", help="allow this owner-authorized chat turn to dispatch an independent review")
    chat.add_argument("--history", action="store_true", help="show recent conversation and summaries without a model call")
    chat.add_argument("--all", action="store_true", help="with --history, include archived full turns")
    chat.add_argument("--new-session", action="store_true", help="start a fresh native coordinator session on this turn; ledger history is kept")
    chat.add_argument("--json", action="store_true")
    compact = sub.add_parser("compact", help="catch up layered conversation summaries without starting a task")
    compact.add_argument("project")
    compact.add_argument("--thread", default="main")
    compact.add_argument("--max-steps", type=int, default=4, help="maximum model summary calls (1-100)")
    compact.add_argument("--dry-run", action="store_true", help="show whether compaction is due without a model call")
    compact.add_argument("--json", action="store_true")
    for command, help_text in [("status", "show active work or one task"), ("summary", "show a task's compact briefing")]:
        item = sub.add_parser(command, help=help_text)
        item.add_argument("id", nargs="?" if command == "status" else None)
        item.add_argument("--json", action="store_true")
    work = sub.add_parser("work", help="continue a queued task to the next owner checkpoint")
    work.add_argument("id")
    work.add_argument("--json", action="store_true")
    decision = sub.add_parser("decide", help="answer a specific pending owner decision")
    decision.add_argument("id")
    decision.add_argument("choice")
    decision.add_argument("--request", required=True, help="request identifier shown by status")
    decision.add_argument("--note", default="")
    decision.add_argument("--continue", dest="continue_work", action="store_true")
    for command in ("pause", "cancel", "resume"):
        item = sub.add_parser(command, help=f"{command} a task")
        item.add_argument("id")
        if command == "resume":
            item.add_argument("--note", required=True, help="confirm how partial changes/processes were reconciled")
    gc = sub.add_parser("gc", help="preview expiration of completed reports; keeps decision receipts")
    gc.add_argument("--days", type=int, default=None, help="override each project's saved retention period")
    gc.add_argument("--apply", action="store_true")
    doctor = sub.add_parser("doctor", help="validate configuration and installed executables without model calls")
    doctor.add_argument("project", nargs="?")
    doctor.add_argument("--auth", action="store_true", help="query Codex/Claude sign-in; show manual validation guidance for Copilot/Gemini")
    assign = sub.add_parser("assign", help="change a role's executor and model for new runs")
    assign.add_argument("role")
    assign.add_argument("--executor", required=True)
    assign.add_argument("--model", required=True)
    assign.add_argument("--effort", help="explicit effort; omitted preserves effort for the same executor")
    assign.add_argument("--project", help="edit the roster declared by this registered name or project TOML")
    imported = sub.add_parser("import-context", help="save historical handoffs without launching work")
    imported.add_argument("project")
    imported.add_argument("source", type=Path, help="directory containing Markdown handoffs")
    imported.add_argument("--summary", type=Path, required=True, help="reconciled context; distinguish facts from old claims")
    context = sub.add_parser("context", help="read imported project context or an archived source")
    context.add_argument("project")
    context.add_argument("--source", help="relative source path printed in the context inventory")
    context.add_argument("--json", action="store_true")
    report = sub.add_parser("report", help="read the detailed evidence for a task")
    report.add_argument("id")
    report.add_argument("--phase")
    sub.add_parser("demo", help="exercise a temporary simulated council; no accounts or API calls")
    return p


def resolve_project(home: Path, name: str) -> Path:
    explicit = Path(name).expanduser()
    if explicit.is_file():
        return explicit.resolve()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise CouncilError("Project must be a registered name or existing TOML path")
    return home / "projects" / f"{name}.toml"


def initialize(args) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", args.name):
        raise CouncilError("Project name must use letters, numbers, hyphens or underscores")
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise CouncilError("Workspace does not exist")
    args.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.home.stat().st_mode & 0o077:
        raise CouncilError(f"Council home is accessible to other users: {args.home}. Restrict it with chmod 700 before continuing")
    target = args.home / "projects" / f"{args.name}.toml"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workflow = Path(__file__).parent / "workflows" / f"{args.workflow}.toml"
    contents = (f"version = 1\nname = {json.dumps(args.name)}\nworkspace = {json.dumps(str(workspace))}\n"
                f"git = {'false' if args.files else 'true'}\nworkflow = {json.dumps(str(workflow))}\n"
                '# Optional roster for this project only, relative to this file. Omitted = COUNCIL_HOME/roster.toml or the bundled roster.\n'
                f'# roster = {json.dumps(args.name + ".roster.toml")}\n'
                'timeout_seconds = 900\nmax_snapshot_bytes = 100000000\nretention_days = 30\nallow_in_place = false\n'
                '# Used only for non-Git projects; list only approved task files.\ninclude = []\n'
                '# Checked by the runner after edits; protected changes require owner disposition.\n'
                'protected_paths = []\n'
                '# Commands are argv arrays; they execute only when a check phase runs.\nchecks = []\n'
                'rules = "State your project requirements and owner-only decisions here."\n')
    try:
        with target.open("x") as handle:
            handle.write(contents)
    except FileExistsError as exc:
        raise CouncilError(f"Project already exists: {target}") from exc
    return target


def select_roster(args, project: dict | None = None) -> tuple[Path, str]:
    """--roster, then COUNCIL_ROSTER, then the project's roster, then the personal roster, then the bundled roster."""
    if args.roster is not None:
        return args.roster, "--roster"
    configured = os.environ.get("COUNCIL_ROSTER")
    if configured:
        return Path(configured).expanduser(), "COUNCIL_ROSTER"
    if project and project.get("roster"):
        path = Path(project["roster"])
        if not path.is_file():
            # A declared roster never falls back to another roster silently.
            raise CouncilError(f"Project roster not found: {path} (set in {project['config_path']}). "
                               "Create it, fix the path, or remove the setting")
        return path, f"project {project['name']}"
    personal = args.home / "roster.toml"
    if personal.exists():
        return personal, "personal"
    return BUNDLED_ROSTER, "bundled"


def refuse_shared_roster(args, project: dict, target: Path):
    """assign --project never edits a roster that a default or another registered project also uses."""
    shared = [(args.home / "roster.toml", "the personal roster"), (BUNDLED_ROSTER, "the bundled roster")]
    if os.environ.get("COUNCIL_ROSTER"):
        shared.append((Path(os.environ["COUNCIL_ROSTER"]).expanduser(), "COUNCIL_ROSTER"))
    for config in sorted((args.home / "projects").glob("*.toml")):
        # Only registered project names; roster files such as NAME.roster.toml live here too.
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", config.stem) or config.resolve() == Path(project["config_path"]):
            continue
        try:
            other = declared_roster(config, load_toml(config))
        except CouncilError as exc:
            raise CouncilError(f"Cannot check whether {target} is shared; fix {config} first: {exc}") from exc
        if other:
            shared.append((Path(other), f"project {config.stem}"))
    for path, owner in shared:
        path = path.resolve()
        # Resolved paths catch symlinks, including dangling ones; samefile catches hard links.
        if target == path or (target.exists() and path.exists() and os.path.samefile(target, path)):
            raise CouncilError(f"Project roster {target} is shared with {owner} ({path}), so this edit would change other runs too. "
                               f"Point roster at a separate file, or edit the shared file on purpose with council --roster {path} assign ...")


def doctor(args):
    project = project_config(resolve_project(args.home, args.project)) if args.project else None
    roster_path, source = select_roster(args, project)
    print(f"Roster: {roster_path} ({source})")
    roster = load_toml(roster_path)
    roster_tables(roster, roster_path)
    checked = set()
    for name, role in roster.get("workflow_roles", {}).items():
        runtime = roster.get("executors", {}).get(role.get("executor"), {})
        binary = runtime.get("binary") or (runtime.get("argv") or [runtime.get("kind", "").replace("-cli", "")])[0]
        available = bool(binary and shutil.which(binary))
        print(f"{name}: {role.get('model', '(missing model)')} via {role.get('executor', '(missing executor)')} · effort={role.get('effort', 'inherited')} · {'installed' if available else 'not found'}")
        kind = runtime.get("kind")
        if name == "coordinator" or "chat_session" in role:
            validate_chat_session(role)
            mode = chat_session_mode(role, runtime)
            print("  Chat session: " + {
                "native": f"native; resumes up to {role.get('chat_session_turns', 20)} turns or until the previous turn "
                          f"reported >= {role.get('chat_session_context_tokens', 100_000)} input/cache tokens, then starts fresh",
                "ledger": "ledger context sent every turn",
                "fallback": f"native requested; {kind} unsupported, ledger fallback"}[mode])
        if args.auth and kind == "antigravity-cli":
            print("  Sign-in: run agy and choose Google; availability requires a live research check.")
        if args.auth and kind == "copilot-cli":
            print("  Sign-in: check Copilot CLI and organization policy with a live run.")
        if args.auth and available and binary not in checked and kind in {"codex-cli", "claude-cli"}:
            checked.add(binary)
            argv = [binary, "login", "status"] if kind == "codex-cli" else [binary, "auth", "status"]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            authenticated = result.returncode == 0
            if kind == "claude-cli":
                try:
                    authenticated = json.loads(result.stdout).get("loggedIn") is True
                except ValueError:
                    authenticated = False
            print(f"  Sign-in: {'confirmed' if authenticated else 'not confirmed; authenticate in your terminal'}")
    if project:
        workflow = workflow_config(Path(project["workflow"]))
        roster_config(roster_path, workflow)
        revision, files = fingerprint(project)
        print(f"Project: {project['name']} · {len(files)} files · revision {revision}")
        if any(p["kind"] == "check" for p in workflow["phases"].values()) and not project.get("checks"):
            print("Setup needed: configure checks before running the delivery workflow.")
    print("No agents launched. Model availability and billing have not been tested.")


def assign_role(path: Path, role: str, executor: str, model: str, effort: str | None = None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", role) or not model.strip():
        raise CouncilError("Use a simple role name and a nonempty model identifier")
    raw = load_toml(path)
    roster_tables(raw, path)
    if executor not in raw.get("executors", {}):
        raise CouncilError(f"Configure executor {executor!r} in {path} first")
    assignment = dict(raw.get("workflow_roles", {}).get(role, {}))
    if assignment.get("executor") != executor:
        assignment.pop("effort", None)
    assignment.update(executor=executor, model=model)
    if effort is not None:
        assignment["effort"] = effort
    validate_effort(assignment, raw["executors"][executor])
    text = path.read_text()
    pattern = rf"(?ms)^\[workflow_roles\.{re.escape(role)}\][^\n]*\n.*?(?=^\[|\Z)"
    owned_fields = {key: assignment[key] for key in ("executor", "model", "effort") if key in assignment}

    def rewrite_section(match):
        # Preserve optional TOML values and adjacent sub-tables verbatim.
        section = match.group(0)
        for key in ("executor", "model", "effort"):
            line_pattern = rf'(?m)^[ \t]*(?:"{key}"|{key})[ \t]*=.*(?:\n|$)'
            if key not in owned_fields:
                section = re.sub(line_pattern, "", section, count=1)
                continue
            replacement = f"{key} = {json.dumps(owned_fields[key])}\n"
            section, count = re.subn(line_pattern, lambda _: replacement, section, count=1)
            if not count:
                section = section.rstrip("\n") + "\n" + replacement
        return section

    updated, count = re.subn(pattern, rewrite_section, text)
    if not count:
        if role in raw.get("workflow_roles", {}):
            raise CouncilError("This role uses a custom TOML layout; edit its assignment directly")
        section = f"[workflow_roles.{role}]\n" + "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in owned_fields.items())
        updated = text.rstrip() + "\n\n" + section
    import tomllib
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise CouncilError("Role assignment would make the roster invalid; original file was preserved") from exc
    # Keep the roster's permissions exactly; the replacement is never more open while written.
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_name(path.name + ".tmp")
    owned = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        owned = True
        with os.fdopen(descriptor, "w") as handle:
            handle.write(updated)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if owned and temporary.exists():
            temporary.unlink()


@contextmanager
def termination_as_error():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def handle_termination(_signum, _frame):
        raise CouncilError("Termination requested; inspect task and workspace before resuming")

    signal.signal(signal.SIGTERM, handle_termination)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _main(argv=None) -> int:
    args = parser().parse_args(argv)
    args.home = args.home.expanduser()
    if args.roster is not None:
        args.roster = args.roster.expanduser()
    try:
        if args.command == "demo":
            from .demo import run_demo
            return run_demo()
        if args.command == "init":
            path = initialize(args)
            print(f"Registered {args.name}. Project files were not changed.\nConfigure rules, file includes if needed, and delivery checks: {path}\n"
                  f"Then: council doctor {args.name}\nTalk: council chat {args.name} 'Your question'\n"
                  f"Separate models for this project: uncomment roster in that file, then council assign ROLE --project {args.name} ...")
            return 0
        if args.command == "doctor":
            doctor(args)
            return 0
        if args.command == "assign":
            if args.project:
                if args.roster is not None:
                    raise CouncilError("Use either --roster or --project with assign, not both")
                project = project_config(resolve_project(args.home, args.project))
                if not project.get("roster"):
                    raise CouncilError(f"Project {project['name']} declares no roster. Add "
                                       f"roster = {json.dumps(project['name'] + '.roster.toml')} to {project['config_path']}, then retry")
                target = Path(project["roster"])
                if target == BUNDLED_ROSTER or target == Path(project["config_path"]):
                    raise CouncilError(f"Project roster {target} is not an editable roster; point roster at a separate file you own")
                refuse_shared_roster(args, project, target)
                base = None
                if not target.exists():
                    if not target.parent.is_dir():
                        raise CouncilError(f"Folder for the project roster does not exist: {target.parent}")
                    # Start from the roster this project would otherwise use, as the personal copy does.
                    personal = args.home / "roster.toml"
                    base = personal if personal.exists() else BUNDLED_ROSTER
                    shutil.copy2(base, target)
                try:
                    assign_role(target, args.role, args.executor, args.model, args.effort)
                except BaseException:
                    if base is not None:
                        target.unlink()
                    raise
                if base is not None:
                    print(f"Created {target} from {base}.")
                print(f"{args.role} now uses {args.model} via {args.executor} for {project['name']} ({target}). "
                      "Existing runs keep their saved assignment.")
                if os.environ.get("COUNCIL_ROSTER"):
                    print("Note: COUNCIL_ROSTER is set and overrides this project roster for new runs.", file=sys.stderr)
                return 0
            roster, _ = select_roster(args)
            if roster.resolve() == BUNDLED_ROSTER:
                personal = args.home / "roster.toml"
                personal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if personal.parent.stat().st_mode & 0o077:
                    raise CouncilError(f"Council home is accessible to other users: {personal.parent}. Restrict it with chmod 700 before continuing")
                if not personal.exists():
                    shutil.copy2(BUNDLED_ROSTER, personal)
                roster = personal
            assign_role(roster, args.role, args.executor, args.model, args.effort)
            print(f"{args.role} now uses {args.model} via {args.executor}. Existing runs keep their saved assignment.")
            return 0
        read_only = args.command in {"status", "summary", "report", "context"} or (args.command == "chat" and args.history) or (args.command == "compact" and args.dry_run)
        if read_only and not (args.home / "council.sqlite3").exists():
            if args.command == "chat" and args.history:
                print("{}" if args.json else "No coordinator conversation yet.")
                return 0
            if args.command == "status" and not args.id:
                print("[]" if args.json else "No tasks yet. Start with council init --help or council demo.")
                return 0
            if args.command == "compact" and args.dry_run:
                project = project_config(resolve_project(args.home, args.project))
                result = {"project": project["name"], "thread": args.thread, "next_level": None,
                          "remaining_raw_gap": 0, "remaining_summary_chars": 0}
                print(json.dumps(result, indent=2) if args.json else
                      f"{project['name']}/{args.thread}: no compaction due")
                return 0
            raise CouncilError("No Council ledger exists yet")
        store = Store(args.home, read_only=read_only)
        try:
            if args.command in {"import-context", "context"}:
                project = project_config(resolve_project(args.home, args.project))
                if args.command == "import-context":
                    from .handoff import import_context
                    result = import_context(store, project["name"], args.source, args.summary)
                    print(f"Imported {len(result['sources'])} documents for {project['name']}. No tasks launched.")
                else:
                    result = store.context(project["name"])
                    if not result:
                        raise CouncilError("No context imported for this project")
                    if args.source:
                        source = next((s for s in result["sources"] if s["path"] == args.source), None)
                        if source is None:
                            raise CouncilError("Unknown archived source; run council context PROJECT")
                        print(source["content"])
                    elif args.json:
                        print(json.dumps(result, indent=2))
                    else:
                        print(result["summary"])
                        print("\nArchived sources (historical evidence):")
                        for source in result["sources"]:
                            print(f"  {source['path']} · {source['sha256'][:12]}")
            elif args.command == "compact":
                if not 1 <= args.max_steps <= 100:
                    raise CouncilError("--max-steps must be between 1 and 100")
                project = project_config(resolve_project(args.home, args.project))
                from .conversation import coordinator_assignment
                from .memory import compact_one
                if args.dry_run:
                    candidate = store.compaction_candidate(project["name"], args.thread)
                    result = {"project": project["name"], "thread": args.thread,
                              "next_level": candidate["level"] if candidate else None,
                              "remaining_raw_gap": store.context_gap(project["name"], args.thread),
                              "remaining_summary_chars": store.summary_context_gap(project["name"], args.thread)}
                else:
                    from .runner import preflight
                    assignment = coordinator_assignment(select_roster(args, project)[0])
                    preflight({"coordinator": assignment})
                    steps = []
                    with store.conversation_lock(project["name"], args.thread):
                        for _ in range(args.max_steps):
                            outcome = compact_one(store, project["name"], args.thread, assignment,
                                                  project.get("timeout_seconds", 900))
                            if outcome is None:
                                break
                            steps.append(outcome)
                        result = {"project": project["name"], "thread": args.thread,
                                  "steps": steps,
                                  "remaining_raw_gap": store.context_gap(project["name"], args.thread),
                                  "remaining_summary_chars": store.summary_context_gap(project["name"], args.thread)}
                print(json.dumps(result, indent=2) if args.json else
                      f"{project['name']}/{args.thread}: {len(result.get('steps', []))} summaries; "
                      f"{result['remaining_raw_gap']} raw turns and {result['remaining_summary_chars']} summary characters outside the prompt budget")
            elif args.command in {"run", "review"}:
                project = project_config(resolve_project(args.home, args.project))
                path = (Path(__file__).parent / "workflows" / "review.toml") if args.command == "review" else (args.workflow or Path(project["workflow"]))
                workflow = workflow_config(path)
                roster = roster_config(select_roster(args, project)[0], workflow)
                writes = any(p.get("kind") == "agent" and not p.get("read_only", True) for p in workflow["phases"].values())
                original = Path(project["workspace"])
                if args.workspace:
                    project["workspace"] = str(args.workspace.expanduser().resolve())
                if writes and not project.get("allow_in_place", False) and Path(project["workspace"]) == original:
                    raise CouncilError("This workflow edits files. Supply --workspace with an isolated working copy, or explicitly enable allow_in_place in the project configuration.")
                revision, files = fingerprint(project)
                task_id = store.create(project, workflow, roster, args.objective, revision, files)
                task = store.get(task_id) if args.plan_only else drive(store, task_id, notify=lambda message: print(message, file=sys.stderr, flush=True))
                print(json.dumps(task, indent=2) if args.json else summary(task))
            elif args.command == "chat":
                project = project_config(resolve_project(args.home, args.project))
                if args.history:
                    if args.new_session:
                        raise CouncilError("--new-session applies to a chat turn, not --history")
                    conversation = store.conversation(project["name"], args.thread, limit=200)
                    if args.all:
                        conversation["turns"] = store.all_conversation_turns(project["name"], args.thread)
                    conversation["session"] = store.chat_session(project["name"], args.thread)
                    conversation["metrics"] = store.chat_metrics(project["name"], args.thread)
                    if args.json:
                        print(json.dumps(conversation, indent=2))
                    else:
                        if conversation["memory"]:
                            print("Working memory (model-authored): " + conversation["memory"])
                        session = conversation["session"]
                        if session:
                            state = ("last turn did not commit; the next turn starts fresh" if session["pending"]
                                     else f"{session['session_turns']} turns")
                            print(f"Native session: {session['runtime']} · {state} · started because: {session['rotation_reason']}")
                        if conversation["metrics"]:
                            latest = conversation["metrics"][-1]
                            measured = ", ".join(f"{key}={latest[key]}" for key in
                                                 ("wall_ms", "input_tokens", "output_tokens", "cache_read_tokens")
                                                 if latest[key] is not None)
                            print(f"Last turn: {latest['mode']}" + (f" · {measured}" if measured else ""))
                        for segment in conversation["summaries"]:
                            print(f"Summary L{segment['level']} · turns {segment['first_turn']}-{segment['last_turn']}: {segment['summary']}")
                        for turn in conversation["turns"]:
                            print("You: " + turn["user_text"] + "\nCoordinator: " + turn["assistant_text"])
                            if turn["task_id"]:
                                linked = store.get(turn["task_id"])
                                print("Review task: " + turn["task_id"] + " · " + linked["status"])
                        if not conversation["turns"]:
                            print("No coordinator conversation yet.")
                else:
                    if args.all:
                        raise CouncilError("--all requires --history")
                    from .conversation import talk
                    roster_path, _ = select_roster(args, project)

                    def respond(message: str, allow_review: bool = False, new_session: bool = False):
                        if message.startswith("/review "):
                            message = "Ask the independent reviewer to review: " + message[len("/review "):]
                            allow_review = True
                        result = talk(store, project, roster_path, message, args.thread, allow_review=allow_review,
                                      new_session=new_session)
                        if args.json:
                            print(json.dumps(result, indent=2))
                        else:
                            print(result["message"])
                            if result["task_id"]:
                                print("\nReview task: " + result["task_id"])
                                print(summary(result["task"] or store.get(result["task_id"])))
                        return result
                    if args.message is not None:
                        answer = respond(args.message, allow_review=args.allow_review, new_session=args.new_session)
                        if answer["task"] and answer["task"]["status"] == "interrupted":
                            return 1
                    elif sys.stdin.isatty():
                        if args.allow_review:
                            raise CouncilError("--allow-review needs a one-shot message; use /review SCOPE inside interactive chat")
                        # --new-session applies to the first turn of an interactive chat.
                        new_session = args.new_session
                        while True:
                            try:
                                message = input("You> ").strip()
                            except (EOFError, KeyboardInterrupt):
                                print()
                                break
                            if message.lower() in {"exit", "quit"}:
                                break
                            if message:
                                respond(message, new_session=new_session)
                                new_session = False
                    else:
                        raise CouncilError("Provide a message, or run council chat PROJECT in a terminal")
            elif args.command in {"status", "summary"}:
                if not args.id:
                    rows = store.listing()
                    if args.json:
                        print(json.dumps(rows, indent=2))
                    elif rows:
                        for row in rows:
                            print(f"{row['id']} · {row['project']} · {row['status']} · {row['phase']}")
                    else:
                        print("No tasks yet. Start with council init --help or council demo.")
                else:
                    task = store.get(args.id)
                    print(json.dumps(task, indent=2) if args.json else summary(task))
            elif args.command == "work":
                task = drive(store, args.id, notify=lambda message: print(message, file=sys.stderr, flush=True))
                print(json.dumps(task, indent=2) if args.json else summary(task))
            elif args.command == "report":
                task = store.get(args.id)
                outputs = task["data"]["outputs"]
                if args.phase and args.phase not in outputs:
                    raise CouncilError("No saved report for that phase")
                for name, output in outputs.items():
                    if args.phase is None or name == args.phase:
                        print(f"\n{name}: {output['summary']}\n{output['report']}")
            elif args.command == "decide":
                decide(store, args.id, args.request, args.choice, args.note)
                task = drive(store, args.id) if args.continue_work else store.get(args.id)
                print(summary(task))
            elif args.command == "resume":
                resume(store, args.id, args.note)
                print(summary(store.get(args.id)))
            elif args.command in {"pause", "cancel"}:
                store.control(args.id, args.command)
                print(summary(store.get(args.id)))
            elif args.command == "gc":
                with store.runner_lock():
                    ids = store.gc(args.days, args.apply)
                print(json.dumps({"applied": args.apply, "tasks": ids, "preserved": "summaries, configuration provenance, and owner decisions"}, indent=2))
            if args.command in {"run", "review", "work"} and task["status"] == "interrupted":
                return 1
            return 0
        finally:
            store.close()
    except (CouncilError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Council: {exc}", file=sys.stderr)
        return 2


def main(argv=None) -> int:
    with termination_as_error():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
