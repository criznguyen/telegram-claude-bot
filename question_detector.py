"""Detect and classify questions in Claude's response.

Classification:
- YES_NO: binary confirmation → auto-approve with "Yes"
- OPTIONS: numbered/lettered choices → show inline keyboard to user
- NONE: no actionable question → deliver response as-is
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class QuestionType(Enum):
    NONE = "none"
    YES_NO = "yes_no"
    OPTIONS = "options"


@dataclass
class DetectedQuestion:
    qtype: QuestionType
    question_text: str = ""
    options: list[str] | None = None  # For OPTIONS type: ["Option A", "Option B", ...]


# Patterns that indicate yes/no questions
_YES_NO_PATTERNS = [
    r"(?:shall|should|do you want|would you like|can i|may i|want me to|proceed|continue|go ahead)\b.*\?",
    r"\?\s*$",  # Ends with question mark (fallback, checked after options)
    r"\((?:y(?:es)?/n(?:o)?|yes/no|y/n)\)",
    r"(?:approve|confirm|accept|agree)\b.*\?",
    r"is (?:this|that) (?:ok|okay|correct|right|fine)\b.*\?",
    r"ready to (?:proceed|start|begin|continue)\?",
]

# Patterns for numbered/lettered option lists
_OPTION_LINE = re.compile(
    r"^\s*(?:"
    r"(?P<num>\d+)[\.\)]\s+"          # 1. or 1)
    r"|(?P<letter>[a-zA-Z])[\.\)]\s+" # a. or A)
    r"|[-*]\s+\*?\*?(?:Option\s+)?"   # - Option or * Option
    r")"
    r"(?P<text>.+)",
    re.MULTILINE,
)

# Patterns that strongly suggest multiple choice
_CHOICE_INTRO = re.compile(
    r"(?:which (?:one|option|approach)|choose|pick|select|prefer|here are .* options|"
    r"following (?:options|approaches|choices)|would you (?:prefer|choose))",
    re.IGNORECASE,
)


def detect_question(text: str) -> DetectedQuestion:
    """Analyze Claude's response and classify any trailing question."""
    if not text or len(text) < 10:
        return DetectedQuestion(qtype=QuestionType.NONE)

    # Take the last ~2000 chars for analysis (question is usually at the end)
    tail = text[-2000:]

    # --- Step 1: Check for numbered/lettered options ---
    options = _extract_options(tail)
    if options and len(options) >= 2:
        # Find the question/intro text above the options
        question_text = _extract_question_above_options(tail)
        return DetectedQuestion(
            qtype=QuestionType.OPTIONS,
            question_text=question_text,
            options=options,
        )

    # --- Step 2: Check for yes/no patterns ---
    # Look at last few sentences
    last_block = _get_last_block(tail)
    for pattern in _YES_NO_PATTERNS:
        if re.search(pattern, last_block, re.IGNORECASE):
            # Make sure it's not an option question without numbered list
            if _CHOICE_INTRO.search(last_block):
                # Might be options in prose form - treat as yes/no still
                # (if no clear numbered list was found above)
                pass
            return DetectedQuestion(
                qtype=QuestionType.YES_NO,
                question_text=last_block.strip(),
            )

    return DetectedQuestion(qtype=QuestionType.NONE)


def _extract_options(text: str) -> list[str]:
    """Extract numbered/lettered options from text."""
    matches = list(_OPTION_LINE.finditer(text))
    if not matches:
        return []

    # Find the largest consecutive group of options (they should be near end)
    groups: list[list[re.Match]] = []
    current_group: list[re.Match] = [matches[0]]

    for prev, curr in zip(matches, matches[1:]):
        # Options should be within ~200 chars of each other
        gap = curr.start() - prev.end()
        if gap < 200:
            current_group.append(curr)
        else:
            groups.append(current_group)
            current_group = [curr]
    groups.append(current_group)

    # Take the last group (most likely the question options)
    best = max(groups, key=len)
    if len(best) < 2:
        return []

    return [m.group("text").strip().rstrip("*") for m in best]


def _extract_question_above_options(text: str) -> str:
    """Get the question/intro sentence above the option list."""
    first_option = _OPTION_LINE.search(text)
    if not first_option:
        return ""

    before = text[: first_option.start()].strip()
    # Take last 1-2 lines before options
    lines = before.split("\n")
    relevant = [l.strip() for l in lines[-3:] if l.strip()]
    return "\n".join(relevant)


def _get_last_block(text: str) -> str:
    """Get the last meaningful block of text (last paragraph or last ~500 chars)."""
    # Split by double newlines to get paragraphs
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return text[-500:]

    last = paragraphs[-1]
    # If last paragraph is very short, include previous one too
    if len(last) < 50 and len(paragraphs) >= 2:
        last = paragraphs[-2] + "\n\n" + last

    return last[-500:]
