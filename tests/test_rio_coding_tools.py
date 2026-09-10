from __future__ import annotations

import asyncio
import base64
import shlex
from io import BytesIO
from pathlib import Path
from time import monotonic

import pytest
from PIL import Image

import rio.coding.image_processing as image_processing
from rio.ai import ImageContent
from rio.coding.image_processing import (
    DEFAULT_MAX_SOURCE_IMAGE_BYTES,
    ImageProcessingFailure,
    ProcessedImage,
    detect_supported_image_mime_type,
    process_image,
    unsupported_image_reason,
)
from rio.coding.tools import (
    ImageSupportState,
    ReadOperations,
    create_bash_tool,
    create_bash_tool_definition,
    create_coding_tools,
    create_edit_tool,
    create_edit_tool_definition,
    create_read_tool,
    create_read_tool_definition,
    create_respond_tool,
    create_write_tool,
    describe_action,
)


def image_bytes(format_name: str = "PNG", *, size: tuple[int, int] = (8, 6)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "navy").save(output, format=format_name)
    return output.getvalue()


def animated_png_bytes() -> bytes:
    png = image_bytes()
    idat_offset = png.index(b"IDAT") - 4
    animated_chunk = b"\x00\x00\x00\x08acTL\x00\x00\x00\x02\x00\x00\x00\x00" + b"\x00" * 4
    return png[:idat_offset] + animated_chunk + png[idat_offset:]


class FakeCancellationToken:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def is_cancelled(self) -> bool:
        return self.cancelled


# --- tool factory / schema ---------------------------------------------------


async def test_create_coding_tools_returns_initial_tool_set(tmp_path: Path) -> None:
    tools = create_coding_tools(cwd=tmp_path)

    assert [tool.name for tool in tools] == ["read", "write", "edit", "bash", "respond"]
    edit_tool = tools[2]
    assert edit_tool.prompt_snippet is not None
    assert "Use edit for precise changes" in edit_tool.prompt_guidelines[0]
    assert "present-participle description" in tools[3].prompt_guidelines[0]


async def test_create_coding_tools_can_exclude_respond(tmp_path: Path) -> None:
    tools = create_coding_tools(cwd=tmp_path, include_respond=False)

    assert [tool.name for tool in tools] == ["read", "write", "edit", "bash"]


def test_bash_tool_schema_requires_display_description(tmp_path: Path) -> None:
    definition = create_bash_tool_definition(cwd=tmp_path)
    properties = definition.input_schema["properties"]

    assert isinstance(properties, dict)
    assert properties["description"]["type"] == "string"
    assert "present-participle summary" in properties["description"]["description"]
    assert definition.input_schema["required"] == ["command", "description"]


def test_tool_definitions_expose_pi_style_prompt_metadata(tmp_path: Path) -> None:
    definition = create_edit_tool_definition(cwd=tmp_path)

    assert definition.prompt_snippet.startswith("Make precise file edits")
    assert len(definition.prompt_guidelines) == 4


def test_read_tool_schema_defines_line_controls_as_integers(tmp_path: Path) -> None:
    definition = create_read_tool_definition(cwd=tmp_path)
    properties = definition.input_schema["properties"]

    assert isinstance(properties, dict)
    assert properties["offset"]["type"] == "integer"
    assert properties["limit"]["type"] == "integer"


# --- read tool ---------------------------------------------------------------


async def test_read_tool_reads_file_with_offset_and_limit(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "notes.txt", "offset": 2, "limit": 1})

    assert result.text == (
        f"read {path} (lines 2-2 of 4)\n\ntwo\n\n[2 more lines in file. Use offset=3 to continue.]"
    )
    assert result.details is not None
    assert result.details["path"] == str(path)
    assert isinstance(result.details["truncation"], dict)


