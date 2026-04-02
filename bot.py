from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import config
import db
import claude_bridge
import context_manager
import intent_router
import file_reader
from question_detector import detect_question, QuestionType

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Per-chat locks to prevent concurrent Claude CLI calls on same session
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    # Check username whitelist
    if config.AUTHORIZED_USERNAMES:
        username = (user.username or "").lower()
        if username in config.AUTHORIZED_USERNAMES:
            return True
    # Check chat ID whitelist
    if config.AUTHORIZED_CHATS:
        if update.effective_chat.id in config.AUTHORIZED_CHATS:
            return True
    # If both lists are empty, allow all
    if not config.AUTHORIZED_USERNAMES and not config.AUTHORIZED_CHATS:
        return True
    return False


def auth_required(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_authorized(update):
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Message chunking
# ---------------------------------------------------------------------------

def split_message(text: str, max_len: int = 4000) -> list[str]:
    if not text:
        return ["(empty response)"]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def send_response(update: Update, status_msg, text: str) -> None:
    """Send response as new message(s). Delete status_msg afterwards."""
    # Delete the status/progress message
    try:
        await status_msg.delete()
    except Exception:
        pass

    chunks = split_message(text)
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk)
        except Exception as e:
            logger.error("Failed to send chunk: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def get_or_create_session(chat_id: int) -> db.Session:
    session = await db.get_active_session(chat_id)
    if session:
        return session
    return await db.create_session(chat_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@auth_required
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = await get_or_create_session(update.effective_chat.id)
    await update.message.reply_text(
        f"Claude Bot ready.\n"
        f"Model: {session.model}\n"
        f"Project: {session.project_path}\n\n"
        "Send any message to chat with Claude.\n"
        "Use /help for commands."
    )


@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/project <name> - Switch project directory\n"
        "/projects - List available projects\n"
        "/newproject <name> - Create new project\n"
        "/model <name> - Switch model (sonnet/opus/haiku)\n"
        "/reset - New session (summarizes current)\n"
        "/history [n] - Recent messages\n"
        "/recall <query> - Search neural-memory\n"
        "/remember <text> - Save to neural-memory\n"
        "/cost - Show total cost\n"
        "/status - Current session info\n\n"
        "📁 File upload: Send .pdf .docx .xlsx .txt .md .py .json etc → saved to docs/ folder\n"
        "Add caption to ask Claude questions about the file."
    )


@auth_required
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        dirs = sorted(
            d.name for d in os.scandir(config.PROJECTS_DIR)
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError as e:
        await update.message.reply_text(f"Error listing projects: {e}")
        return

    text = "Available projects:\n" + "\n".join(f"  /{d}" if False else f"  {d}" for d in dirs)
    await update.message.reply_text(text[:4096])


@auth_required
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /project <name>")
        return

    name = context.args[0]
    project_path = os.path.join(config.PROJECTS_DIR, name)

    if not os.path.isdir(project_path):
        await update.message.reply_text(f"Project not found: {project_path}")
        return

    chat_id = update.effective_chat.id
    session = await get_or_create_session(chat_id)
    await db.update_session_project(session.id, project_path)
    await update.message.reply_text(f"Switched to: {project_path}")


@auth_required
async def cmd_newproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /newproject <name>")
        return

    name = context.args[0]
    # Sanitize: only allow alphanumeric, hyphens, underscores, dots
    if not all(c.isalnum() or c in "-_." for c in name):
        await update.message.reply_text("Invalid name. Use only letters, numbers, hyphens, underscores, dots.")
        return

    project_path = os.path.join(config.PROJECTS_DIR, name)

    if os.path.exists(project_path):
        await update.message.reply_text(f"Already exists: {project_path}\nUse /project {name} to switch.")
        return

    try:
        os.makedirs(project_path, exist_ok=True)
        # Initialize git repo
        proc = await asyncio.subprocess.create_subprocess_exec(
            "git", "init", cwd=project_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except OSError as e:
        await update.message.reply_text(f"Error creating project: {e}")
        return

    # Auto-switch session to new project
    chat_id = update.effective_chat.id
    session = await get_or_create_session(chat_id)
    await db.update_session_project(session.id, project_path)
    await update.message.reply_text(
        f"Created & switched to: {project_path}\n"
        f"Git initialized."
    )


@auth_required
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /model <sonnet|opus|haiku>")
        return

    model = context.args[0].lower()
    valid = ["sonnet", "opus", "haiku"]
    if model not in valid:
        await update.message.reply_text(f"Valid models: {', '.join(valid)}")
        return

    chat_id = update.effective_chat.id
    session = await get_or_create_session(chat_id)
    await db.update_session_model(session.id, model)
    await update.message.reply_text(f"Model switched to: {model}")


@auth_required
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    status = await update.message.reply_text("Summarizing and creating new session...")

    async with chat_locks[chat_id]:
        new_session = await context_manager.rotate_session(chat_id)

    await status.edit_text(
        f"New session created.\n"
        f"ID: {new_session.id[:8]}...\n"
        f"Project: {new_session.project_path}\n"
        f"Model: {new_session.model}"
    )


@auth_required
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    limit = 10
    if context.args:
        try:
            limit = min(int(context.args[0]), 50)
        except ValueError:
            pass

    messages = await db.get_recent_messages(update.effective_chat.id, limit=limit)
    if not messages:
        await update.message.reply_text("No history yet.")
        return

    lines = []
    for m in messages:
        role = "You" if m.role == "user" else "Bot"
        content = m.content[:200]
        if len(m.content) > 200:
            content += "..."
        lines.append(f"[{role}] {content}")

    text = "\n\n".join(lines)
    chunks = split_message(text)
    for chunk in chunks:
        await update.message.reply_text(chunk)


@auth_required
async def cmd_recall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /recall <query>")
        return

    query = " ".join(context.args)
    status = await update.message.reply_text(f"Recalling: {query}...")

    response = await claude_bridge.call_claude(
        prompt=f'Use the nmem_recall tool to search for: "{query}". Show the results.',
        model="haiku",
        timeout=30,
    )
    await send_response(update, status, response.result)


@auth_required
async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remember <text to save>")
        return

    text = " ".join(context.args)
    status = await update.message.reply_text("Saving to memory...")

    response = await claude_bridge.call_claude(
        prompt=f'Use the nmem_remember tool to save this: "{text}". Confirm when done.',
        model="haiku",
        timeout=30,
    )
    await send_response(update, status, response.result)


@auth_required
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat_cost = await db.get_total_cost(chat_id)
    total_cost = await db.get_total_cost()
    await update.message.reply_text(
        f"This chat: ${chat_cost:.4f}\n"
        f"All chats: ${total_cost:.4f}"
    )


@auth_required
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = await db.get_active_session(chat_id)
    if not session:
        await update.message.reply_text("No active session. Send a message to start.")
        return

    total_tokens = session.total_input_tokens + session.total_output_tokens
    model_limit = config.MODEL_CONTEXT_WINDOWS.get(session.model, 200_000)
    usage_pct = (total_tokens / model_limit * 100) if model_limit else 0

    await update.message.reply_text(
        f"Session: {session.id[:8]}...\n"
        f"Model: {session.model}\n"
        f"Project: {session.project_path}\n"
        f"Messages: {session.message_count}\n"
        f"Tokens: {total_tokens:,} / {model_limit:,} ({usage_pct:.1f}%)\n"
        f"Cost: ${session.total_cost_usd:.4f}\n"
        f"Created: {session.created_at}\n"
        f"Last used: {session.last_used_at}"
    )


# ---------------------------------------------------------------------------
# File upload handler
# ---------------------------------------------------------------------------

@auth_required
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads: save to docs/ and send content to Claude."""
    chat_id = update.effective_chat.id
    doc = update.message.document
    caption = update.message.caption or ""

    if not doc.file_name:
        await update.message.reply_text("File has no name.")
        return

    status_msg = await update.message.reply_text(f"📥 Downloading {doc.file_name}...")

    try:
        # Download file
        tg_file = await doc.get_file()
        import io as io_module
        buf = io_module.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        file_data = buf.read()

        # Get active session
        session = await get_or_create_session(chat_id)

        # Create docs folder if needed
        docs_dir = os.path.join(session.project_path, "docs")
        os.makedirs(docs_dir, exist_ok=True)

        # Save file to docs/
        file_path = os.path.join(docs_dir, doc.file_name)
        with open(file_path, "wb") as f:
            f.write(file_data)

        # Extract content
        content, extraction_msg = file_reader.extract_content(doc.file_name, file_data)

        if not content:
            await status_msg.edit_text(f"❌ {extraction_msg}")
            return

        # Build prompt
        prompt_parts = [f"<file name=\"{doc.file_name}\">"]
        prompt_parts.append(content)
        prompt_parts.append("</file>")
        prompt_parts.append(f"\n📁 Saved to: docs/{doc.file_name}")
        if caption:
            prompt_parts.append(f"\n\nUser request: {caption}")

        prompt = "\n".join(prompt_parts)

        # Update status and process
        await status_msg.edit_text(f"💬 Processing {doc.file_name}... ({extraction_msg})")
        await _process_message(update, chat_id, prompt, status_msg)

    except Exception as e:
        logger.exception("Document handler error")
        await status_msg.edit_text(f"❌ Error: {e}")


# ---------------------------------------------------------------------------
# Pending option selections: chat_id -> {session, question_text}
# ---------------------------------------------------------------------------
pending_options: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Core Claude call + question detection loop
# ---------------------------------------------------------------------------

async def _call_and_save(
    session: db.Session,
    chat_id: int,
    prompt: str,
    system_prompt: str | None = None,
    status_msg=None,
) -> claude_bridge.ClaudeResponse:
    """Call Claude CLI with streaming, save messages to DB, update token counts."""
    is_new = session.message_count == 0

    # Build streaming callback that updates the Telegram status message
    stream_cb = _make_stream_callback(status_msg) if status_msg else None

    response = await claude_bridge.call_claude(
        prompt=prompt,
        session_id=session.id,
        is_new_session=is_new,
        model=session.model,
        cwd=session.project_path,
        system_prompt=system_prompt,
        on_stream=stream_cb,
    )

    # If session expired in CLI, recover context from DB + neural-memory and retry
    if response.is_error and "no conversation found" in response.result.lower():
        logger.warning("Session %s expired in CLI, recovering context", session.id)
        recovery_ctx = await context_manager.build_recovery_context(chat_id)
        sys_prompt = context_manager.build_system_prompt(session, session.summary)
        if recovery_ctx:
            sys_prompt += "\n\n" + recovery_ctx

        recovery_prompt = (
            "IMPORTANT: Your previous session was lost. Before answering, "
            "use nmem_recall to recover relevant context about the current conversation "
            f"and project. Query: \"{prompt[:200]}\"\n\n"
            "After recalling context, answer the user's message:\n\n"
            f"{prompt}"
        )

        # Reset stream state for retry
        stream_cb = _make_stream_callback(status_msg) if status_msg else None
        response = await claude_bridge.call_claude(
            prompt=recovery_prompt,
            session_id=session.id,
            is_new_session=True,
            model=session.model,
            cwd=session.project_path,
            system_prompt=sys_prompt,
            on_stream=stream_cb,
        )

    await db.save_message(session.id, chat_id, "user", prompt)
    await db.save_message(
        session.id, chat_id, "assistant", response.result,
        tokens_used=response.input_tokens + response.output_tokens,
        cost_usd=response.cost_usd,
    )
    await db.update_session_tokens(
        session.id,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_usd=response.cost_usd,
    )

    return response


# Minimum interval between Telegram status edits (avoid 400 errors)
_STATUS_EDIT_INTERVAL = 3.0  # seconds


def _make_stream_callback(status_msg):
    """Create a streaming callback that only updates status with tool usage info."""
    state = {
        "tools": [],
        "last_edit": 0.0,
        "last_sent_text": "",
    }

    async def _flush_status():
        """Update status message with tool activity (infrequent edits only)."""
        import time
        now = time.monotonic()
        if now - state["last_edit"] < _STATUS_EDIT_INTERVAL:
            return
        if not state["tools"]:
            return

        display = "⏳ " + ", ".join(state["tools"][-5:]) + "..."
        if display == state["last_sent_text"]:
            return
        try:
            await status_msg.edit_text(display[:4096])
            state["last_edit"] = now
            state["last_sent_text"] = display
        except Exception:
            pass

    async def callback(event_type: str, chunk: str):
        if event_type == "tool":
            state["tools"].append(chunk)
            await _flush_status()

    return callback


async def _process_message(
    update: Update,
    chat_id: int,
    text: str,
    status_msg,
) -> None:
    """Full message processing: call Claude, detect questions, auto-approve or ask user."""
    # Keep "typing..." indicator alive while processing
    typing_task = asyncio.create_task(_keep_typing(update, chat_id))
    try:
        await _process_message_inner(update, chat_id, text, status_msg)
    finally:
        typing_task.cancel()


async def _keep_typing(update: Update, chat_id: int) -> None:
    """Send 'typing' chat action every 5s until cancelled."""
    try:
        while True:
            try:
                await update.get_bot().send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


async def _process_message_inner(
    update: Update,
    chat_id: int,
    text: str,
    status_msg,
) -> None:
    async with chat_locks[chat_id]:
        session = await get_or_create_session(chat_id)

        # --- Intent routing: classify with Haiku first ---
        intent = await intent_router.classify_intent(text)
        if intent == intent_router.INTENT_HAIKU:
            # Simple task → Haiku handles directly, no tech lead needed
            response = await claude_bridge.call_claude(
                prompt=text,
                session_id=session.id,
                is_new_session=session.message_count == 0,
                model="haiku",
                cwd=session.project_path,
                on_stream=_make_stream_callback(status_msg),
            )
            await db.save_message(session.id, chat_id, "user", text)
            await db.save_message(
                session.id, chat_id, "assistant", response.result,
                tokens_used=response.input_tokens + response.output_tokens,
                cost_usd=response.cost_usd,
            )
            await db.update_session_tokens(
                session.id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
            )
            await send_response(update, status_msg, response.result)
            return
        # INTENT_OPUS or INTENT_CONTINUE → use session model (opus tech lead)

        # Build system prompt for new sessions
        system_prompt = None
        if session.message_count == 0:
            summary = session.summary
            if not summary:
                recovery = await context_manager.build_recovery_context(chat_id)
                if recovery:
                    summary = recovery
            system_prompt = context_manager.build_system_prompt(session, summary)

        # --- Call Claude (Opus tech lead) + auto-approve loop ---
        all_responses: list[str] = []
        auto_approve_count = 0

        response = await _call_and_save(session, chat_id, text, system_prompt, status_msg)
        all_responses.append(response.result)

        while not response.is_error and auto_approve_count < config.MAX_AUTO_APPROVE_ROUNDS:
            detected = detect_question(response.result)

            if detected.qtype == QuestionType.YES_NO:
                # Auto-approve: send "Yes" back to Claude
                auto_approve_count += 1
                logger.info(
                    "Auto-approve #%d for chat %d: %s",
                    auto_approve_count, chat_id, detected.question_text[:100],
                )
                try:
                    await status_msg.edit_text(
                        f"Auto-approved ({auto_approve_count}x)..."
                    )
                except Exception:
                    pass

                # Refresh session (message_count changed after save)
                session = await get_or_create_session(chat_id)
                response = await _call_and_save(session, chat_id, "Yes, proceed.", status_msg=status_msg)
                all_responses.append(response.result)

            elif detected.qtype == QuestionType.OPTIONS:
                # Show options to user via inline keyboard, stop the loop
                options = detected.options or []
                keyboard = []
                for i, opt in enumerate(options[:10]):  # max 10 buttons
                    label = opt[:60] + ("..." if len(opt) > 60 else "")
                    keyboard.append(
                        [InlineKeyboardButton(label, callback_data=f"opt:{i}")]
                    )
                # Add a "skip" button
                keyboard.append(
                    [InlineKeyboardButton("Skip (don't answer)", callback_data="opt:skip")]
                )

                # Store pending state with full option texts
                pending_options[chat_id] = {
                    "session_id": session.id,
                    "options": options[:10],
                }

                # Send accumulated response + options keyboard
                combined = "\n\n---\n\n".join(all_responses)
                await send_response(update, status_msg, combined)
                reply_markup = InlineKeyboardMarkup(keyboard)
                question = detected.question_text or "Choose an option:"
                # Telegram limits caption to 4096 chars
                await update.message.reply_text(
                    question[:4096],
                    reply_markup=reply_markup,
                )
                return  # Exit loop, wait for callback

            else:
                # No question detected, done
                break

    # Send final combined response
    combined = "\n\n---\n\n".join(all_responses)
    await send_response(update, status_msg, combined)

    # Check if we hit the auto-approve limit while Claude was still asking
    if auto_approve_count >= config.MAX_AUTO_APPROVE_ROUNDS:
        last_detected = detect_question(response.result)
        if last_detected.qtype == QuestionType.YES_NO:
            keyboard = [
                [
                    InlineKeyboardButton("Yes, continue", callback_data="opt:0"),
                    InlineKeyboardButton("No, stop", callback_data="opt:skip"),
                ],
            ]
            pending_options[chat_id] = {"session_id": session.id, "options": ["Yes, proceed."]}
            await update.message.reply_text(
                f"Auto-approved {auto_approve_count}x, limit reached. Continue?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

    if auto_approve_count > 0:
        try:
            await update.message.reply_text(
                f"Auto-approved {auto_approve_count} confirmation(s)."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Callback handler for option selection
# ---------------------------------------------------------------------------

async def handle_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard option selection."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    if not _is_authorized(update):
        return

    data = query.data or ""
    if not data.startswith("opt:"):
        return

    parts = data.split(":", 1)
    choice_idx = parts[1] if len(parts) > 1 else ""

    # Remove inline keyboard
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if choice_idx == "skip":
        await query.edit_message_text("Skipped.")
        pending_options.pop(chat_id, None)
        return

    # Look up full option text from pending state
    pending = pending_options.get(chat_id, {})
    options_list = pending.get("options", [])
    try:
        idx = int(choice_idx)
        choice_text = options_list[idx] if idx < len(options_list) else f"Option {idx}"
    except (ValueError, IndexError):
        choice_text = f"Option {choice_idx}"

    # Show selection and send to Claude
    await query.edit_message_text(f"Selected: {choice_text}")

    pending = pending_options.pop(chat_id, None)
    if not pending:
        await query.message.reply_text("Session expired. Send a new message.")
        return

    status_msg = await query.message.reply_text("...")

    # Send the user's choice as the next message to Claude
    reply_text = choice_text or f"Option {choice_idx}"
    await _process_message(update, chat_id, reply_text, status_msg)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

@auth_required
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text
    if not text:
        return

    status_msg = await update.message.reply_text("...")
    await _process_message(update, chat_id, text, status_msg)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    await db.init_db()
    logger.info("Database initialized")

    # Register bot menu commands
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("help", "Show help"),
        BotCommand("projects", "List available projects"),
        BotCommand("project", "Switch project"),
        BotCommand("newproject", "Create new project"),
        BotCommand("model", "Switch model (sonnet/opus/haiku)"),
        BotCommand("reset", "End session & start new"),
        BotCommand("history", "Show recent messages"),
        BotCommand("recall", "Search neural-memory"),
        BotCommand("remember", "Save to neural-memory"),
        BotCommand("cost", "Show usage costs"),
        BotCommand("status", "Current session info"),
    ])


async def post_shutdown(app: Application) -> None:
    await db.close_db()
    logger.info("Database closed")


def main() -> None:
    if not config.BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("newproject", cmd_newproject))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("recall", cmd_recall))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("status", cmd_status))

    # Option selection callback
    app.add_handler(CallbackQueryHandler(handle_option_callback, pattern=r"^opt:"))

    # Document/file handler (must be before text handler so it takes priority)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Catch-all text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting... Authorized chats: %s", config.AUTHORIZED_CHATS or "ALL")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
