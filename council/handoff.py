"""Import historical Markdown handoffs as evidence without launching work."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from .workflow import CouncilError


def import_context(store, project: str, source: Path, summary_path: Path) -> dict:
    if source.is_symlink() or not source.is_dir():
        raise CouncilError("Context source must be a regular directory")
    root = source.resolve()
    summary = summary_path.read_text()
    if not summary.strip() or len(summary.encode()) > 32_000:
        raise CouncilError("Context summary must be nonempty and at most 32 KB")
    sources = []
    total = 0
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise CouncilError("Context import does not follow symlinks")
        if not path.is_file():
            continue
        if path.stat().st_size > 512_000:
            raise CouncilError("A context document exceeds 512 KB")
        payload = path.read_bytes()
        total += len(payload)
        if total > 8_000_000 or len(sources) >= 500:
            raise CouncilError("Context import exceeds 8 MB or 500 documents")
        sources.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(payload).hexdigest(),
                        "content": payload.decode("utf-8"), "source_mtime": path.stat().st_mtime})
    if not sources:
        raise CouncilError("No Markdown documents found")
    revision = hashlib.sha256(json.dumps([str(root), summary, [(s['path'], s['sha256']) for s in sources]],
                                       sort_keys=True).encode()).hexdigest()
    existing = store.context(project)
    if existing and existing["revision"] == revision:
        return existing
    data = {"revision": revision, "summary": summary, "source_root": str(root),
            "imported_at": time.time(), "sources": sources}
    store.save_context(project, data)
    return data
