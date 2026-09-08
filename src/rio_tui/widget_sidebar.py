"""A collapsible plan and project browser beside the conversation."""

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Collapsible, DirectoryTree, Static


class ProjectTree(DirectoryTree):
    # Textual defaults to emoji here; ASCII keeps every row one cell per column.
    ICON_NODE_EXPANDED = "- "
    ICON_NODE = "+ "
    ICON_FILE = "  "

    def filter_paths(self, paths):
        hidden = {
            ".git",
            ".venv",
            ".worktrees",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
        }
        return [path for path in paths if path.name not in hidden]


class Plan(Static):
    def update_state(self, state):
        plan = state.get("plan", [])
        if isinstance(plan, dict):
            plan = plan.get("steps", plan.get("items", []))
        if isinstance(plan, str):
            plan = [plan]
        lines = Text()
        if isinstance(plan, list):
            for item in plan:
                if isinstance(item, dict):
                    title = item.get("content") or item.get("description") or item.get("title", "")
                    status = item.get("status", "pending")
                else:
                    title, status = str(item), "pending"
                complete = status in {"completed", "done"}
                marker = "✓" if complete else "›" if status in {"in_progress", "running"} else "·"
                lines.append(f"{marker} {title}\n", style="dim strike" if complete else "")
        if not lines:
            lines.append("No plan yet", style="dim italic")
        self.update(lines)


class Sidebar(Vertical):
    def __init__(self, cwd):
        super().__init__(id="sidebar")
        self.cwd = cwd

    def compose(self):
        yield Collapsible(Plan(id="plan"), title="Plan", collapsed=False, classes="plan-panel")
        yield Collapsible(
            ProjectTree(self.cwd), title="Project", collapsed=False, classes="project-panel"
        )
        yield Vertical(id="extension-sidebar")
