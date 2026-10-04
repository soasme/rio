"""Notebook coding agent built on JSON state and patching.

The model manages its own context, and the context is a Jupyter notebook. Each
step the model patches the notebook with RFC 6902 JSON Patch; the runtime
checks the result is a valid notebook, runs the changed code cells, and sends
the notebook with their outputs back as the next context.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.coding.agent_events import (
    AgentEvent,
    ExecutionEvent,
    PatchEvent,
    ReasoningEvent,
    RunEndEvent,
    RunStartEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.coding.harness import EventListener, Harness, HarnessCancellationToken, HarnessConfig
from rio.coding.loop import context_limit, run_notebook_loop
from rio.coding.notebook import (
    KernelExecutor,
    Notebook,
    NotebookError,
    NotebookExecutor,
    apply_patch,
    changed_cells,
    diff,
    markdown_cell,
    new_notebook,
    notebook_tokens,
    render_notebook,
)
from rio.coding.prompt import STEP_TOOL_NAME, build_messages, notebook_protocol, skill_step_tool
from rio.coding.skill import HarnessSpec

__all__ = [name for name in globals() if not name.startswith("_")]