async def test_read_tool_returns_images_as_model_content(tmp_path: Path) -> None:
    image_data = image_bytes()
    path = tmp_path / "diagram.png"
    path.write_bytes(image_data)
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "diagram.png"})

    assert result.text == f"read {path}\n\nRead image file [image/png]"
    image = next(block for block in result.content if isinstance(block, ImageContent))
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data) == image_data
    assert result.details == {
        "path": str(path),
        "source_mime_type": "image/png",
        "mime_type": "image/png",
        "bytes": len(image_data),
        "processed_bytes": len(image_data),
        "width": 8,
        "height": 6,
    }


async def test_read_tool_detects_images_by_content_not_extension(tmp_path: Path) -> None:
    path = tmp_path / "diagram.txt"
    path.write_bytes(image_bytes())
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "diagram.txt"})

    assert any(isinstance(block, ImageContent) for block in result.content)


async def test_read_tool_resizes_over_dimension_images(tmp_path: Path) -> None:
    path = tmp_path / "large.png"
    path.write_bytes(image_bytes(size=(2_500, 100)))
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "large.png"})

    assert result.text.startswith(f"read {path}\n\n")
    assert "Image resized from 2500x100 to 2000x80" in result.text
    image = next(block for block in result.content if isinstance(block, ImageContent))
    with Image.open(BytesIO(base64.b64decode(image.data))) as processed:
        assert processed.size == (2_000, 80)


async def test_read_tool_explicitly_omits_images_for_text_only_model(tmp_path: Path) -> None:
    path = tmp_path / "galaxy.png"
    path.write_bytes(image_bytes())
    image_support = ImageSupportState(supported=False)
    tool = create_read_tool(cwd=tmp_path, image_support=image_support)

    omitted = await tool.execute("test-call", {"path": "galaxy.png"})

    assert omitted.text.startswith(f"read {path}\n\n")
    assert "current model does not support image input" in omitted.text
    assert "do not infer or describe" in omitted.text
    assert "switch to a vision-capable model" in omitted.text
    assert not any(isinstance(block, ImageContent) for block in omitted.content)

    image_support.supported = True
    attached = await tool.execute("test-call", {"path": "galaxy.png"})

    assert any(isinstance(block, ImageContent) for block in attached.content)


async def test_read_tool_converts_bmp_to_png(tmp_path: Path) -> None:
    path = tmp_path / "legacy.bmp"
    path.write_bytes(image_bytes("BMP"))
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "legacy.bmp"})

    assert result.text.startswith(f"read {path}\n\n")
    assert "Read image file [image/png]" in result.text
    assert "Image converted from image/bmp to image/png" in result.text
    image = next(block for block in result.content if isinstance(block, ImageContent))
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data).startswith(b"\x89PNG\r\n\x1a\n")
    assert result.details is not None
    assert result.details["source_mime_type"] == "image/bmp"


async def test_read_tool_reports_decode_failure_without_attachment(tmp_path: Path) -> None:
    malformed = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR" + b"\x00" * 17 + b"\x00\x00\x00\x00IDAT" + b"\x00" * 4
    )
    (tmp_path / "broken.png").write_bytes(malformed)
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "broken.png"})

    assert "Image omitted: could not decode a valid image" in result.text
    assert not any(isinstance(block, ImageContent) for block in result.content)


@pytest.mark.parametrize(
    ("filename", "data", "reason"),
    [
        ("animated.png", animated_png_bytes(), "animated PNG images are not supported"),
        ("image.jxl", b"\xff\xd8\xff\xf7not-jpeg", "JPEG XL images are not supported"),
    ],
)
async def test_read_tool_reports_known_unsupported_image_variants(
    tmp_path: Path, filename: str, data: bytes, reason: str
) -> None:
    (tmp_path / filename).write_bytes(data)
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": filename})

    assert reason in result.text
    assert not any(isinstance(block, ImageContent) for block in result.content)


