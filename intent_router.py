"""Intent classification using Haiku for smart model routing."""
from __future__ import annotations

import json
import logging

import config
from claude_bridge import call_claude

logger = logging.getLogger(__name__)

# Intent types and their target models
INTENT_HAIKU = "haiku"      # Simple Q&A, greetings, formatting, translation
INTENT_OPUS = "opus"        # Planning, architecture, review, complex reasoning
INTENT_CONTINUE = "continue"  # Continue existing session flow (don't reclassify)

CLASSIFY_PROMPT = """Classify this user message into ONE intent. Reply with ONLY a JSON object, no other text.

Categories:
- "simple": Greetings, small talk, simple factual questions, formatting requests, translation, status checks, "what is X", "explain X"
- "complex": Architecture, planning, SDLC, code review, debugging complex issues, multi-step tasks, "build X", "implement X", "fix bug in X", "design X", system design, refactoring
- "continue": Follow-up to previous conversation ("yes", "ok", "go ahead", "tiep tuc", "do it", short replies that reference prior context)

User message:
{message}

JSON format: {{"intent": "simple|complex|continue"}}"""


async def classify_intent(text: str) -> str:
    """Classify user message intent using Haiku. Returns model name to use."""
    # Short messages that are clearly continuations
    stripped = text.strip().lower()
    if len(stripped) < 15 and any(w in stripped for w in [
        "ok", "yes", "no", "y", "n", "tiep", "tiếp", "do it", "go", "dung", "đúng",
        "khong", "không", "co", "có", "cancel", "stop", "huy", "huỷ",
    ]):
        return INTENT_CONTINUE

    try:
        response = await call_claude(
            prompt=CLASSIFY_PROMPT.format(message=text[:500]),
            model="haiku",
            timeout=15,
        )

        if response.is_error:
            logger.warning("Intent classification failed: %s", response.result[:100])
            return INTENT_CONTINUE  # Fallback: use existing session model

        # Parse JSON response
        raw = response.result.strip()
        # Handle cases where Haiku wraps in markdown
        if "```" in raw:
            raw = raw.split("```")[1].replace("json", "").strip()
        data = json.loads(raw)
        intent = data.get("intent", "continue")

        if intent == "simple":
            logger.info("Intent: simple → haiku")
            return INTENT_HAIKU
        elif intent == "complex":
            logger.info("Intent: complex → opus")
            return INTENT_OPUS
        else:
            logger.info("Intent: continue → session model")
            return INTENT_CONTINUE

    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.warning("Intent parse error: %s", e)
        return INTENT_CONTINUE
