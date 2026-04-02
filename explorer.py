"""IDE-like project exploration commands for Telegram bot."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path


MAX_OUTPUT = 3800  # Leave room for formatting within Telegram's 4096 limit


def _safe_path(project_path: str, user_path: str) -> str | None:
    """Resolve user_path relative to project_path, block directory traversal."""
    base = Path(project_path).resolve()
    target = (base / user_path).resolve()
    if not str(target).startswith(str(base)):
        return None
    return str(target)


async def _run(cmd: list[str], cwd: str, timeout: int = 10) -> str:
    """Run a subprocess and return stdout+stderr, truncated to MAX_OUTPUT."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return "(command timed out)"
    except Exception as e:
        return f"(error: {e})"

    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n\n... truncated ({len(stdout)} bytes total)"
    return text.strip() or "(empty output)"


# ---------------------------------------------------------------------------
# /tree [path] [depth]
# ---------------------------------------------------------------------------

async def tree(project_path: str, args: list[str]) -> str:
    """Show directory structure. Usage: /tree [path] [depth]"""
    rel_path = "."
    depth = "3"

    for arg in args:
        if arg.isdigit():
            depth = arg
        else:
            rel_path = arg

    target = _safe_path(project_path, rel_path)
    if not target:
        return "Invalid path."
    if not os.path.isdir(target):
        return f"Not a directory: {rel_path}"

    # Use find instead of tree (more commonly available)
    result = await _run(
        ["find", rel_path, "-maxdepth", depth,
         "-not", "-path", "*/.git/*",
         "-not", "-path", "*/__pycache__/*",
         "-not", "-path", "*/node_modules/*",
         "-not", "-path", "*/.venv/*",
         "-not", "-name", ".git"],
        cwd=project_path,
    )

    # Sort and format as tree-like output
    lines = sorted(result.split("\n"))
    formatted = []
    for line in lines:
        if not line.strip():
            continue
        depth_level = line.count(os.sep)
        indent = "  " * depth_level
        name = os.path.basename(line) or line
        if os.path.isdir(os.path.join(project_path, line)):
            name += "/"
        formatted.append(f"{indent}{name}")

    output = "\n".join(formatted)
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n... truncated"
    return f"📂 {rel_path}\n\n{output}"


# ---------------------------------------------------------------------------
# /view <file> [start_line] [end_line]
# ---------------------------------------------------------------------------