async def test_read_tool_rejects_oversized_image_from_prefix_before_full_read(
    tmp_path: Path,
) -> None:
    def unexpected_full_read(path: Path) -> bytes:
        raise AssertionError(f"unexpected full read: {path}")

    source_size = DEFAULT_MAX_SOURCE_IMAGE_BYTES + 1
    operations = ReadOperations(
        validate_path=lambda path: None,
        read_bytes=unexpected_full_read,
        size_bytes=lambda path: source_size,
        read_prefix=lambda path, limit: image_bytes()[:limit],
    )
    tool = create_read_tool(cwd=tmp_path, operations=operations)

    result = await tool.execute("test-call", {"path": "huge.png"})

    assert "exceeding the 50.0MB processing limit" in result.text
    assert not any(isinstance(block, ImageContent) for block in result.content)
    assert result.details is not None
    assert result.details["bytes"] == source_size


async def test_read_tool_still_reads_large_text_files(tmp_path: Path) -> None:
    operations = ReadOperations(
        validate_path=lambda path: None,
        read_bytes=lambda path: b"large text",
        size_bytes=lambda path: DEFAULT_MAX_SOURCE_IMAGE_BYTES + 1,
        read_prefix=lambda path, limit: b"large text"[:limit],
    )
    tool = create_read_tool(cwd=tmp_path, operations=operations)

    result = await tool.execute("test-call", {"path": "large.txt"})

    path = tmp_path / "large.txt"
    assert result.text == f"read {path} (lines 1-1 of 1)\n\nlarge text"


async def test_read_tool_uses_pluggable_read_operations(tmp_path: Path) -> None:
    reads: list[Path] = []
    operations = ReadOperations(
        validate_path=lambda path: None,
        read_bytes=lambda path: reads.append(path) or b"remote text",
    )
    tool = create_read_tool(cwd=tmp_path, operations=operations)

    result = await tool.execute("test-call", {"path": "not-local.txt"})

    path = tmp_path / "not-local.txt"
    assert result.text == f"read {path} (lines 1-1 of 1)\n\nremote text"
    assert reads == [path]


async def test_read_tool_treats_zero_offset_as_start_of_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "notes.txt", "offset": 0, "limit": 1})

    assert result.text == (
        f"read {path} (lines 1-1 of 4)\n\none\n\n[3 more lines in file. Use offset=2 to continue.]"
    )


async def test_read_tool_header_names_action_path_and_line_range(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "notes.txt"})

    assert result.text.startswith(f"read {path} (lines 1-4 of 4)\n\n")


@pytest.mark.parametrize("alias", ["file", "file_path", "filepath", "filename"])
async def test_read_tool_accepts_path_aliases(tmp_path: Path, alias: str) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {alias: "notes.txt"})

    assert result.text == f"read {path} (lines 1-2 of 2)\n\none\n"
    assert result.details is not None
    assert result.details["path"] == str(path)


