"""Selectable conversation blocks and per-tool disclosure."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import HorizontalGroup, VerticalGroup, VerticalScroll
from textual.widgets import Collapsible, Label, Markdown, Static
from textual_diff_view import DiffView

from rio_coding.tools import describe_action


class UserMessage(HorizontalGroup):
    def __init__(self, text):
        super().__init__(classes="conversation-block")
        self.text = text

    def compose(self) -> ComposeResult:
        yield Label("❯", classes="prompt-mark")
        yield Markdown(self.text)


class Answer(Markdown):
    def __init__(self, text):
        super().__init__(text, classes="conversation-block")
        self.text = text


class Thought(Collapsible):
    def __init__(self, text):
        super().__init__(
            Markdown(text), title="Thinking", collapsed=True, classes="conversation-block thought"
        )


class Notice(Static):
    def __init__(self, text, *, error=False):
        super().__init__(
            Text(text), classes="conversation-block error" if error else "conversation-block notice"
        )
        self.text = text


class ToolBlock(Collapsible):
    """One tool invocation, updated in place when its result arrives."""

    def __init__(self, name, arguments, *, cwd=None):
        self.tool_name = name
        self.arguments = arguments
        self.cwd = cwd
        self.output = ""
        self.result = VerticalGroup()
        self.label = f"{name} {describe_action(name, arguments, cwd=cwd)}".strip()
        super().__init__(
            self.result,
            title=f"{self.label} · running",
            collapsed=True,
            classes="conversation-block tool-block",
        )

    async def complete(self, output, *, error=False, diff=None):
        self.output = output
        self.title = f"{self.label} · {'failed' if error else 'done'}"
        self.set_class(error, "error")
        self.set_class(not error, "success")
        if diff:
            before, after = diff
            layout = self.app.settings.diff_layout
            path = describe_action(self.tool_name, self.arguments, cwd=self.cwd) or "file"
            await self.result.mount(
                DiffView(
                    path,
                    path,
                    before,
                    after,
                    split=layout == "split",
                    auto_split=layout == "auto",
                    wrap=False,
                )
            )
        elif output:
            if self.tool_name in {"bash", "read"}:
                # Tool output remains literal; only assistant answers are Markdown.
                await self.result.mount(Static(Text.from_ansi(output)))
            else:
                await self.result.mount(Markdown(output))
        if error:
            self.collapsed = False


class Conversation(VerticalScroll):
    """Blocks grow upward from the prompt; scrolling up suspends auto-follow."""

    def compose(self):
        yield VerticalGroup(id="blocks")

    async def add(self, widget):
        follow = self.is_vertical_scroll_end
        blocks = self.query_one("#blocks", VerticalGroup)
        await blocks.mount(widget)
        if len(blocks.children) > 500:
            await blocks.children[0].remove()
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        return widget

    async def clear(self):
        await self.query_one("#blocks").remove_children()

    def on_click(self, event):
        if event.button != 3:
            return
        from rio_tui.dialogs import Picker

        candidates = [event.widget, *event.widget.ancestors]
        block = next((w for w in candidates if w.has_class("conversation-block")), None)
        if block is None:
            return
        text = getattr(block, "text", None) or getattr(block, "output", "")
        choices = [("Copy", "copy")]
        if isinstance(block, UserMessage):
            choices.append(("Edit prompt", "edit"))
        if isinstance(block, ToolBlock):
            choices.append(("Expand / collapse", "toggle"))

        def selected(action):
            if action == "copy":
                self.app.copy_to_clipboard(text)
            elif action == "edit":
                editor = self.screen.query_one("#editor")
                editor.text = text
                editor.focus()
            elif action == "toggle":
                block.collapsed = not block.collapsed

        self.app.push_screen(Picker("Message", choices), selected)
        event.stop()
