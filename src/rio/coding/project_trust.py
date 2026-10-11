"""Saved project trust policy for MCP configuration.

Project trust controls ambient project resources. It is deliberately not a
filesystem, process, network, tool, model, or prompt-injection sandbox.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from rio.coding.paths import RioPaths

TrustDecision = Literal["trusted", "untrusted"]


class ProjectTrustError(RuntimeError):
    """A trust path, store, or persistence operation failed safely."""


@dataclass(frozen=True, slots=True)
class CanonicalProjectPath:
    """An existing, canonical project working directory."""

    value: Path


@dataclass(frozen=True, slots=True)
class SavedTrustEntry:
    """A validated saved exact or inherited decision."""

    path: CanonicalProjectPath
    decision: TrustDecision


def _darwin_filesystem_path(path: Path) -> Path:
    """Return macOS's case-preserving path for an existing filesystem object."""
    import fcntl

    descriptor = os.open(path, os.O_RDONLY)
    try:
        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)  # F_GETPATH / MAXPATHLEN
    finally:
        os.close(descriptor)
    value = raw.split(b"\0", 1)[0]
    if not value:
        raise OSError(f"Could not determine filesystem casing for {path}")
    return Path(os.fsdecode(value))


def canonicalize_project_path(path: Path, *, base: Path | None = None) -> CanonicalProjectPath:
    """Strictly canonicalize an existing destination cwd."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        if base is None:
            raise ProjectTrustError("A base directory is required for a relative project cwd")
        expanded = base.expanduser() / expanded
    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectTrustError(f"Could not canonicalize project cwd {expanded}: {exc}") from exc
    if not resolved.is_dir():
        raise ProjectTrustError(f"Project cwd is not a directory: {resolved}")
    try:
        if sys.platform == "win32":
            resolved = Path(os.path.normcase(str(resolved)))
        elif sys.platform == "darwin":
            # normcase() is a no-op on Darwin. F_GETPATH asks the mounted
            # filesystem for its case-preserving spelling, so aliases on a
            # case-insensitive volume share a key without collapsing distinct
            # paths on a case-sensitive volume.
            resolved = _darwin_filesystem_path(resolved)
    except (OSError, UnicodeError) as exc:
        raise ProjectTrustError(
            f"Could not canonicalize project cwd casing {resolved}: {exc}"
        ) from exc
    return CanonicalProjectPath(resolved)


class ProjectTrustStore:
    """Versioned, locked, atomically replaced trust decision store."""

    def __init__(self, paths: RioPaths | None = None) -> None:
        self.paths = paths or RioPaths()
        self.path = self.paths.home / "trust.json"
        self.lock_path = self.paths.home / "trust.json.lock"
        self.pending_path = self.paths.home / "trust.json.pending"

    def nearest(self, cwd: CanonicalProjectPath) -> SavedTrustEntry | None:
        decisions = self.read()
        current = cwd.value
        while True:
            decision = decisions.get(current)
            if decision is not None:
                return SavedTrustEntry(CanonicalProjectPath(current), decision)
            if current.parent == current:
                return None
            current = current.parent

    def read(self) -> dict[Path, TrustDecision]:
        with self._locked():
            return self._read_unlocked()

    def set(self, path: CanonicalProjectPath, decision: TrustDecision) -> None:
        with self._locked():
            decisions = self._read_unlocked()
            decisions[path.value] = decision
            self._write_unlocked(decisions)

    def remove(self, path: CanonicalProjectPath) -> None:
        with self._locked():
            decisions = self._read_unlocked()
            decisions.pop(path.value, None)
            self._write_unlocked(decisions)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.paths.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as handle:
                os.chmod(self.lock_path, 0o600)
                _lock(handle)
                try:
                    yield
                finally:
                    _unlock(handle)
        except ProjectTrustError:
            raise
        except OSError as exc:
            raise ProjectTrustError(
                f"Could not lock project trust store {self.path}: {exc}"
            ) from exc

    def _read_unlocked(self) -> dict[Path, TrustDecision]:
        # A pending journal means an update did not reach its commit point.
        # Ordinary reads must never guess whether the interrupted operation was
        # a grant or a revocation: either direction could resurrect trust.
        # Recovery is attempted only by the writer that observed its own
        # failure; a journal left by a crash remains visibly fail-closed.
        if self.pending_path.exists():
            raise ProjectTrustError(
                f"Project trust store {self.path} has an incomplete update; "
                f"pending journal requires explicit recovery: {self.pending_path}"
            )
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectTrustError(
                f"Could not read project trust store {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"version", "decisions"}:
            raise ProjectTrustError(f"Malformed project trust store {self.path}: unknown schema")
        if payload["version"] != 1 or not isinstance(payload["decisions"], list):
            raise ProjectTrustError(f"Unsupported or malformed project trust store {self.path}")
        result: dict[Path, TrustDecision] = {}
        for raw in payload["decisions"]:
            if not isinstance(raw, dict) or set(raw) != {"path", "decision"}:
                raise ProjectTrustError(f"Malformed decision in project trust store {self.path}")
            raw_path = raw["path"]
            decision = raw["decision"]
            if not isinstance(raw_path, str) or decision not in ("trusted", "untrusted"):
                raise ProjectTrustError(f"Malformed decision in project trust store {self.path}")
            candidate = Path(raw_path)
            if not candidate.is_absolute() or Path(os.path.normpath(raw_path)) != candidate:
                raise ProjectTrustError(f"Noncanonical path in project trust store {self.path}")
            normalized = Path(os.path.normcase(raw_path)) if sys.platform == "win32" else candidate
            if sys.platform == "darwin" and candidate.exists():
                try:
                    normalized = _darwin_filesystem_path(candidate)
                except (OSError, UnicodeError) as exc:
                    raise ProjectTrustError(
                        f"Could not validate path casing in project trust store {self.path}: {exc}"
                    ) from exc
                if normalized != candidate:
                    raise ProjectTrustError(
                        f"Noncanonical path casing in project trust store {self.path}: {candidate}"
                    )
            if normalized in result:
                raise ProjectTrustError(f"Duplicate path in project trust store {self.path}")
            result[normalized] = decision
        return result

    def _write_unlocked(self, decisions: Mapping[Path, TrustDecision]) -> None:
        payload = {
            "version": 1,
            "decisions": [
                {"path": str(path), "decision": decision}
                for path, decision in sorted(decisions.items(), key=lambda item: str(item[0]))
            ],
        }
        data = (json.dumps(payload, indent=2) + "\n").encode()
        prior_bytes = self.path.read_bytes() if self.path.exists() else None

        # Persist a fail-closed undo journal before touching trust.json. Readers
        # reject the store while this marker exists, so even failed recovery can
        # never expose a newly granting destination.
        journal = (b"present\n" + prior_bytes) if prior_bytes is not None else b"absent\n"
        try:
            self._atomic_replace(self.pending_path, journal, prefix=".trust-pending-")
            self._atomic_replace(self.path, data, prefix=".trust-")
        except OSError as exc:
            recovery_error = self._recover_unlocked()
            detail = f"; recovery failed: {recovery_error}" if recovery_error else ""
            raise ProjectTrustError(
                f"Could not write project trust store {self.path}: {exc}{detail}"
            ) from exc

        # The destination and its directory entry are durable. Failure to clear
        # the journal is still a failed update and must restore the prior state.
        try:
            self.pending_path.unlink()
        except OSError as exc:
            recovery_error = self._recover_unlocked()
            detail = f"; recovery failed: {recovery_error}" if recovery_error else ""
            raise ProjectTrustError(
                f"Could not commit project trust store {self.path}: {exc}{detail}"
            ) from exc
        # Journal cleanup is not part of the data commit. If this fsync fails,
        # either the deletion persists (the durable destination grants) or the
        # journal reappears after a crash (reads fail closed).
        with suppress(OSError):
            _fsync_directory(self.paths.home)

    def _atomic_replace(self, destination: Path, data: bytes, *, prefix: str) -> None:
        fd = -1
        temporary: Path | None = None
        try:
            fd, raw = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=self.paths.home)
            temporary = Path(raw)
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(self.paths.home)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _recover_unlocked(self) -> OSError | None:
        """Restore the journaled state; retain the marker on every failure."""
        if not self.pending_path.exists():
            return None
        try:
            journal = self.pending_path.read_bytes()
            marker, separator, prior_bytes = journal.partition(b"\n")
            if not separator or marker not in {b"present", b"absent"}:
                raise OSError("malformed project trust recovery journal")
            if marker == b"present":
                self._atomic_replace(self.path, prior_bytes, prefix=".trust-rollback-")
            else:
                self.path.unlink(missing_ok=True)
                _fsync_directory(self.paths.home)
            self.pending_path.unlink()
            with suppress(OSError):
                _fsync_directory(self.paths.home)
        except OSError as exc:
            return exc
        return None


def is_project_trusted(cwd: Path, paths: RioPaths | None = None) -> bool:
    """Read the nearest saved MCP decision, otherwise the default. Errors deny trust."""
    paths = paths or RioPaths()
    try:
        canonical = canonicalize_project_path(cwd, base=Path.cwd())
        saved = ProjectTrustStore(paths).nearest(canonical)
        if saved is not None:
            return saved.decision == "trusted"
        settings_path = paths.home / "settings.json"
        if not settings_path.exists():
            return False
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            return False
        # 'ask' is retained for old settings; one-shot commands do not prompt.
        default = settings.get("defaultProjectTrust", "ask")
        return isinstance(default, str) and default == "always"
    except (ProjectTrustError, OSError, ValueError):
        return False


def _lock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise ProjectTrustError(f"Could not acquire project trust lock: {exc}") from exc


def _unlock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
