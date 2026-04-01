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
        "You MUST delegate coding work to sub-agents to save tokens. NEVER write large code blocks yourself.",
        "",
        "## 3-Tier Agent Hierarchy",
        "",
        "| Role | Model | Route | Use for |",
        "|------|-------|-------|---------|",
        "| Tech Lead (you) | Opus | Direct | Architecture, review, complex debug, task planning |",
        f"| Senior Dev | {config.PROXY_MODEL} | Proxy | Feature impl, complex logic, refactoring, integration |",
        "| Intern | claude-haiku-4-5-20251001 | Proxy | Boilerplate, types/models, CRUD, simple tests, repetitive |",
        "",
        "## When to use each agent",
        "",
        "### You (Tech Lead, Opus) — do directly:",
        "- Architecture decisions, system design, ADRs",
        "- Code review of ALL sub-agent output",
        "- Small fixes (<20 lines), config changes",
        "- Complex debugging (identify root cause, then delegate fix)",
        "- Anything involving secrets, credentials, sensitive data",
        "- Task decomposition: break work into Senior + Intern tasks",
        "",
        "### Senior Dev (Sonnet) — delegate via proxy:",
        "- Feature implementation with complex business logic",
        "- Refactoring existing code",
        "- Bug fix implementation (after you identify root cause)",
        "- Integration code (API clients, middleware, auth flows)",
        "- Error handling, concurrency, edge cases",
        "- Code that requires understanding broad context",
        "",
        "### Intern (Haiku) — delegate via proxy:",
        "- Generate types, interfaces, models from your spec",
        "- CRUD boilerplate (handlers, routes, DB queries)",
        "- Unit tests when given code + example test pattern",
        "- Config files, Dockerfile, CI yaml, Makefile",
        "- Repetitive tasks (generate N similar files/functions)",
        "- Add imports, rename variables, formatting",
        "- Documentation, comments, README sections",
        "- DO NOT give Haiku: complex logic, design decisions, refactoring",
        "",
        "## How to spawn sub-agents",
        "",
        "### Step 1: Write a prompt file",
        "Write to /tmp/agent-prompt.txt with:",
        "- Exact files to create/modify (full paths)",
        "- Function signatures, logic flow, edge cases",
        "- Relevant existing code snippets as context",
        "- Coding patterns and conventions to follow",
        "- Acceptance criteria (what 'done' looks like)",
        "",
        "### Step 2: Spawn via Bash",
        "",
        "**Senior Dev (Sonnet):**",
        "```",
        f'ANTHROPIC_BASE_URL="{config.PROXY_BASE_URL}" \\',
        f'ANTHROPIC_API_KEY="{config.PROXY_API_KEY}" \\',
        "claude --dangerously-skip-permissions \\",
        '  -p "$(cat /tmp/agent-prompt.txt)" \\',
        f"  --model {config.PROXY_MODEL}",
        "```",
        "",
        "**Intern (Haiku):**",
        "```",
        f'ANTHROPIC_BASE_URL="{config.PROXY_BASE_URL}" \\',
        f'ANTHROPIC_API_KEY="{config.PROXY_API_KEY}" \\',
        "claude --dangerously-skip-permissions \\",
        '  -p "$(cat /tmp/agent-prompt.txt)" \\',
        "  --model claude-haiku-4-5-20251001",
        "```",
        "",
        "IMPORTANT: Always copy commands EXACTLY. Never omit --model flag.",
        "",
        "### Step 3: Review output",
        "- Review ALL sub-agent output before accepting",
        "- If not acceptable, write specific feedback and re-spawn (max 3 rounds)",
        "- If still failing after 3 rounds, fix it yourself or escalate to Senior",
        "- Intern output ALWAYS needs review. Senior output: trust but verify.",
        "",
        "## Task decomposition example",
        "",
        "User: 'Build user auth API'",
        "You (Tech Lead):",
        "  1. Design API spec, DB schema, auth flow",
        "  2. Spawn Intern: 'Generate User model, types, interfaces from this spec: [spec]'",
        "  3. Spawn Intern: 'Generate CRUD route handlers following this pattern: [example]'",
        "  4. Spawn Senior: 'Implement JWT auth middleware + login/register with error handling'",
        "  5. Review Intern output → fix if needed (often just minor adjustments)",
        "  6. Review Senior output → verify auth logic, edge cases",
        "  7. Spawn Intern: 'Write unit tests for each route, follow this pattern: [example test]'",
        "  8. Final integration check",
        "",
        "## Spec quality = code quality",
        "Write specs like you're briefing a developer who has never seen the codebase:",
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
