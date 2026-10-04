"""HarnessSpec: the fixed instructions for a domain and how its notebook runs."""

from __future__ import annotations

from dataclasses import dataclass

from rio.coding.notebook import NotebookExecutor


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    name: str
    instructions: str
    #: Runs the changed cells of a patched notebook, e.g. a `KernelExecutor`.
    #: The caller owns its lifetime.
    executor: NotebookExecutor
