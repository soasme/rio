from __future__ import annotations

import contextlib
import io
import itertools
from collections.abc import Iterator

import pytest

from rio.agent import HarnessSpec, Notebook
from rio.agent.notebook import with_kernel
from rio.ai import AssistantDoneEvent, AssistantMessage, TextContent, ToolCall
from rio.ai.types import JSONObject
from rio.coding import catalog_loader, models_dev, models_dev_store, provider_catalog

_call_ids = itertools.count()
_REAL_BUNDLED_LOADER = models_dev.bundled_models_dev_catalog_document


def add_code(source: str) -> JSONObject:
    """A patch operation that appends a code cell."""
    return {"op": "add", "path": "/cells/-", "value": {"cell_type": "code", "source": source}}


def add_markdown(source: str) -> JSONObject:
    """A patch operation that appends a markdown cell."""
    return {"op": "add", "path": "/cells/-", "value": {"cell_type": "markdown", "source": source}}


def step_response(*, reasoning: str = "", patch: list | None = None, reply: str | None = None):
    """Build one FakeProvider stream: reasoning text and a single `skill_step` call."""
    content = []
    if reasoning:
        content.append(TextContent(text=reasoning))
    arguments: JSONObject = {"patch": list(patch or [])}
    if reply is not None:
        arguments["reply"] = reply
    content.append(ToolCall(id=f"call-{next(_call_ids)}", name="skill_step", arguments=arguments))
    message = AssistantMessage(content=content, stop_reason="toolUse")
    return [AssistantDoneEvent(reason="toolUse", message=message)]


class FakeExecutor:
    """Run changed code cells in-process with `exec`, in one namespace per "kernel"."""

    def __init__(self) -> None:
        self.kernel_id = "k1"
        self.is_running = False
        self.namespace: dict = {}

    def restart(self, kernel_id: str) -> None:
        self.kernel_id, self.is_running, self.namespace = kernel_id, False, {}

    async def __call__(self, notebook: Notebook, changed: list[int]) -> Notebook:
        self.is_running = True
        for index in changed:
            cell = notebook["cells"][index]
            stdout = io.StringIO()
            before = {name: id(value) for name, value in self.namespace.items()}
            try:
                with contextlib.redirect_stdout(stdout):
                    exec(cell["source"], self.namespace)
                outputs = [{"output_type": "stream", "name": "stdout", "text": stdout.getvalue()}]
            except Exception as exc:
                outputs = [
                    {
                        "output_type": "error",
                        "ename": type(exc).__name__,
                        "evalue": str(exc),
                        "traceback": [],
                    }
                ]
            cell["outputs"] = outputs
            cell["execution_count"] = index + 1
            defines = sorted(
                name
                for name, value in self.namespace.items()
                if not name.startswith("_") and before.get(name) != id(value)
            )
            cell["metadata"]["rio"] = {"kernel": self.kernel_id, "defines": defines}
        return with_kernel(notebook, self.kernel_id, running=True)


def make_skill(**overrides) -> HarnessSpec:
    defaults = dict(name="demo", instructions="You are the demo skill.", executor=FakeExecutor())
    defaults.update(overrides)
    return HarnessSpec(**defaults)


def _clear_catalog_caches() -> None:
    catalog_loader.builtin_catalog.cache_clear()
    catalog_loader._builtin_raw_with_generated_models.cache_clear()
    provider_catalog._load_builtin_catalog.cache_clear()


@contextlib.contextmanager
def _bundled_models_dev_catalog(enabled: bool) -> Iterator[None]:
    loader = _REAL_BUNDLED_LOADER if enabled else lambda: None
    with pytest.MonkeyPatch.context() as monkeypatch:
        for module in (models_dev, models_dev_store):
            monkeypatch.setattr(module, "bundled_models_dev_catalog_document", loader)
        _clear_catalog_caches()
        try:
            yield
        finally:
            _clear_catalog_caches()


@pytest.fixture(scope="session", autouse=True)
def _without_bundled_models_dev_catalog() -> Iterator[None]:
    """Keep the regenerated models.dev snapshot out of tests.

    The snapshot mirrors upstream data that changes on every refresh, so tests
    see only the repo-owned catalog.toml. Tests of the snapshot itself use the
    `bundled_models_dev_catalog` fixture and check its schema, not its values.
    """
    with _bundled_models_dev_catalog(enabled=False):
        yield


@pytest.fixture
def bundled_models_dev_catalog() -> Iterator[None]:
    """Load the real bundled models.dev snapshot for this test."""
    with _bundled_models_dev_catalog(enabled=True):
        yield
