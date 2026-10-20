from pathlib import Path


def resolve_inside(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("absolute")
    root = Path(root).resolve()
    result = root / relative
    if not result.is_relative_to(root):
        raise ValueError("escape")
    return result
