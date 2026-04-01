# Telegram Claude Bot

A Telegram bot that bridges to [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) with multi-model routing, streaming responses, and persistent memory.

## Features

- **Multi-model routing** — Haiku classifies intent, Opus handles complex tasks (tech lead), Sonnet does heavy coding via proxy
- **Streaming responses** — See Claude's thinking, tool usage, and text output in real-time
- **Session management** — Persistent sessions with auto-recovery when CLI sessions expire
- **Neural-memory integration** — Saves and recalls knowledge across sessions via [neural-memory MCP](https://github.com/nhadaututtheky/neural-memory)
- **Auto-approve** — Automatically answers yes/no confirmation prompts from Claude
- **Typing indicator** — Shows "typing..." while Claude is processing
- **Cost tracking** — Per-session and total cost monitoring
- **Project switching** — Work on multiple projects from the same chat

## Architecture

```
Telegram Chat
    │
    ▼
┌─────────────────────┐
│   Intent Router     │  ◄── Haiku: classify simple/complex/continue
│   (Haiku, direct)   │
└────────┬────────────┘
         │
    simple │ complex/continue
         │     │
         ▼     ▼
      Haiku  ┌──────────────────┐
      reply  │   Tech Lead      │  ◄── Opus: plan, review, architect
             │   (Opus, direct) │
             └────────┬─────────┘
                      │
                      │ delegates coding
                      ▼
             ┌──────────────────┐
             │   Dev Agent      │  ◄── Sonnet: implement specs
             │ (Sonnet, proxy)  │
             └──────────────────┘
```

## Prerequisites

- **Python 3.11+**
- **[Claude CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated
- **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/criznguyen/telegram-claude-bot.git
cd telegram-claude-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Find your Claude CLI path

```bash
which claude
# Example output: /home/youruser/.local/bin/claude
```

### 4. Create a Telegram bot

1. Open Telegram, search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 5. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
# Required
TELEGRAM_BOT_TOKEN=your-bot-token-from-botfather
CLAUDE_PATH=/home/youruser/.local/bin/claude

# Authorization (comma-separated)
AUTHORIZED_USERNAMES=your_telegram_username
AUTHORIZED_CHAT_IDS=

# Model config
DEFAULT_MODEL=opus

# Timeouts & limits (0 = unlimited)
CLAUDE_TIMEOUT=0
MAX_COST_PER_REQUEST=0

# Optional: Proxy for dev sub-agents (saves tokens on heavy coding)
# PROXY_API_KEY=your-proxy-api-key
# PROXY_MODEL=claude-sonnet-4-6
```

### 6. Run the bot

**Direct:**

```bash
python bot.py
```

**As a systemd service (recommended):**

```bash
# Edit the service file — update paths to match your system
nano telegram-claude-bot.service

# Install and start
sudo cp telegram-claude-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-claude-bot

# Check status
sudo systemctl status telegram-claude-bot
sudo journalctl -u telegram-claude-bot -f
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Start the bot |
| `/help` | Show help |
| `/projects` | List available projects |
| `/project <path>` | Switch working directory |
| `/model <name>` | Switch model (opus/sonnet/haiku) |
| `/reset` | End current session & start new |
| `/history` | Show recent messages |
| `/recall <query>` | Search neural-memory |
| `/remember <text>` | Save to neural-memory |
| `/cost` | Show usage costs |
| `/status` | Current session info |

## Multi-Model Routing

The bot uses a 3-tier model strategy to optimize cost vs quality:

| Task Type | Model | Route | Cost |
|-----------|-------|-------|------|
| Simple Q&A, greetings, explanations | Haiku | Direct | ~$0.001 |
| Architecture, planning, code review | Opus | Direct | ~$0.15 |
| Heavy coding, test writing | Sonnet | Proxy | ~$0.03 |

**How it works:**

1. Every message is first classified by **Haiku** (~15ms, ~$0.001)
2. Simple messages are answered by Haiku directly
3. Complex tasks go to **Opus** (tech lead) which can:
   - Handle it directly (small fixes, architecture, review)
   - Delegate to **Sonnet** via proxy for heavy coding with detailed specs
   - Review Sonnet's output and iterate if needed

### Proxy Setup (Optional)

The proxy route is optional. Without it, Opus handles everything directly. To enable:

1. Set `PROXY_API_KEY` and `PROXY_MODEL` in `.env`
2. The tech lead (Opus) will automatically delegate coding tasks to Sonnet via proxy

## Streaming

The bot streams Claude's output to Telegram in real-time:

- Thinking preview (last 300 chars)
- Tool usage indicators
- Text response as it's generated
- Typing indicator while processing

Messages are updated every ~1.5s (Telegram API rate limit).

## Session Recovery

If the Claude CLI session expires (CLI restart, update, cleanup), the bot automatically:

1. Detects "No conversation found" error
2. Recovers context from the database (last 10 messages)
3. Recalls relevant knowledge from neural-memory
4. Creates a new CLI session with recovered context

Sessions persist until you manually `/reset`.

## Project Structure

```
telegram-claude-bot/
├── bot.py                  # Main bot logic, handlers, streaming
├── claude_bridge.py        # Claude CLI subprocess (batch + streaming)
├── config.py               # Environment config
├── context_manager.py      # System prompts, session recovery
├── db.py                   # SQLite session & message storage
├── intent_router.py        # Haiku intent classification
├── question_detector.py    # Detect yes/no and option questions
├── requirements.txt        # Python dependencies
├── telegram-claude-bot.service  # systemd unit file
├── .env.example            # Environment template
└── .env                    # Your config (not committed)
```

## License

MIT
