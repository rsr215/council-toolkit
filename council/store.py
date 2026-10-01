"""Single durable ledger; no per-message files and no implicit job replay."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
import stat
import json
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

from .workflow import CouncilError


class Store:
    def __init__(self, home: Path, read_only: bool = False):
        self.home = home.expanduser().resolve()
        if not read_only:
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.home.stat().st_mode) & 0o077:
            raise CouncilError(f"Council home is accessible to other users: {self.home}. Restrict it with chmod 700 before continuing")
        database = self.home / "council.sqlite3"
        if read_only:
            self.db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.db = sqlite3.connect(database, timeout=10)
            os.chmod(database, 0o600)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            self.db.close()
            raise CouncilError("This ledger needs a newer version of Council")
        if read_only:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        try:
            self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,
            phase TEXT NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, closed REAL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS project_context (
            project TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY, task TEXT NOT NULL, at REAL NOT NULL,
            kind TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations (
            project TEXT NOT NULL, thread TEXT NOT NULL, memory TEXT NOT NULL,
            updated REAL NOT NULL, PRIMARY KEY(project, thread));
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
            at REAL NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
            action TEXT NOT NULL, task_id TEXT);
        CREATE INDEX IF NOT EXISTS conversation_turns_project_thread
            ON conversation_turns(project, thread, id);
        CREATE TABLE IF NOT EXISTS conversation_segments (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
            level INTEGER NOT NULL, first_turn INTEGER NOT NULL, last_turn INTEGER NOT NULL,
            summary TEXT NOT NULL, active INTEGER NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS conversation_segments_active
            ON conversation_segments(project, thread, active, level, first_turn);
        CREATE TABLE IF NOT EXISTS conversation_archives (
            project TEXT NOT NULL, thread TEXT NOT NULL, first_turn INTEGER NOT NULL,
            last_turn INTEGER NOT NULL, turns_zlib BLOB NOT NULL,
            PRIMARY KEY(project, thread, first_turn));
        CREATE TABLE IF NOT EXISTS chat_sessions (
            project TEXT NOT NULL, thread TEXT NOT NULL, runtime TEXT NOT NULL,
            session_id TEXT NOT NULL, context_key TEXT NOT NULL, workspace_revision TEXT NOT NULL,
            last_turn_id INTEGER, session_turns INTEGER NOT NULL, pending INTEGER NOT NULL,
            rotation_reason TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            PRIMARY KEY(project, thread));
        CREATE TABLE IF NOT EXISTS chat_metrics (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
            turn_id INTEGER NOT NULL, at REAL NOT NULL, mode TEXT NOT NULL, rotation_reason TEXT,
            wall_ms INTEGER, duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_creation_tokens INTEGER, cost_usd REAL);
        CREATE INDEX IF NOT EXISTS chat_metrics_project_thread
            ON chat_metrics(project, thread, id);
        PRAGMA user_version=4;
        COMMIT;
        """)
        except sqlite3.Error:
            self.db.rollback()
            self.db.close()
            raise
        for suffix in ("-wal", "-shm"):
            journal = Path(str(database) + suffix)
            if journal.exists():
                os.chmod(journal, 0o600)

    def close(self):
        self.db.close()

    @contextmanager
    def runner_lock(self):
        with (self.home / "runner.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CouncilError("A council worker is already active; use status, pause, or cancel") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def conversation_lock(self, project: str, thread: str):
        # One small lock file per conversation, never one per turn.
        locks = self.home / "conversation-locks"
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256((project + "\0" + thread).encode()).hexdigest()[:24]
        with (locks / key).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CouncilError("This coordinator conversation is already active; retry after it finishes") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def context(self, project: str) -> dict | None:
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='project_context'").fetchone():
            return None
        row = self.db.execute("SELECT data FROM project_context WHERE project=?", (project,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_context(self, project: str, data: dict):
        with self.db:
            self.db.execute("INSERT INTO project_context VALUES (?,?) ON CONFLICT(project) DO UPDATE SET data=excluded.data",
                            (project, json.dumps(data)))

    def conversation(self, project: str, thread: str = "main", limit: int = 12,
                     max_chars: int | None = None) -> dict:
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversations'").fetchone():
            return {"memory": "", "turns": [], "summaries": []}
        row = self.db.execute("SELECT memory FROM conversations WHERE project=? AND thread=?", (project, thread)).fetchone()
        turns = self.db.execute("""SELECT id,at,user_text,assistant_text,action,task_id
            FROM conversation_turns WHERE project=? AND thread=? ORDER BY id DESC LIMIT ?""",
            (project, thread, limit)).fetchall()
        selected = []
        used = 0
        for turn in turns:
            size = len(turn["user_text"]) + len(turn["assistant_text"])
            if max_chars is not None and selected and used + size > max_chars:
                break
            selected.append(dict(turn))
            used += size
        summaries = []
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_segments'").fetchone():
            summaries = [dict(item) for item in self.db.execute("""SELECT id,level,first_turn,last_turn,summary
                FROM conversation_segments WHERE project=? AND thread=? AND active=1
                ORDER BY first_turn""", (project, thread))]
        return {"memory": row[0] if row else "", "turns": list(reversed(selected)), "summaries": summaries}

    def all_conversation_turns(self, project: str, thread: str = "main") -> list[dict]:
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_turns'").fetchone():
            return []
        turns = [dict(row) for row in self.db.execute("""SELECT id,at,user_text,assistant_text,action,task_id
            FROM conversation_turns WHERE project=? AND thread=?""", (project, thread))]
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_archives'").fetchone():
            for row in self.db.execute("""SELECT turns_zlib FROM conversation_archives
                WHERE project=? AND thread=? ORDER BY first_turn""", (project, thread)):
                turns.extend(json.loads(zlib.decompress(row[0])))
        return sorted(turns, key=lambda turn: turn["id"])

    def context_gap(self, project: str, thread: str) -> int:
        count = self.db.execute("SELECT COUNT(*) FROM conversation_turns WHERE project=? AND thread=?",
                                (project, thread)).fetchone()[0]
        selected = len(self.conversation(project, thread, limit=32, max_chars=32_000)["turns"])
        return count - selected

    def summary_context_gap(self, project: str, thread: str, budget: int = 10_000) -> int:
        summaries = self.conversation(project, thread, limit=0)["summaries"]
        return max(0, sum(len(item["summary"]) + 100 for item in summaries) - budget)

    def compaction_candidate(self, project: str, thread: str) -> dict | None:
        count = self.db.execute("SELECT COUNT(*) FROM conversation_turns WHERE project=? AND thread=?",
                                (project, thread)).fetchone()[0]
        selected = len(self.conversation(project, thread, limit=32, max_chars=32_000)["turns"])
        if count >= 31 or count > selected:
            keep = min(16, selected) if count >= 31 else selected
            rows = self.db.execute("""SELECT id,at,user_text,assistant_text,action,task_id
                FROM conversation_turns WHERE project=? AND thread=? ORDER BY id LIMIT ?""",
                (project, thread, min(24, count - keep))).fetchall()
            turns = []
            used = 0
            for row in rows:
                item = dict(row)
                size = len(json.dumps(item))
                if turns and used + size > 48_000:
                    break
                turns.append(item)
                used += size
            return {"kind": "turns", "turns": turns, "first_turn": turns[0]["id"],
                    "last_turn": turns[-1]["id"], "level": 1}
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_segments'").fetchone():
            return None
        active = [dict(row) for row in self.db.execute("""SELECT id,level,first_turn,last_turn,summary
            FROM conversation_segments WHERE project=? AND thread=? AND active=1 ORDER BY first_turn""",
            (project, thread))]
        if sum(len(item["summary"]) + 100 for item in active) > 10_000 and len(active) >= 2:
            rows = active[:4]
            return {"kind": "summaries", "sources": rows, "first_turn": rows[0]["first_turn"],
                    "last_turn": rows[-1]["last_turn"], "level": max(row["level"] for row in rows) + 1}
        for level in sorted({item["level"] for item in active}):
            for start in range(len(active) - 3):
                rows = active[start:start + 4]
                if all(item["level"] == level for item in rows):
                    return {"kind": "summaries", "sources": rows, "first_turn": rows[0]["first_turn"],
                            "last_turn": rows[-1]["last_turn"], "level": level + 1}
        return None

    def save_compaction(self, project: str, thread: str, candidate: dict, summary: str):
        if not summary.strip() or len(summary) > 2500:
            raise CouncilError("Conversation summary must contain 1-2500 characters")
        with self.db:
            if candidate["kind"] == "turns":
                ids = [turn["id"] for turn in candidate["turns"]]
                current = [dict(row) for row in self.db.execute("""SELECT id,at,user_text,assistant_text,action,task_id
                    FROM conversation_turns WHERE project=? AND thread=? ORDER BY id LIMIT ?""",
                    (project, thread, len(ids)))]
                if current != candidate["turns"]:
                    raise CouncilError("Conversation changed during compaction; retry")
                archive = zlib.compress(json.dumps(current, ensure_ascii=False).encode(), level=6)
                self.db.execute("INSERT INTO conversation_archives VALUES (?,?,?,?,?)",
                                (project, thread, ids[0], ids[-1], archive))
                self.db.executemany("DELETE FROM conversation_turns WHERE id=? AND project=? AND thread=?",
                                    [(id_, project, thread) for id_ in ids])
            elif candidate["kind"] == "summaries":
                ids = [source["id"] for source in candidate["sources"]]
                current = [dict(row) for row in self.db.execute("""SELECT id,level,first_turn,last_turn,summary
                    FROM conversation_segments WHERE project=? AND thread=? AND id IN (""" + ",".join("?" for _ in ids) + ") AND active=1 ORDER BY first_turn", [project, thread, *ids])]
                if current != candidate["sources"]:
                    raise CouncilError("Conversation summaries changed during compaction; retry")
                self.db.executemany("UPDATE conversation_segments SET active=0 WHERE id=? AND project=? AND thread=?",
                                    [(id_, project, thread) for id_ in ids])
            else:
                raise CouncilError("Unknown conversation compaction kind")
            self.db.execute("""INSERT INTO conversation_segments
                (project,thread,level,first_turn,last_turn,summary,active,created)
                VALUES (?,?,?,?,?,?,1,?)""",
                (project, thread, candidate["level"], candidate["first_turn"],
                 candidate["last_turn"], summary, time.time()))
            # Summaries changed, so a native chat session no longer lines up with the ledger.
            self.db.execute("DELETE FROM chat_sessions WHERE project=? AND thread=?", (project, thread))

    def last_turn_id(self, project: str, thread: str) -> int | None:
        return self.db.execute("""SELECT MAX(id) FROM (
            SELECT id FROM conversation_turns WHERE project=? AND thread=?
            UNION ALL SELECT last_turn FROM conversation_archives WHERE project=? AND thread=?)""",
            (project, thread, project, thread)).fetchone()[0]

    def chat_session(self, project: str, thread: str) -> dict | None:
        # Older ledgers opened read-only have no session table.
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_sessions'").fetchone():
            return None
        row = self.db.execute("SELECT * FROM chat_sessions WHERE project=? AND thread=?", (project, thread)).fetchone()
        return dict(row) if row else None

    def chat_metrics(self, project: str, thread: str, limit: int = 10) -> list[dict]:
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_metrics'").fetchone():
            return []
        rows = self.db.execute("""SELECT turn_id,at,mode,rotation_reason,wall_ms,duration_ms,input_tokens,
            output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd
            FROM chat_metrics WHERE project=? AND thread=? ORDER BY id DESC LIMIT ?""",
            (project, thread, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def chat_turn_usage(self, project: str, thread: str, turn_id: int) -> int | None:
        """Provider-reported input plus cache tokens for one committed turn, or None if none were reported."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_metrics'").fetchone():
            return None
        row = self.db.execute("""SELECT input_tokens,cache_read_tokens,cache_creation_tokens
            FROM chat_metrics WHERE project=? AND thread=? AND turn_id=? ORDER BY id DESC LIMIT 1""",
            (project, thread, turn_id)).fetchone()
        reported = [value for value in (row or ()) if value is not None]
        return sum(reported) if reported else None

    def _put_chat_session(self, project: str, thread: str, binding: dict, pending: bool):
        self.db.execute("""INSERT INTO chat_sessions
            (project,thread,runtime,session_id,context_key,workspace_revision,last_turn_id,
             session_turns,pending,rotation_reason,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project,thread) DO UPDATE SET runtime=excluded.runtime,
            session_id=excluded.session_id,context_key=excluded.context_key,
            workspace_revision=excluded.workspace_revision,last_turn_id=excluded.last_turn_id,
            session_turns=excluded.session_turns,pending=excluded.pending,
            rotation_reason=excluded.rotation_reason,created=excluded.created,updated=excluded.updated""",
            (project, thread, binding["runtime"], binding["session_id"], binding["context_key"],
             binding["workspace_revision"], binding["last_turn_id"], binding["session_turns"],
             int(pending), binding["rotation_reason"], binding["created"], time.time()))

    def begin_chat_session(self, project: str, thread: str, binding: dict):
        """Mark the native session in use before a provider call; only a committed turn clears it."""
        with self.db:
            self._put_chat_session(project, thread, binding, pending=True)

    def drop_chat_session(self, project: str, thread: str):
        with self.db:
            self.db.execute("DELETE FROM chat_sessions WHERE project=? AND thread=?", (project, thread))

    def _append_exchange(self, project: str, thread: str, user_text: str, assistant_text: str,
                         memory: str, action: str, task_id: str | None = None, chat: dict | None = None):
        now = time.time()
        cursor = self.db.execute("""INSERT INTO conversation_turns
            (project,thread,at,user_text,assistant_text,action,task_id)
            VALUES (?,?,?,?,?,?,?)""",
            (project, thread, now, user_text, assistant_text, action, task_id))
        self.db.execute("""INSERT INTO conversations(project,thread,memory,updated) VALUES (?,?,?,?)
            ON CONFLICT(project,thread) DO UPDATE SET memory=excluded.memory,updated=excluded.updated""",
            (project, thread, memory, now))
        if chat is not None:
            # The session binding and its measurements commit with the ledger turn they describe.
            turn_id = cursor.lastrowid
            if chat["binding"] is not None:
                self._put_chat_session(project, thread, {**chat["binding"], "last_turn_id": turn_id}, pending=False)
            metrics = chat["metrics"]
            self.db.execute("""INSERT INTO chat_metrics
                (project,thread,turn_id,at,mode,rotation_reason,wall_ms,duration_ms,input_tokens,
                 output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project, thread, turn_id, now, metrics["mode"], metrics.get("rotation_reason"),
                 metrics.get("wall_ms"), metrics.get("duration_ms"), metrics.get("input_tokens"),
                 metrics.get("output_tokens"), metrics.get("cache_read_tokens"),
                 metrics.get("cache_creation_tokens"), metrics.get("cost_usd")))

    def append_exchange(self, project: str, thread: str, user_text: str, assistant_text: str,
                        memory: str, action: str, task_id: str | None = None, chat: dict | None = None):
        with self.db:
            self._append_exchange(project, thread, user_text, assistant_text, memory, action, task_id, chat)

    def create(self, project: dict, workflow: dict, roster: dict, objective: str, revision: str,
               files: dict, exchange: dict | None = None) -> str:
        if not objective.strip():
            raise CouncilError("Objective cannot be empty")
        size = sum(p.stat().st_size for p in self.home.glob("council.sqlite3*"))
        if size > 256 * 1024 * 1024:
            raise CouncilError("Ledger exceeds 256 MB. Inspect council gc before creating more work")
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        data = {"objective": objective, "project": project, "workflow": workflow,
                "roster": roster, "steps": 0, "visits": {}, "outputs": {}, "decisions": [],
                "expected_workspace": revision, "initial_files": files,
                "summary": "Queued", "gate": None, "control": None, "pruned": False}
        context = self.context(project["name"])
        if context:
            data["project_context"] = {key: context[key] for key in ("summary", "revision", "imported_at")}
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                            (task_id, project["name"], "ready", workflow["entry"], 0, now, now, None, json.dumps(data)))
            self.event(task_id, "created", objective)
            if exchange is not None:
                self._append_exchange(project["name"], exchange["thread"], exchange["user_text"],
                                      exchange["assistant_text"], exchange["memory"], "review", task_id,
                                      exchange.get("chat"))
        return task_id

    def event(self, task_id: str, kind: str, message: str):
        self.db.execute("INSERT INTO events(task,at,kind,message) VALUES (?,?,?,?)",
                        (task_id, time.time(), kind, message[:8000]))

    def get(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise CouncilError(f"Unknown task: {task_id}")
        task = dict(row)
        task["data"] = json.loads(task["data"])
        return task

    def save(self, task: dict, kind: str, message: str):
        now = time.time()
        closed = now if task["status"] in {"completed", "cancelled"} else None
        with self.db:
            cursor = self.db.execute("""UPDATE tasks SET status=?,phase=?,revision=revision+1,
                updated=?,closed=?,data=? WHERE id=? AND revision=?""",
                (task["status"], task["phase"], now, closed, json.dumps(task["data"]), task["id"], task["revision"]))
            if cursor.rowcount != 1:
                raise CouncilError("Task changed concurrently; refresh status before retrying")
            self.event(task["id"], kind, message)
        task["revision"] += 1
        task["updated"] = now
        task["closed"] = closed

    def listing(self) -> list[dict]:
        rows = self.db.execute("SELECT id,project,status,phase,revision,updated FROM tasks ORDER BY updated DESC")
        return [dict(row) for row in rows]

    def control(self, task_id: str, action: str):
        task = self.get(task_id)
        if task["status"] in {"completed", "cancelled"}:
            raise CouncilError("Task is already closed")
        if task["status"] == "running":
            task["data"]["control"] = action
        else:
            task["status"] = "cancelled" if action == "cancel" else "paused"
        self.save(task, action, f"Owner requested {action}")

    def gc(self, days: int | None, apply: bool) -> list[str]:
        if days is not None and days < 1:
            raise CouncilError("Retention must be at least one day")
        candidates = [self.get(row[0]) for row in self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('completed','cancelled')")]
        candidates = [task for task in candidates if task["closed"] is not None and
                      task["closed"] < time.time() - (days if days is not None else task["data"]["project"].get("retention_days", 30)) * 86400]
        ids = [task["id"] for task in candidates if not task["data"].get("pruned")]
        if apply:
            for task in candidates:
                if task["id"] not in ids:
                    continue
                data = task["data"]
                # Keep compact outcome, decision receipts, identity and provenance.
                data["phase_summaries"] = {name: output["summary"] for name, output in data["outputs"].items()}
                for key in ("outputs", "initial_files"):
                    data[key] = {}
                data.pop("last_error", None)
                data["pruned"] = True
                with self.db:
                    self.db.execute("DELETE FROM events WHERE task=?", (task["id"],))
                    self.db.execute("UPDATE tasks SET data=?,revision=revision+1 WHERE id=?",
                                    (json.dumps(data), task["id"]))
                    self.event(task["id"], "compacted", "Expired reports removed; outcome and decisions retained")
            if ids:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.db.execute("VACUUM")
        return ids
