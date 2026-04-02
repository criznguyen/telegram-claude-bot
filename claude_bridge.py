from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable

import config

logger = logging.getLogger(__name__)

# Callback type: receives (event_type, text_chunk) — e.g. ("thinking", "..."), ("text", "...")
StreamCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class ClaudeResponse:
    result: str = ""
    session_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    is_error: bool = False


def _build_cmd(
    prompt: str,
    session_id: str | None,
    is_new_session: bool,
    model: str,
    system_prompt: str | None,
    streaming: bool = False,
) -> list[str]:
    fmt = "stream-json" if streaming else "json"
    cmd = [
        config.CLAUDE_PATH,
        "-p",
        "--output-format", fmt,
        "--model", model,
        "--dangerously-skip-permissions",
    ]
    if config.MAX_COST_PER_REQUEST > 0:
        cmd.extend(["--max-budget-usd", str(config.MAX_COST_PER_REQUEST)])
    if streaming:
        cmd.append("--verbose")

    if session_id:
        if is_new_session:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    cmd.append(prompt)
    return cmd


async def call_claude(
    prompt: str,
    session_id: str | None = None,
    is_new_session: bool = True,
    model: str = "sonnet",
    cwd: str | None = None,
    system_prompt: str | None = None,
    timeout: int | None = None,
    on_stream: StreamCallback | None = None,
) -> ClaudeResponse:
    """Run claude CLI. If on_stream is provided, use streaming mode."""
    timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT or None
    streaming = on_stream is not None
    cmd = _build_cmd(prompt, session_id, is_new_session, model, system_prompt, streaming)
    work_dir = cwd or config.PROJECTS_DIR

    logger.info(
        "Claude CLI call: model=%s, session=%s, new=%s, stream=%s, cwd=%s",
        model, session_id, is_new_session, streaming, work_dir,
    )

    if streaming:
        return await _run_streaming(cmd, work_dir, timeout, on_stream)
    else:
        return await _run_batch(cmd, work_dir, timeout)


async def _run_batch(cmd: list[str], cwd: str, timeout: int) -> ClaudeResponse:
    """Original batch mode — wait for full response."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=10 * 1024 * 1024,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return ClaudeResponse(
            result="Request timed out. Try a simpler prompt or increase timeout.",
            is_error=True,
        )
    except Exception as e:
        logger.exception("Claude CLI subprocess error")
        return ClaudeResponse(result=f"Error running Claude CLI: {e}", is_error=True)

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        full_err = err_text or stdout_text
        logger.error("Claude CLI error (rc=%d): %s", proc.returncode, full_err[:500])
        return ClaudeResponse(
            result=f"Claude CLI error: {full_err[:1000]}",
            is_error=True,
        )

    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ClaudeResponse(result=raw or "Empty response from Claude.")

    return _parse_response(data)


async def _run_streaming(
    cmd: list[str],
    cwd: str,
    timeout: int,
    on_stream: StreamCallback,
) -> ClaudeResponse:
    """Streaming mode — parse stream-json lines and call on_stream callback."""
    # Use 10MB line limit — Claude CLI can emit very large JSON lines
    # (tool results, file contents, etc.) that exceed asyncio's 64KB default.
    _LINE_LIMIT = 10 * 1024 * 1024

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=_LINE_LIMIT,
        )
    except Exception as e:
        logger.exception("Claude CLI subprocess error")
        return ClaudeResponse(result=f"Error running Claude CLI: {e}", is_error=True)

    result_data: dict | None = None
    last_thinking = ""
    last_text = ""

    try:
        async def read_stream():
            nonlocal result_data, last_thinking, last_text
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                evt_type = event.get("type", "")

                if evt_type == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        block_type = block.get("type", "")
                        if block_type == "thinking":
                            thinking = block.get("thinking", "")
                            if thinking and thinking != last_thinking:
                                # Send only the new part
                                new_part = thinking[len(last_thinking):]
                                if new_part:
                                    await on_stream("thinking", new_part)
                                last_thinking = thinking
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text and text != last_text:
                                new_part = text[len(last_text):]
                                if new_part:
                                    await on_stream("text", new_part)
                                last_text = text
                        elif block_type == "tool_use":
                            tool_name = block.get("name", "unknown")
                            await on_stream("tool", tool_name)

                elif evt_type == "result":
                    result_data = event

        await asyncio.wait_for(read_stream(), timeout=timeout)
        await proc.wait()

    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return ClaudeResponse(
            result="Request timed out.",
            is_error=True,
        )

    if proc.returncode != 0 and result_data is None:
        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        full_err = stderr_bytes.decode("utf-8", errors="replace").strip()
        logger.error("Claude CLI stream error (rc=%d): %s", proc.returncode, full_err[:500])
        return ClaudeResponse(
            result=f"Claude CLI error: {full_err[:1000]}",
            is_error=True,
        )

    if result_data:
        return _parse_response(result_data)

    # Fallback: use accumulated text
    return ClaudeResponse(result=last_text or "Empty response from Claude.")


def _parse_response(data: dict) -> ClaudeResponse:
    """Parse Claude CLI JSON output into ClaudeResponse."""
    result_text = data.get("result", "")
    session_id = data.get("session_id", "")
    cost = data.get("cost_usd", 0) or data.get("total_cost_usd", 0) or 0
    duration = data.get("duration_ms", 0) or data.get("duration_api_ms", 0) or 0

    input_tokens = data.get("input_tokens", 0) or 0
    output_tokens = data.get("output_tokens", 0) or 0

    usage = data.get("usage", {})
    if usage:
        input_tokens = input_tokens or usage.get("input_tokens", 0)
        output_tokens = output_tokens or usage.get("output_tokens", 0)
        input_tokens += usage.get("cache_creation_input_tokens", 0)
        input_tokens += usage.get("cache_read_input_tokens", 0)

    is_error = data.get("is_error", False) or (
        data.get("type") == "result"
        and str(data.get("subtype", "")).startswith("error")
    )
    if is_error and not result_text:
        errors = data.get("errors", [])
        result_text = (
            "; ".join(errors) if errors
            else data.get("error", "Unknown error from Claude CLI")
        )

    return ClaudeResponse(
        result=result_text or "Empty response.",
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        duration_ms=duration,
        is_error=is_error,
    )
