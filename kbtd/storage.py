"""The Markdown file is the only task store; selection is disposable UI state."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .events import Event
from .ids import default_prefix
from .markdown import Document, parse_markdown
from .watchers import SkippedWatchers, Watchers


TEMPLATE = "# Tasks\n\n## Backlog\n\n## In Progress\n\n## Done\n"
type Version = tuple[int, int, int, int]
type SectionKey = tuple[str, int]  # heading and occurrence (for duplicate names)


def _version(st: os.stat_result) -> Version:
    return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size


@dataclass(frozen=True)
class Snapshot:
    document: Document
    version: Version
    watcher_warning: str = ""


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    # A sibling temp file keeps replace() on the same filesystem.
    tmp = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    name = Path(tmp.name)
    try:
        with tmp:
            tmp.write(text.encode("utf-8"))
            tmp.flush()
            os.fsync(tmp.fileno())
        if mode is not None:
            name.chmod(mode)
        os.replace(name, path)
    finally:
        name.unlink(missing_ok=True)


class BoardFile:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def create(self) -> None:
        with self.path.open("x", encoding="utf-8", newline="") as f:
            f.write(TEMPLATE)

    def id_prefix(self, document: Document) -> str:
        """The prefix for a new card ID: the board's counter line, else derived once."""
        return document.id_prefix or default_prefix(self.path)

    def version(self) -> Version:
        return _version(self.path.stat())

    def load(self) -> Snapshot:
        with self.path.open("r", encoding="utf-8", newline="") as f:
            version = _version(os.fstat(f.fileno()))
            return Snapshot(parse_markdown(f.read()), version)

    def save(self, document: Document, events: Sequence[Event] = ()) -> Snapshot:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if not os.access(self.path, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), self.path)
        _atomic_write(self.path, document.serialize(), mode)
        snapshot = self.load()
        if events:
            try:
                Watchers(self.path).emit_many(events)
            except SkippedWatchers as e:
                return replace(snapshot, watcher_warning=f"Board saved. {e}")
            except (OSError, RuntimeError, ValueError) as e:
                return replace(snapshot, watcher_warning=f"Board saved. Watcher dispatch failed: {e}")
        return snapshot


class SelectionStore:
    """One small JSON file per absolute path. Never required to use the board."""

    def __init__(self, project: Path):
        root = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
        key = hashlib.sha256(os.fsencode(project.resolve())).hexdigest()
        self.path = Path(root) / "kbtd" / f"{key}.json"

    def load(self) -> SectionKey | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                name, n = data.get("currentSection"), data.get("occurrence", 0)
                if isinstance(name, str) and type(n) is int and n >= 0:
                    return name, n
        except (OSError, ValueError):
            pass
        return None

    def save(self, key: SectionKey | None) -> None:
        try:
            if key is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.path, json.dumps({"currentSection": key[0], "occurrence": key[1]}))
        except OSError:
            pass  # A read-only state directory should not prevent editing tasks.
