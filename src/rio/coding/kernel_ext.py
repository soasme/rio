"""Loaded into every rio kernel: tracks the names cells define and stands in for stale ones.

After each cell runs, `get_ipython()._rio_defined` lists the names the cell
bound or rebound, found by comparing the namespace before and after it. The
executor reads it and stamps the cell with it.

In a new kernel, a name that only stale cells defined is bound to a
`StaleValue`. Using it raises `StaleVariableError`, which names the cell to
run again, instead of letting the cell run with a missing or wrong value.
"""

from __future__ import annotations

from typing import Any, NoReturn


class StaleVariableError(NameError):
    """A cell used a variable that only exists in an older kernel."""

    def _render_traceback_(self) -> list[str]:
        # IPython shows this instead of a traceback through the placeholder.
        return [f"StaleVariableError: {self}"]


class StaleValue:
    """Placeholder for a variable a stale cell defined. Almost any use of it raises."""

    __slots__ = ("_rio_name", "_rio_cell")

    def __init__(self, name: str, cell: int) -> None:
        object.__setattr__(self, "_rio_name", name)
        object.__setattr__(self, "_rio_cell", cell)

    def _rio_fail(self, *args: Any, **kwargs: Any) -> NoReturn:
        name, cell = self._rio_name, self._rio_cell
        raise StaleVariableError(
            f"`{name}` was defined by cell {cell}, which ran in an older kernel. Run cell "
            f"{cell} again (remove its metadata.rio.kernel stamp) or define `{name}` again."
        )

    def __getattr__(self, attribute: str) -> NoReturn:
        self._rio_fail()


_DUNDERS = [
    "__setattr__",
    "__delattr__",
    "__call__",
    "__bool__",
    "__len__",
    "__iter__",
    "__next__",
    "__contains__",
    "__getitem__",
    "__setitem__",
    "__delitem__",
    "__repr__",
    "__str__",
    "__format__",
    "__bytes__",
    "__hash__",
    "__eq__",
    "__ne__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__add__",
    "__radd__",
    "__sub__",
    "__rsub__",
    "__mul__",
    "__rmul__",
    "__truediv__",
    "__rtruediv__",
    "__floordiv__",
    "__rfloordiv__",
    "__mod__",
    "__rmod__",
    "__pow__",
    "__rpow__",
    "__matmul__",
    "__and__",
    "__or__",
    "__xor__",
    "__lshift__",
    "__rshift__",
    "__neg__",
    "__pos__",
    "__abs__",
    "__invert__",
    "__int__",
    "__float__",
    "__complex__",
    "__index__",
    "__round__",
    "__enter__",
    "__exit__",
    "__aenter__",
    "__aexit__",
    "__await__",
    "__aiter__",
    "__anext__",
    "__fspath__",
]
for _name in _DUNDERS:
    setattr(StaleValue, _name, StaleValue._rio_fail)


def load_ipython_extension(ipython: Any) -> None:
    hidden = set(ipython.user_ns_hidden)

    def snapshot() -> dict[str, int]:
        return {
            name: id(value)
            for name, value in ipython.user_ns.items()
            if not name.startswith("_") and name not in hidden
        }

    before: dict[str, int] = {}

    def pre_run_cell(info: Any) -> None:
        before.clear()
        before.update(snapshot())

    def post_run_cell(result: Any) -> None:
        ipython._rio_defined = sorted(
            name
            for name, identity in snapshot().items()
            if before.get(name) != identity and not isinstance(ipython.user_ns[name], StaleValue)
        )

    def install_stale(names: dict[str, int]) -> None:
        for name, cell in names.items():
            if name not in ipython.user_ns:
                ipython.user_ns[name] = StaleValue(name, cell)

    ipython._rio_defined = []
    ipython._rio_install_stale = install_stale
    ipython.events.register("pre_run_cell", pre_run_cell)
    ipython.events.register("post_run_cell", post_run_cell)