async def view(project_path: str, args: list[str]) -> str:
    """View file content with optional line range. Usage: /view <file> [start] [end]"""
    if not args:
        return "Usage: /view <file> [start_line] [end_line]"

    file_path = args[0]
    target = _safe_path(project_path, file_path)
    if not target:
        return "Invalid path."
    if not os.path.isfile(target):
        return f"File not found: {file_path}"

    start_line = 1
    end_line = None
    if len(args) >= 2:
        try:
            start_line = max(1, int(args[1]))
        except ValueError:
            pass
    if len(args) >= 3:
        try:
            end_line = int(args[2])
        except ValueError:
            pass

    try:
        with open(target, "r", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return f"Error reading file: {e}"

    total = len(all_lines)
    if end_line is None:
        # Auto-limit to ~80 lines from start
        end_line = min(start_line + 79, total)

    selected = all_lines[start_line - 1 : end_line]
    numbered = []
    for i, line in enumerate(selected, start=start_line):
        numbered.append(f"{i:4d} | {line.rstrip()}")

    content = "\n".join(numbered)
    if len(content) > MAX_OUTPUT:
        content = content[:MAX_OUTPUT] + "\n... truncated"

    header = f"📄 {file_path} (lines {start_line}-{end_line} of {total})"
    return f"{header}\n\n{content}"


# ---------------------------------------------------------------------------
# /diff [file]
# ---------------------------------------------------------------------------

async def diff(project_path: str, args: list[str]) -> str:
    """Show git diff. Usage: /diff [file] [--staged]"""
    cmd = ["git", "diff", "--stat"]
    detail_cmd = ["git", "diff"]

    staged = "--staged" in args or "-s" in args
    file_filter = [a for a in args if not a.startswith("-")]

    if staged:
        cmd.insert(2, "--staged")
        detail_cmd.insert(2, "--staged")

    # Get stat overview first
    stat = await _run(cmd, cwd=project_path)

    # Get detailed diff (optionally for specific file)
    if file_filter:
        detail_cmd.extend(file_filter)

    detail = await _run(detail_cmd, cwd=project_path, timeout=15)

    label = "staged " if staged else ""
    if not detail or detail == "(empty output)":
        return f"No {label}changes."

    # If detail is small enough, show it directly
    if len(detail) <= MAX_OUTPUT - 200:
        return f"📝 Git {label}diff\n\n{detail}"

    # Otherwise show stat + truncated detail
    combined = f"📝 Git {label}diff\n\n{stat}\n\n{detail}"
    if len(combined) > MAX_OUTPUT:
        combined = combined[:MAX_OUTPUT] + "\n... truncated"
    return combined


# ---------------------------------------------------------------------------
# /log [n] [--file <path>]
# ---------------------------------------------------------------------------

async def log(project_path: str, args: list[str]) -> str:
    """Show git log. Usage: /log [count] [file]"""
    count = "15"
    file_path = None

    for arg in args:
        if arg.isdigit():
            count = str(min(int(arg), 50))
        elif not arg.startswith("-"):
            file_path = arg

    cmd = [
        "git", "log",
        f"--max-count={count}",
        "--format=%h %ad %an | %s",
        "--date=short",
    ]

    if file_path:
        cmd.append("--")
        cmd.append(file_path)

    result = await _run(cmd, cwd=project_path)
    header = f"📋 Last {count} commits"
    if file_path:
        header += f" for {file_path}"
    return f"{header}\n\n{result}"


# ---------------------------------------------------------------------------
# /branch
# ---------------------------------------------------------------------------

async def branch(project_path: str, args: list[str]) -> str:
    """Show git branches. Usage: /branch"""
    cmd = ["git", "branch", "-vv"]
    if "-a" in args or "--all" in args:
        cmd.append("-a")
    result = await _run(cmd, cwd=project_path)
    return f"🌿 Branches\n\n{result}"


# ---------------------------------------------------------------------------
# /find <pattern>
# ---------------------------------------------------------------------------

async def find(project_path: str, args: list[str]) -> str:
    """Find files by name pattern. Usage: /find <pattern>"""
    if not args:
        return "Usage: /find <pattern>"

    pattern = args[0]
    cmd = [
        "find", ".", "-type", "f",
        "-iname", f"*{pattern}*",
        "-not", "-path", "*/.git/*",
        "-not", "-path", "*/__pycache__/*",
        "-not", "-path", "*/node_modules/*",
        "-not", "-path", "*/.venv/*",
    ]
    result = await _run(cmd, cwd=project_path)
    count = len([l for l in result.split("\n") if l.strip()]) if result != "(empty output)" else 0
    return f"🔍 Files matching '{pattern}' ({count} found)\n\n{result}"


# ---------------------------------------------------------------------------
# /grep <pattern> [path]
# ---------------------------------------------------------------------------

async def grep(project_path: str, args: list[str]) -> str:
    """Search file contents. Usage: /grep <pattern> [path]"""
    if not args:
        return "Usage: /grep <pattern> [path]"

    pattern = args[0]
    search_path = args[1] if len(args) > 1 else "."

    cmd = [
        "grep", "-rn", "--include=*.py", "--include=*.go", "--include=*.js",
        "--include=*.ts", "--include=*.tsx", "--include=*.jsx",
        "--include=*.java", "--include=*.rs", "--include=*.md",
        "--include=*.yaml", "--include=*.yml", "--include=*.json",
        "--include=*.toml", "--include=*.sql", "--include=*.html",
        "--include=*.css", "--include=*.sh", "--include=*.txt",
        "--include=*.proto", "--include=*.graphql",
        "--color=never",
        "-I",  # skip binary
        pattern,
        search_path,
    ]
    result = await _run(cmd, cwd=project_path, timeout=15)

    if "No such file" in result:
        return f"Path not found: {search_path}"

    count = len([l for l in result.split("\n") if l.strip()]) if result != "(empty output)" else 0
    return f"🔎 '{pattern}' ({count} matches)\n\n{result}"


# ---------------------------------------------------------------------------
# /blame <file> [start] [end]
# ---------------------------------------------------------------------------

async def blame(project_path: str, args: list[str]) -> str:
    """Show git blame. Usage: /blame <file> [start_line] [end_line]"""
    if not args:
        return "Usage: /blame <file> [start_line] [end_line]"

    file_path = args[0]
    target = _safe_path(project_path, file_path)
    if not target or not os.path.isfile(target):
        return f"File not found: {file_path}"

    cmd = ["git", "blame", "--date=short", file_path]

    if len(args) >= 3:
        try:
            start = int(args[1])
            end = int(args[2])
            cmd.extend([f"-L{start},{end}"])
        except ValueError:
            pass
    elif len(args) >= 2:
        try:
            start = int(args[1])
            cmd.extend([f"-L{start},{start + 39}"])
        except ValueError:
            pass

    result = await _run(cmd, cwd=project_path)
    return f"👤 Blame: {file_path}\n\n{result}"
