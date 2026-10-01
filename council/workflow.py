"""Portable workflow contracts. Models and project policies belong in TOML."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parent.parent
TERMINALS = {"@done"}
# Runtimes whose adapter can resume a coordinator chat session; others keep ledger context.
NATIVE_CHAT_RUNTIMES = {"claude-cli"}


class CouncilError(Exception):
    pass


def load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CouncilError(f"Cannot load {path}: {exc}") from exc


def workflow_config(path: Path) -> dict:
    cfg = load_toml(path)
    phases = cfg.get("phases", {})
    if cfg.get("version") != 1 or cfg.get("entry") not in phases:
        raise CouncilError("Workflow needs version = 1 and an entry naming a phase")
    if not isinstance(cfg.get("max_steps"), int) or not 1 <= cfg["max_steps"] <= 100:
        raise CouncilError("max_steps must be between 1 and 100")
    for name, phase in phases.items():
        if name.startswith("@") or phase.get("kind") not in {"agent", "check", "human"}:
            raise CouncilError(f"Invalid phase {name}")
        routes = phase.get("routes", {})
        if not routes or any(target not in phases and target not in TERMINALS for target in routes.values()):
            raise CouncilError(f"Invalid routes for {name}")
        if phase["kind"] == "agent" and not all(phase.get(k) for k in ("role", "instructions")):
            raise CouncilError(f"Agent phase {name} requires a role and instructions")
        if any(item not in phases for item in phase.get("inputs", [])):
            raise CouncilError(f"Unknown context input for {name}")
        if phase["kind"] == "check" and not {"pass", "fail"}.issubset(routes):
            raise CouncilError(f"Check phase {name} needs pass and fail routes")
        if not 1 <= phase.get("max_visits", cfg["max_steps"]) <= cfg["max_steps"]:
            raise CouncilError(f"Invalid max_visits for {name}")
        if any(item not in phases for item in phase.get("fresh_inputs", [])):
            raise CouncilError(f"Unknown freshness requirement for {name}")
        if any(item not in phases for item in phase.get("fresh_on_complete", [])):
            raise CouncilError(f"Unknown completion freshness requirement for {name}")
    return cfg


def validate_effort(assignment: dict, executor: dict) -> None:
    effort = assignment.get("effort")
    if effort is None:
        return
    levels = {"antigravity-cli": {"low", "medium", "high", "max"}, "claude-cli": {"low", "medium", "high", "xhigh", "max"},
              "codex-cli": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"},
              "copilot-cli": {"low", "medium", "high", "xhigh", "max"}}
    if not isinstance(effort, str) or not effort.strip():
        raise CouncilError("effort must be a nonempty string")
    kind = executor.get("kind")
    if kind in levels and effort not in levels[kind]:
        raise CouncilError(f"Unsupported {kind} effort: {effort}")
    if kind == "command" and "{effort}" not in executor.get("argv", []):
        raise CouncilError("Command executor must consume {effort} when effort is configured")


def validate_chat_session(assignment: dict) -> None:
    if assignment.get("chat_session", "ledger") not in {"native", "ledger"}:
        raise CouncilError('chat_session must be "native" or "ledger"')
    turns = assignment.get("chat_session_turns", 20)
    if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 100:
        raise CouncilError("chat_session_turns must be an integer between 1 and 100")
    tokens = assignment.get("chat_session_context_tokens", 100_000)
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not 1_000 <= tokens <= 2_000_000:
        raise CouncilError("chat_session_context_tokens must be an integer between 1000 and 2000000")


def chat_session_mode(assignment: dict, executor: dict) -> str:
    """native, ledger, or fallback when native is requested on a runtime without resumable sessions."""
    if assignment.get("chat_session", "ledger") != "native":
        return "ledger"
    return "native" if executor.get("kind") in NATIVE_CHAT_RUNTIMES else "fallback"


def roster_tables(cfg: dict, path: Path) -> None:
    for key, detail in (("workflow_roles", "executor and model"), ("executors", "kind")):
        entries = cfg.get(key, {})
        if not isinstance(entries, dict):
            raise CouncilError(f"{path}: {key} must be a table")
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                raise CouncilError(f"{path}: {key}.{name} must be a table with {detail}")


def roster_config(path: Path, workflow: dict) -> dict:
    cfg = load_toml(path)
    roster_tables(cfg, path)
    roles = cfg.get("workflow_roles", {})
    executors = cfg.get("executors", {})
    selected = {}
    for phase in workflow["phases"].values():
        if phase["kind"] != "agent":
            continue
        role = phase["role"]
        assignment = roles.get(role, {})
        if not assignment.get("model") or assignment.get("executor") not in executors:
            raise CouncilError(f"Configure workflow_roles.{role}: executor and model")
        executor = executors[assignment["executor"]]
        if executor.get("kind") not in {"codex-cli", "claude-cli", "antigravity-cli", "copilot-cli", "command"}:
            raise CouncilError(f"Unknown executor kind for {role}")
        if executor["kind"] == "command":
            if executor.get("trusted") is not True:
                raise CouncilError("Command executor runs with host permissions; set trusted = true only for an adapter you control")
            argv = executor.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                raise CouncilError("Command executors need a nonempty argv array")
        if executor["kind"] == "antigravity-cli" and not phase.get("read_only", True):
            raise CouncilError("Antigravity executor currently supports read-only research phases only")
        validate_effort(assignment, executor)
        validate_chat_session(assignment)
        selected[role] = {**assignment, "runtime": executor}
    return selected


def declared_roster(path: Path, cfg: dict) -> str | None:
    if "roster" not in cfg:
        return None
    # Existence is checked only when this roster is actually selected.
    if not isinstance(cfg["roster"], str) or not cfg["roster"].strip():
        raise CouncilError("roster must be a nonempty path to a roster TOML")
    roster = Path(cfg["roster"]).expanduser()
    return str((roster if roster.is_absolute() else path.resolve().parent / roster).resolve())


def project_config(path: Path) -> dict:
    cfg = load_toml(path)
    root = Path(cfg.get("workspace", "")).expanduser()
    root = (root if root.is_absolute() else path.resolve().parent / root).resolve()
    if cfg.get("version") != 1 or not cfg.get("name") or not cfg.get("workspace") or not root.is_dir():
        raise CouncilError("Project needs version = 1, name, and an existing workspace")
    cfg["workspace"] = str(root)
    cfg["config_path"] = str(path.resolve())
    if cfg.get("workflow"):
        workflow = Path(cfg["workflow"]).expanduser()
        cfg["workflow"] = str((workflow if workflow.is_absolute() else path.resolve().parent / workflow).resolve())
    if "roster" in cfg:
        cfg["roster"] = declared_roster(path, cfg)
    for command in cfg.get("checks", []):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise CouncilError("checks must be arrays of arguments, never shell command strings")
    if not 1 <= cfg.get("timeout_seconds", 900) <= 7200:
        raise CouncilError("timeout_seconds must be between 1 and 7200")
    if not 1 <= cfg.get("retention_days", 30) <= 3650:
        raise CouncilError("retention_days must be between 1 and 3650")
    if not all(isinstance(x, str) for x in cfg.get("protected_paths", [])):
        raise CouncilError("protected_paths must be an array of glob patterns")
    return cfg


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise CouncilError(result.stderr.decode(errors="replace").strip() or "Git command failed")
    return result.stdout


def workspace_files(project: dict) -> list[Path]:
    root = Path(project["workspace"])
    if project.get("git", True):
        top = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if top != root:
            raise CouncilError("workspace must be a Git repository root")
        names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        files = [Path(os.fsdecode(name)) for name in names if name]
    else:
        patterns = project.get("include", [])
        if not patterns:
            raise CouncilError("Non-Git projects require explicit include patterns")
        files = [p.relative_to(root) for pattern in patterns for p in root.glob(pattern) if p.is_file()]
    result = []
    total = 0
    for relative in sorted(set(files)):
        if relative.is_absolute() or ".." in relative.parts:
            raise CouncilError("Workspace path escapes project")
        if any(part in {".git", ".council", "__pycache__"} or part.startswith(".env") for part in relative.parts):
            continue
        source = root / relative
        if not source.exists() and not source.is_symlink():
            continue  # deleted tracked file
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != root and root in parent.parents):
            raise CouncilError(f"Snapshot requires regular files, found symlink: {relative}")
        if not source.is_file():
            raise CouncilError(f"Snapshot cannot include special file or submodule: {relative}")
        total += source.stat().st_size
        if total > project.get("max_snapshot_bytes", 100_000_000):
            raise CouncilError("Workspace snapshot exceeds max_snapshot_bytes")
        result.append(relative)
    return result


def fingerprint(project: dict) -> tuple[str, dict]:
    root = Path(project["workspace"])
    entries = {}
    for relative in workspace_files(project):
        source = root / relative
        entries[str(relative)] = {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "mode": source.stat().st_mode & 0o777}
    head = git(root, "rev-parse", "HEAD").decode().strip() if project.get("git", True) else "files"
    digest = hashlib.sha256(json.dumps([head, entries], sort_keys=True).encode()).hexdigest()
    return f"{head}:{digest}", entries


def snapshot(project: dict, target: Path) -> None:
    root = Path(project["workspace"])
    for relative in workspace_files(project):
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, dest)


def response_schema(outcomes: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {"outcome": {"type": "string", "enum": outcomes + ["needs_owner"]},
                           "summary": {"type": "string"}, "report": {"type": "string"},
                           "question": {"type": "string"}},
            "required": ["outcome", "summary", "report", "question"]}


def validate_response(value: object, outcomes: list[str]) -> dict:
    if not isinstance(value, dict) or set(value) != {"outcome", "summary", "report", "question"}:
        raise CouncilError("Agent response must contain exactly outcome, summary, report, question")
    if not all(isinstance(v, str) for v in value.values()) or value["outcome"] not in outcomes + ["needs_owner"]:
        raise CouncilError("Agent returned an invalid outcome or field type")
    if not value["summary"].strip() or (value["outcome"] == "needs_owner" and not value["question"].strip()):
        raise CouncilError("Agent response is missing its summary or owner question")
    if len(json.dumps(value)) > 128_000:
        raise CouncilError("Agent report exceeds 128 KB")
    if len(value["summary"]) > 2000 or len(value["question"]) > 8000:
        raise CouncilError("Agent summary/question exceeds its size limit")
    return value
