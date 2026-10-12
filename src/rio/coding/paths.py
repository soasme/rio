"""Canonical filesystem paths for rio user data."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True, slots=True)
class RioPaths:
    """Resolved rio filesystem locations.

    rio keeps durable application data under the user's home directory.
    """

    home: Path = field(default_factory=lambda: Path.home() / ".rio")

    @property
    def sessions_dir(self) -> Path:
        """Return the user-level session directory."""
        return self.home / "sessions"


def atomic_write_text(path: Path, text: str) -> None:
    """Write text through a sibling temp file and atomically replace the target."""
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()
        raise