async def test_read_tool_prefers_path_over_alias(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\n")
    (tmp_path / "other.txt").write_text("two\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "notes.txt", "file": "other.txt"})

    assert result.text.startswith(f"read {tmp_path / 'notes.txt'} ")


async def test_read_tool_rejects_missing_path(tmp_path: Path) -> None:
    tool = create_read_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="path must be a string; accepted argument names: "):
        await tool.execute("test-call", {})


# --- read tool: batch `files` -------------------------------------------------


async def test_read_tool_batch_reads_multiple_files_as_separate_blocks(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("bravo\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"files": ["a.txt", "b.txt"]})

    path_a = tmp_path / "a.txt"
    path_b = tmp_path / "b.txt"
    assert result.text == (
        f"read {path_a} (lines 1-2 of 2)\n\nalpha\n\n\nread {path_b} (lines 1-2 of 2)\n\nbravo\n"
    )
    assert len(result.content) == 2
    assert result.details is not None
    assert [entry["path"] for entry in result.details["files"]] == [str(path_a), str(path_b)]


async def test_read_tool_batch_applies_shared_offset_and_limit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    (tmp_path / "b.txt").write_text("uno\ndos\ntres\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"files": ["a.txt", "b.txt"], "offset": 2, "limit": 1})

    assert "two" in result.text
    assert "dos" in result.text
    assert "three" not in result.text
    assert "tres" not in result.text


async def test_read_tool_batch_propagates_error_for_bad_entry(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n")
    tool = create_read_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="File not found"):
        await tool.execute("test-call", {"files": ["a.txt", "missing.txt"]})


async def test_read_tool_batch_rejects_empty_files_list(tmp_path: Path) -> None:
    tool = create_read_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="files must be a non-empty list of path strings"):
        await tool.execute("test-call", {"files": []})


async def test_read_tool_batch_rejects_non_string_entries(tmp_path: Path) -> None:
    tool = create_read_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="files must be a list of path strings"):
        await tool.execute("test-call", {"files": [1, "a.txt"]})


async def test_read_tool_prefers_path_over_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("bravo\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "a.txt", "files": ["b.txt"]})

    assert result.text.startswith(f"read {tmp_path / 'a.txt'} ")
    assert "bravo" not in result.text


async def test_write_tool_rejects_non_string_path(tmp_path: Path) -> None:
    tool = create_write_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="path must be a string"):
        await tool.execute("test-call", {"path": 1, "content": "hello"})


# --- write tool ----------------------------------------------------------


async def test_write_tool_creates_parent_directories(tmp_path: Path) -> None:
    tool = create_write_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"path": "nested/file.txt", "content": "hello"})

    path = tmp_path / "nested" / "file.txt"
    assert result.text == f"write {path} (5 characters)\n\nSuccessfully wrote to {path}."
    assert path.read_text() == "hello"


@pytest.mark.parametrize("alias", ["file", "file_path", "filepath", "filename"])
async def test_write_tool_accepts_path_aliases(tmp_path: Path, alias: str) -> None:
    tool = create_write_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {alias: "file.txt", "content": "hello"})

    path = tmp_path / "file.txt"
    assert result.text == f"write {path} (5 characters)\n\nSuccessfully wrote to {path}."
    assert path.read_text() == "hello"


# --- edit tool -------------------------------------------------------------


async def test_edit_tool_applies_multiple_exact_replacements(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("alpha\nbeta\ngamma\n")
    tool = create_edit_tool(cwd=tmp_path)

    result = await tool.execute(
        "test-call",
        {
            "path": "file.txt",
            "edits": [
                {"oldText": "alpha", "newText": "one"},
                {"oldText": "gamma", "newText": "three"},
            ],
        },
    )

    assert result.text == (
        f"edit {path} (2 edit(s))\n\nSuccessfully replaced 2 block(s) in {path}."
    )
    assert path.read_text() == "one\nbeta\nthree\n"


@pytest.mark.parametrize("alias", ["file", "file_path", "filepath", "filename"])
async def test_edit_tool_accepts_path_aliases(tmp_path: Path, alias: str) -> None:
    path = tmp_path / "file.txt"
    path.write_text("alpha\n")
    tool = create_edit_tool(cwd=tmp_path)

    result = await tool.execute(
        "test-call",
        {alias: "file.txt", "edits": [{"oldText": "alpha", "newText": "one"}]},
    )

    assert result.text == (
        f"edit {path} (1 edit(s))\n\nSuccessfully replaced 1 block(s) in {path}."
    )
    assert path.read_text() == "one\n"


async def test_edit_tool_rejects_missing_path(tmp_path: Path) -> None:
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="path must be a string"):
        await tool.execute("test-call", {"edits": [{"oldText": "a", "newText": "b"}]})


async def test_edit_tool_rolls_back_when_any_edit_fails(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    original = "alpha\nbeta\ngamma\n"
    path.write_text(original)
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="Could not find edits\\[1\\]"):
        await tool.execute(
            "test-call",
            {
                "path": "file.txt",
                "edits": [
                    {"oldText": "alpha", "newText": "one"},
                    {"oldText": "missing", "newText": "nope"},
                ],
            },
        )

    assert path.read_text() == original


