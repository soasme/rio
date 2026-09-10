"""Application preferences, independent of the coding runtime."""

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class Settings:
    theme: str = "textual-dark"
    sidebar: bool = False
    column: bool = True
    thoughts: bool = True
    notifications: bool = False
    diff_layout: str = "auto"

    @classmethod
    def load(cls, path: Path):
        try:
            values = json.loads(path.read_text())
            if not isinstance(values, dict):
                return cls()
            defaults = cls()
            valid = {
                f.name: values[f.name]
                for f in fields(cls)
                if f.name in values and type(values[f.name]) is type(getattr(defaults, f.name))
            }
            if valid.get("diff_layout", "auto") not in {"auto", "unified", "split"}:
                valid.pop("diff_layout")
            return cls(**valid)
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile("w", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(asdict(self), file, indent=2)
                file.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
