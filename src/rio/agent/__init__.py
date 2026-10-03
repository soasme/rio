"""rio.agent: a Context Language Model (CLM) agent runtime (arXiv:2609.37725).

The model manages its own context, and the context is a Jupyter notebook. Each
step the model patches the notebook with RFC 6902 JSON Patch; the runtime
checks the result is a valid notebook, runs the changed code cells, and sends
the notebook with their outputs back as the next context.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.agent.events import (
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
from rio.agent.harness import EventListener, Harness, HarnessCancellationToken, HarnessConfig
from rio.agent.loop import context_limit, run_context_loop
from rio.agent.notebook import (
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
from rio.agent.prompt import build_messages, system_prompt
from rio.agent.spec import STEP_TOOL, STEP_TOOL_NAME, HarnessSpec, StepArgs

__all__ = [name for name in globals() if not name.startswith("_")]
