import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHATS = [
    int(x) for x in os.getenv("AUTHORIZED_CHAT_IDS", "").split(",") if x.strip()
]
AUTHORIZED_USERNAMES = [
    x.strip().lower().lstrip("@")
    for x in os.getenv("AUTHORIZED_USERNAMES", "criz_nguyen").split(",") if x.strip()
]
CLAUDE_PATH = os.getenv("CLAUDE_PATH", "claude")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "sonnet")
PROJECTS_DIR = os.path.expanduser(os.getenv("PROJECTS_DIR", "~/projects"))
DB_PATH = Path(__file__).parent / "data" / "bot.db"
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "120"))
TOKEN_THRESHOLD_PCT = 0.75
MAX_COST_PER_REQUEST = float(os.getenv("MAX_COST_PER_REQUEST", "1.0"))
# Auto-approve: max consecutive yes/no auto-replies before forcing user interaction
MAX_AUTO_APPROVE_ROUNDS = int(os.getenv("MAX_AUTO_APPROVE_ROUNDS", "50"))
# Proxy config for dev sub-agents
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "http://pro-x.io.vn/")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
PROXY_MODEL = os.getenv("PROXY_MODEL", "claude-sonnet-4-6")
# Approximate context window sizes per model family
MODEL_CONTEXT_WINDOWS = {
    "sonnet": 200_000,
    "opus": 200_000,
    "haiku": 200_000,
}
