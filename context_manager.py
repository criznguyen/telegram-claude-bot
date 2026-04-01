from __future__ import annotations

import logging

import config
import db
from claude_bridge import call_claude

logger = logging.getLogger(__name__)


def should_rotate(session: db.Session) -> bool:
    """Check if session should be rotated based on token usage."""
    model_limit = config.MODEL_CONTEXT_WINDOWS.get(session.model, 200_000)
    threshold = int(model_limit * config.TOKEN_THRESHOLD_PCT)
    current = session.total_input_tokens + session.total_output_tokens
    if current > threshold:
        logger.info(
            "Session %s needs rotation: %d tokens > %d threshold",
            session.id, current, threshold,
        )
        return True
    return False


async def summarize_session(session_id: str, chat_id: int) -> str:
    """Generate a summary of the session conversation from DB messages."""
    messages = await db.get_recent_messages(chat_id, limit=50, session_id=session_id)
    if not messages:
        return "No conversation history."

    conversation = "\n".join(
        f"{'User' if m.role == 'user' else 'Assistant'}: {m.content[:500]}"
        for m in messages[-30:]  # Last 30 messages max
    )

    summary_prompt = (
        "Summarize the following conversation concisely. "
        "Focus on: key decisions, current tasks, important context, "
        "and any unfinished work. Keep under 500 words.\n\n"
        f"{conversation}"
    )

    response = await call_claude(
        prompt=summary_prompt,
        model="haiku",  # Use cheapest model for summaries
        timeout=60,
    )

    return response.result if not response.is_error else "Failed to generate summary."


async def rotate_session(chat_id: int) -> db.Session:
    """Deactivate current session and create a new one with context."""
    old_session = await db.get_active_session(chat_id)
    if not old_session:
        return await db.create_session(chat_id)

    # Summarize the old session
    summary = await summarize_session(old_session.id, chat_id)
    logger.info("Session %s rotated. Summary: %s", old_session.id, summary[:200])

    # Deactivate old
    await db.deactivate_session(old_session.id, summary)

    # Create new session with same project/model
    new_session = await db.create_session(
        chat_id=chat_id,
        project_path=old_session.project_path,
        model=old_session.model,
    )

    return new_session


def build_system_prompt(session: db.Session, summary: str | None = None) -> str:
    """Build system prompt for a new/rotated session."""
    parts = [
        "You are a **Tech Lead agent** — a senior software architect communicating via Telegram.",
        f"Current working directory: {session.project_path}",
        "You have access to all Claude Code tools including file editing, "
        "terminal commands, and neural-memory for persistent knowledge.",
        "Keep responses concise - this is a chat interface.",
        "",
        "## Your Role: Tech Lead (Direct CLI, Opus)",
        "You handle planning, architecture, code review, and quality control.",
        "For heavy coding/testing work, you MUST delegate to proxy sub-agents to save tokens.",
        "",
        "## Proxy Sub-Agent Routing",
        "When you need implementation code or test writing, delegate to a Dev sub-agent:",
        "",
        "### When to delegate (proxy):",
        "- Writing new files or large code blocks (>50 lines)",
        "- Implementing features from your specs",
        "- Writing test suites",
        "- Bug fix implementation (after you identify root cause)",
        "- Refactoring existing code",
        "",
        "### When to do yourself (direct):",
        "- Architecture decisions, system design",
        "- Code review of sub-agent output",
        "- Small fixes (<20 lines), config changes",
        "- Anything involving secrets, credentials, sensitive data",
        "- Final polish/adjustments after review",
        "",
        "### How to spawn a Dev sub-agent:",
        "1. Write a detailed prompt file to /tmp/agent-prompt.txt with:",
        "   - Exact files to create/modify (with full paths)",
        "   - Function signatures, logic flow, edge cases",
        "   - Relevant existing code snippets as context",
        "   - Coding patterns and conventions to follow",
        "   - Acceptance criteria (what 'done' looks like)",
        "2. Spawn via Bash:",
        '   ```',
        '   ANTHROPIC_BASE_URL="http://pro-x.io.vn/" \\',
        f'   ANTHROPIC_API_KEY="{config.PROXY_API_KEY}" \\',
        '   claude --dangerously-skip-permissions \\',
        '     -p "$(cat /tmp/agent-prompt.txt)" \\',
        f'     --model {config.PROXY_MODEL}',
        '   ```',
        "   IMPORTANT: Always copy this command EXACTLY. Never omit --model flag.",
        "3. Review the output — check correctness, patterns, edge cases",
        "4. If not acceptable, write specific feedback and re-spawn (max 3 rounds)",
        "5. If still failing after 3 rounds, fix it yourself directly",
        "",
        "### Spec quality = code quality. Write specs like you're briefing a senior dev:",
        "- WHAT to build (not just 'implement feature X')",
        "- WHERE (exact file paths, line numbers if modifying)",
        "- HOW (algorithm, data flow, error handling)",
        "- CONTEXT (paste relevant existing code, types, interfaces)",
        "- DONE WHEN (testable acceptance criteria)",
        "",
        "## Neural-memory auto-save rules",
        "After completing each task, proactively save important knowledge to neural-memory using nmem_remember. Save when you:",
        "- Make a key decision or choose between alternatives",
        "- Fix a bug (root cause + solution)",
        "- Discover a pattern, insight, or non-obvious finding",
        "- Learn a user preference or workflow",
        "- Complete a significant task or milestone",
        "- Encounter an important project fact (architecture, config, convention)",
        "Do NOT save: routine file reads, trivial changes, things already in git history.",
        "At the end of each session or after multiple exchanges, call nmem_auto to auto-detect and save any missed knowledge.",
        "Before starting work on a topic, call nmem_recall to check existing context.",
    ]

    if summary:
        parts.append(f"\n## Previous conversation summary:\n{summary}")

    return "\n".join(parts)


async def build_recovery_context(chat_id: int) -> str:
    """Build context from DB when session is lost/corrupted."""
    messages = await db.get_recent_messages(chat_id, limit=10)
    if not messages:
        return ""

    lines = ["## Recent conversation (recovered from history):"]
    for m in messages:
        role = "User" if m.role == "user" else "Assistant"
        content = m.content[:300]
        lines.append(f"{role}: {content}")

    return "\n".join(lines)
