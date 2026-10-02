"""rio.agent: a Context Language Model (CLM) agent runtime (arXiv:2609.37725).

The model natively manages its own context. The context is a list of turns
that the runtime mirrors to a file before every step; the model edits that
file with its ordinary tools, and the edited file becomes its next context.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.agent.context import Turn, parse_context, render_context, turn
from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.agent.events import (
    ActionEndEvent,
    ActionStartEvent,
    AgentEvent,
    ContextEditEvent,
    ReasoningEvent,
    RunEndEvent,
    RunStartEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.agent.harness import EventListener, Harness, HarnessCancellationToken, HarnessConfig
from rio.agent.loop import context_limit, run_context_loop
from rio.agent.prompt import build_messages, context_protocol
from rio.agent.skill import HarnessSpec

__all__ = [name for name in globals() if not name.startswith("_")]
