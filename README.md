# 🧠 CHIRAN Context Service + MCP Server

**Eliminates context loss between Claude sessions — fully automatic, zero manual steps.**

## How It Works

CHIRAN runs as a FastAPI service with an MCP (Model Context Protocol) endpoint. Once connected, Claude can directly invoke CHIRAN tools to load your project context, save session summaries, record decisions, and track sprint items. You never copy-paste anything again.

```
You open Claude → Claude calls chiran.generate_handoff() → Claude knows everything
You finish working → Claude calls chiran.create_session() → Context saved for next time
```

## Setup (One-Time, ~15 minutes)

### Step 1: Deploy CHIRAN

**Option A: Railway (recommended — alongside existing Fono backend)**
```bash
# Push to GitHub
cd chiran-context-service
git init && git add -A && git commit -m "CHIRAN context service"
gh repo create fono-chiran --private --push

# In Railway dashboard:
# 1. New Service > Deploy from GitHub repo
# 2. Set env vars:
#    DATABASE_URL = (your existing Fono PostgreSQL connection string from Doppler)
#    CHIRAN_PORT  = 8004
# 3. Deploy

# Once deployed, note your Railway URL:
# https://chiran-context-service-production-XXXX.up.railway.app
```

**Option B: Local (Docker)**
```bash
docker compose up -d
# CHIRAN available at http://localhost:8004
# MCP endpoint at http://localhost:8004/mcp
```

### Step 2: Seed Current State
```bash
# One-time: load Fono's current state into CHIRAN
curl -X POST https://your-chiran-url.railway.app/api/v1/seed
```

### Step 3: Connect Claude to CHIRAN

#### For claude.ai (this chat interface)
1. Click the **+** button in the chat input area
2. Go to **Connectors** > **Manage Connectors**
3. Click **Add custom connector**
4. Enter your CHIRAN URL: `https://your-chiran-url.railway.app/mcp`
5. Done. Claude can now call CHIRAN tools in every conversation.

#### For Claude Desktop
Add to your `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "chiran": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "https://your-chiran-url.railway.app/mcp"
      ]
    }
  }
}
```
Restart Claude Desktop. CHIRAN tools appear in the tools menu.

#### For Claude Code (terminal)
```bash
claude mcp add --transport http chiran https://your-chiran-url.railway.app/mcp
```

## What Claude Can Do Once Connected

Claude sees these tools automatically:

| Tool | What It Does | When Claude Uses It |
|------|-------------|-------------------|
| `generate_handoff` | Load full project context | **Start of every conversation** |
| `list_deployments` | See what's deployed vs planned | When discussing infrastructure |
| `create_decision` | Record a decision + rejected alternatives | After making a technical choice |
| `list_decisions` | Check existing decisions | Before suggesting anything |
| `list_sprint` | See current priorities | When planning work |
| `create_sprint_item` | Add new task | When new work is identified |
| `update_sprint_item` | Mark done/blocked | When work status changes |
| `create_session` | Save session summary | **End of every conversation** |
| `list_sessions` | Review past sessions | When needing historical context |
| `list_questions` | See unresolved questions | Before making assumptions |
| `resolve_question` | Mark question answered | When a question gets resolved |
| `upsert_code_state` | Track code files | When creating/modifying files |

## What Changes for You

### Before CHIRAN
1. Hit context limit
2. Open new chat
3. Spend 15 min writing handoff prompt
4. Claude still forgets decisions, re-suggests rejected ideas
5. Lose track of what's deployed vs planned

### After CHIRAN
1. Hit context limit
2. Open new chat
3. Claude automatically loads context via MCP
4. Zero information loss, zero manual steps

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   Claude Session                  │
│                                                  │
│  "What should I work on?"                        │
│       │                                          │
│       ▼                                          │
│  [MCP Tool Call: generate_handoff]               │
│       │                                          │
│       ▼                                          │
│  ┌──────────────────────────────────┐            │
│  │     CHIRAN Context Service       │            │
│  │     (FastAPI + MCP at /mcp)      │            │
│  │                                  │            │
│  │  /api/v1/context/handoff  ──► Deployments     │
│  │  /api/v1/decisions        ──► Decisions       │
│  │  /api/v1/sprint           ──► Sprint Items    │
│  │  /api/v1/sessions         ──► Session History │
│  │  /api/v1/questions        ──► Open Questions  │
│  │  /api/v1/code             ──► Code State      │
│  └──────────────┬───────────────────┘            │
│                 │                                │
│                 ▼                                │
│  ┌──────────────────────────────────┐            │
│  │     PostgreSQL (shared with      │            │
│  │     Fono backend on Railway)     │            │
│  └──────────────────────────────────┘            │
└──────────────────────────────────────────────────┘
```

## Relationship to Full CHIRAN

This is the lightweight v0.1 — focused purely on development continuity. The full CHIRAN super-orchestrator described in the architecture docs will eventually:

- Monitor all 37 Fono agents via IAD audit stream
- Issue daily WhatsApp briefings at 7am
- Surface critical decisions automatically
- Track agent trust scores and performance

This service becomes the **state backbone** that the full CHIRAN builds on top of. Nothing built here gets thrown away.

## API Docs

Once running, visit:
- **Swagger UI**: `https://your-url/docs`
- **MCP endpoint**: `https://your-url/mcp`
- **Health check**: `https://your-url/health`