async def test_edit_tool_requires_unique_matches(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("repeat\nrepeat\n")
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="Found 2 occurrences"):
        await tool.execute(
            "test-call",
            {
                "path": "file.txt",
                "edits": [{"oldText": "repeat", "newText": "once"}],
            },
        )


# --- bash tool ---------------------------------------------------------------


async def test_bash_tool_tolerates_missing_required_display_description(tmp_path: Path) -> None:
    tool = create_bash_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"command": "printf hello"})

    assert result.text == "bash printf hello (exit 0)\n\nhello"
    assert result.details is not None
    assert result.details["exit_code"] == 0
    assert result.details["timed_out"] is False


@pytest.mark.parametrize("alias", ["cmd", "shell_command", "bash_command"])
async def test_bash_tool_accepts_command_aliases(tmp_path: Path, alias: str) -> None:
    tool = create_bash_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {alias: "printf hello"})

    assert result.text == "bash printf hello (exit 0)\n\nhello"
    assert result.details is not None
    assert result.details["command"] == "printf hello"


async def test_bash_tool_prefers_command_over_alias(tmp_path: Path) -> None:
    tool = create_bash_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"command": "printf hello", "cmd": "printf goodbye"})

    assert result.text == "bash printf hello (exit 0)\n\nhello"


async def test_bash_tool_rejects_missing_command(tmp_path: Path) -> None:
    tool = create_bash_tool(cwd=tmp_path)

    with pytest.raises(ValueError, match="command must be a string"):
        await tool.execute("test-call", {"description": "Doing nothing"})


async def test_create_coding_tools_applies_shell_command_prefix(
    tmp_path: Path,
) -> None:
    tools = create_coding_tools(
        cwd=tmp_path,
        shell_command_prefix="shopt -s expand_aliases\nalias greet='printf coding-tool-alias'",
    )
    bash_tool = next(tool for tool in tools if tool.name == "bash")

    result = await bash_tool.execute("test-call", {"command": "greet"})

    assert result.text == "bash greet (exit 0)\n\ncoding-tool-alias"
    assert result.details is not None
    assert result.details["shell_command_prefix_applied"] is True


async def test_bash_tool_applies_opt_in_shell_command_prefix(tmp_path: Path) -> None:
    rc_path = tmp_path / ".zshrc"
    marker = tmp_path / "sourced"
    rc_path.write_text(
        f"alias greet='printf alias-output'\ntouch {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    prefix = f"shopt -s expand_aliases\neval \"$(grep '^alias ' {shlex.quote(str(rc_path))})\""
    tool = create_bash_tool(cwd=tmp_path, shell_command_prefix=prefix)

    result = await tool.execute("test-call", {"command": "greet"})

    assert result.text == "bash greet (exit 0)\n\nalias-output"
    assert result.details is not None
    assert result.details["shell_command_prefix_applied"] is True
    assert not marker.exists()


async def test_bash_tool_reports_timeout(tmp_path: Path) -> None:
    tool = create_bash_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"command": "sleep 1", "timeout": 0.01})

    assert result.details is not None
    assert result.details["timed_out"] is True
    assert result.text.startswith("bash sleep 1 ")
    assert "timed out" in result.text


async def test_bash_tool_timeout_kills_shell_children(tmp_path: Path) -> None:
    tool = create_bash_tool(cwd=tmp_path)
    marker = tmp_path / "marker"

    start = monotonic()
    result = await tool.execute(
        "test-call", {"command": "(sleep 0.25; touch marker) & wait", "timeout": 0.01}
    )
    duration = monotonic() - start
    await asyncio.sleep(0.35)

    assert result.details is not None
    assert result.details["timed_out"] is True
    assert duration < 0.5
    assert not marker.exists()


async def test_bash_tool_cancellation_kills_shell_children(tmp_path: Path) -> None:
    tool = create_bash_tool(cwd=tmp_path)
    token = FakeCancellationToken()

    task = asyncio.create_task(
        tool.execute("test-call", {"command": "sleep 1 & wait"}, signal=token)
    )
    await asyncio.sleep(0.05)
    token.cancel()
    start = monotonic()
    result = await task
    duration = monotonic() - start

    assert result.details is not None
    assert result.details["cancelled"] is True
    assert "cancelled" in result.text
    assert duration < 0.5


async def test_bash_tool_header_truncates_long_commands(tmp_path: Path) -> None:
    long_command = "printf " + "x" * 100
    tool = create_bash_tool(cwd=tmp_path)

    result = await tool.execute("test-call", {"command": long_command})

    header_line = result.text.splitlines()[0]
    assert header_line.startswith("bash printf xxx")
    assert header_line.endswith("(exit 0)")
    assert "..." in header_line
    assert len(header_line) < len(long_command)


# --- respond tool --------------------------------------------------------


async def test_respond_tool_terminates_the_run_with_the_final_message() -> None:
    tool = create_respond_tool()

    result = await tool.execute("test-call", {"message": "The tests are green."})

    assert result.terminate is True
    assert result.text == "The tests are green."


async def test_respond_tool_requires_a_string_message() -> None:
    tool = create_respond_tool()

    with pytest.raises(ValueError, match="message must be a string"):
        await tool.execute("test-call", {})


# --- image_processing (ported directly, no SKILL.state changes needed) ------


def image_processing_bytes(format_name: str, *, size: tuple[int, int] = (16, 12)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "teal").save(output, format=format_name)
    return output.getvalue()


@pytest.mark.parametrize(
    ("format_name", "mime_type"),
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
        ("BMP", "image/bmp"),
    ],
)
def test_detects_supported_images_by_content(format_name: str, mime_type: str) -> None:
    assert detect_supported_image_mime_type(image_processing_bytes(format_name)) == mime_type


def test_rejects_jpeg_xl_and_malformed_headers() -> None:
    assert detect_supported_image_mime_type(b"\xff\xd8\xff\xf7not-jpeg") is None
    assert detect_supported_image_mime_type(b"\x89PNG\r\n\x1a\nnot-a-png") is None
    assert detect_supported_image_mime_type(b"BM" + b"\x00" * 40) is None


def test_rejects_animated_png_before_image_data() -> None:
    png = image_processing_bytes("PNG")
    idat_offset = png.index(b"IDAT") - 4
    animated_chunk = b"\x00\x00\x00\x08acTL\x00\x00\x00\x02\x00\x00\x00\x00" + b"\x00" * 4
    animated = png[:idat_offset] + animated_chunk + png[idat_offset:]

    assert detect_supported_image_mime_type(animated) is None
    assert unsupported_image_reason(animated) == "animated PNG images are not supported"


def test_jpeg_xl_has_explicit_unsupported_reason() -> None:
    data = b"\xff\xd8\xff\xf7not-jpeg"

    assert detect_supported_image_mime_type(data) is None
    assert unsupported_image_reason(data) == "JPEG XL images are not supported"


def test_bmp_is_converted_to_png() -> None:
    data = image_processing_bytes("BMP")

    result = process_image(data, "image/bmp")

    assert isinstance(result, ProcessedImage)
    assert result.mime_type == "image/png"
    assert result.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert "Image converted from image/bmp to image/png." in result.notes


def test_large_dimensions_are_resized_without_changing_aspect_ratio() -> None:
    data = image_processing_bytes("PNG", size=(2_500, 1_000))

    result = process_image(data, "image/png")

    assert isinstance(result, ProcessedImage)
    assert (result.width, result.height) == (2_000, 800)
    assert "Image resized from 2500x1000 to 2000x800." in result.notes


def test_small_images_are_not_upscaled_or_reencoded() -> None:
    data = image_processing_bytes("PNG", size=(20, 10))

    result = process_image(data, "image/png")

    assert isinstance(result, ProcessedImage)
    assert (result.width, result.height) == (20, 10)
    assert result.data == data
    assert result.notes == ()


def test_encoded_size_limit_triggers_bounded_downscaling() -> None:
    image = Image.effect_noise((300, 300), 100).convert("RGB")
    output = BytesIO()
    image.save(output, format="PNG")

    result = process_image(output.getvalue(), "image/png", max_bytes=3_000)

    assert isinstance(result, ProcessedImage)
    assert len(result.data) <= 3_000
    assert result.width < 300
    assert result.height < 300


def test_source_byte_limit_omits_image_before_decoding() -> None:
    data = image_processing_bytes("PNG")

    result = process_image(data, "image/png", max_source_bytes=len(data) - 1)

    assert isinstance(result, ImageProcessingFailure)
    assert "processing limit" in result.message


def test_source_pixel_limit_omits_image_before_loading_pixels() -> None:
    data = image_processing_bytes("PNG", size=(4, 4))

    result = process_image(data, "image/png", max_source_pixels=15)

    assert isinstance(result, ImageProcessingFailure)
    assert "16 pixels" in result.message
    assert "15-pixel processing limit" in result.message


def test_encode_failure_returns_safe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_encode(image: Image.Image, mime_type: str, attempt: int) -> bytes:
        del image, mime_type, attempt
        raise OSError("encoder unavailable")

    monkeypatch.setattr(image_processing, "_encode_image", fail_encode)

    result = process_image(image_processing_bytes("BMP"), "image/bmp")

    assert isinstance(result, ImageProcessingFailure)
    assert result.message == "could not encode processed image (encoder unavailable)"


def test_decode_failure_returns_safe_failure() -> None:
    malformed = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR" + b"\x00" * 17 + b"\x00\x00\x00\x00IDAT" + b"\x00" * 4
    )
    assert detect_supported_image_mime_type(malformed) == "image/png"

    result = process_image(malformed, "image/png")

    assert isinstance(result, ImageProcessingFailure)
    assert "could not decode a valid image" in result.message


@pytest.mark.parametrize("alias", ["path", "file", "file_path", "filepath", "filePath", "fileName"])
def test_describe_action_resolves_path_aliases(alias: str) -> None:
    assert describe_action("read", {alias: "/repo/src/app.py"}) == "/repo/src/app.py"


def test_describe_action_shortens_paths_under_cwd() -> None:
    assert describe_action("edit", {"filePath": "/repo/src/app.py"}, cwd="/repo") == "src/app.py"
    assert describe_action("read", {"path": "/other/app.py"}, cwd="/repo") == "/other/app.py"
    assert describe_action("read", {"path": "src/app.py"}, cwd="/repo") == "src/app.py"


def test_describe_action_lists_batch_read_files() -> None:
    files = ["/repo/a.py", "/repo/b.py"]
    assert describe_action("read", {"files": files}, cwd="/repo") == "a.py, b.py"
    assert describe_action("read", {"files": [f"/repo/{index}.py" for index in range(4)]}) == (
        "4 files"
    )


@pytest.mark.parametrize("alias", ["command", "cmd", "shell_command", "bashCmd"])
def test_describe_action_resolves_bash_aliases(alias: str) -> None:
    assert describe_action("bash", {alias: "ls -l\ncat x"}) == "ls -l"


def test_describe_action_truncates_long_commands() -> None:
    description = describe_action("bash", {"cmd": "echo " + "x" * 200})

    assert len(description) == 120
    assert description.endswith("…")


def test_describe_action_falls_back_to_first_string_argument() -> None:
    assert describe_action("grep", {"limit": 3, "pattern": "todo"}) == "todo"


def test_describe_action_returns_empty_without_a_target() -> None:
    assert describe_action("read", {}) == ""
    assert describe_action("edit", {"edits": [{"oldText": "a", "newText": "b"}]}) == ""
    assert describe_action("bash", {"timeout": 5}) == ""
