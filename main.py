"""
CHIRAN Context Service — Lightweight Super-Orchestrator for Fono
================================================================
Solves the #1 development bottleneck: context loss between Claude sessions.

CHIRAN maintains a structured, searchable record of:
1. DEPLOYMENT STATE — what's live vs planned vs in-progress
2. DECISIONS — what was decided and WHY (so Claude never re-suggests rejected ideas)
3. SPRINT STATE — current priorities, blockers, next actions
4. SESSION HISTORY — compressed summaries of every work session

Designed to match Fono's existing stack:
- FastAPI (async, Pydantic v2)
- PostgreSQL (via asyncpg)
- JWT auth (same pattern as api-service)
- Deployable on Railway alongside existing services

Usage (MCP — fully automatic, no manual steps):
- Add CHIRAN as a remote MCP connector in Claude.ai or Claude Code
- Claude automatically calls CHIRAN tools to load context, save sessions, record decisions
- No copy-paste, no curl, no manual handoffs

Usage (REST — fallback):
- GET /api/v1/context/handoff → generates handoff document
- POST /api/v1/sessions → save session summary
- POST /api/v1/decisions → record decisions

MCP endpoint: /mcp (auto-generated from all FastAPI routes)
Port: 8004 (after voice-service:8001, api-service:8002, audit-agent:8003)
"""

import os
import json
import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, ConfigDict
from uuid import UUID as _UUID


# =============================================================================
# CONFIGURATION
# =============================================================================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/fono"
)
PORT = int(os.getenv("CHIRAN_PORT", "8004"))


# =============================================================================
# DATABASE SETUP
# =============================================================================

db_pool: Optional[asyncpg.Pool] = None


async def get_db() -> asyncpg.Pool:
    """Dependency: return the connection pool."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database not ready")
    return db_pool


SCHEMA_SQL = """
-- ============================================================
-- CHIRAN Context Service Schema
-- Designed for Fono development continuity
-- ============================================================

-- 1. DEPLOYMENT STATE: What's actually running in production
CREATE TABLE IF NOT EXISTS deployment_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    component       TEXT NOT NULL,           -- e.g. 'data-collection-pipeline', 'api-service'
    status          TEXT NOT NULL DEFAULT 'planned',  -- planned | in_progress | deployed | deprecated
    environment     TEXT NOT NULL DEFAULT 'production', -- production | staging | local
    version         TEXT,                    -- e.g. 'phase0-step2', 'v0.1.0'
    details         JSONB NOT NULL DEFAULT '{}',  -- flexible metadata
    deployed_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(component, environment)
);

-- 2. DECISIONS: What was decided and WHY
CREATE TABLE IF NOT EXISTS decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,           -- e.g. 'FastAPI over OpenClaw'
    category        TEXT NOT NULL DEFAULT 'technical', -- technical | architectural | strategic | provider
    decision        TEXT NOT NULL,           -- what was decided
    reasoning       TEXT NOT NULL,           -- WHY (this is the critical part)
    alternatives    JSONB DEFAULT '[]',      -- what was considered and rejected
    status          TEXT NOT NULL DEFAULT 'active', -- active | superseded | revisit
    session_id      UUID,                    -- which session this came from
    decided_at      TIMESTAMPTZ DEFAULT now(),
    revisit_after   TIMESTAMPTZ              -- optional: when to re-evaluate
);

CREATE INDEX IF NOT EXISTS idx_decisions_category ON decisions(category);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);

-- 3. SPRINT STATE: Current priorities and blockers
CREATE TABLE IF NOT EXISTS sprint_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    description     TEXT,
    priority        INTEGER NOT NULL DEFAULT 3,  -- 1=critical, 2=high, 3=medium, 4=low
    status          TEXT NOT NULL DEFAULT 'todo', -- todo | in_progress | blocked | done | cancelled
    blocker         TEXT,                    -- what's blocking this
    depends_on      UUID[],                 -- other sprint_item IDs
    session_id      UUID,                   -- which session created/updated this
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sprint_status ON sprint_items(status);
CREATE INDEX IF NOT EXISTS idx_sprint_priority ON sprint_items(priority);

-- 4. SESSION HISTORY: Compressed summaries of every Claude work session
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_number  SERIAL,                 -- auto-incrementing for easy reference
    title           TEXT NOT NULL,           -- e.g. 'Phase 0 Step 2 Backend Deploy'
    summary         TEXT NOT NULL,           -- what was accomplished
    key_outputs     JSONB DEFAULT '[]',      -- files created, endpoints built, etc.
    decisions_made  UUID[],                  -- links to decisions table
    problems_hit    JSONB DEFAULT '[]',      -- errors, blockers encountered
    next_actions    JSONB DEFAULT '[]',      -- what the NEXT session should do
    context_for_next TEXT,                   -- free-text context that must carry forward
    tools_used      TEXT[],                  -- e.g. ['claude_code', 'claude_chat', 'manual']
    duration_mins   INTEGER,                -- approximate session length
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

-- 5. CODE STATE: Track what files/modules exist and their purpose
CREATE TABLE IF NOT EXISTS code_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path       TEXT NOT NULL UNIQUE,    -- e.g. 'apps/voice-service/main.py'
    purpose         TEXT NOT NULL,           -- what this file does
    status          TEXT NOT NULL DEFAULT 'active', -- active | stale | planned | deleted
    last_modified   TIMESTAMPTZ DEFAULT now(),
    session_id      UUID,                   -- which session last touched it
    notes           TEXT                     -- any important context
);

CREATE INDEX IF NOT EXISTS idx_code_state_status ON code_state(status);

-- 6. OPEN QUESTIONS: Things that need to be resolved
CREATE TABLE IF NOT EXISTS open_questions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question        TEXT NOT NULL,
    context         TEXT,                    -- why this matters
    category        TEXT DEFAULT 'technical',
    status          TEXT NOT NULL DEFAULT 'open', -- open | answered | deferred
    answer          TEXT,                    -- when resolved
    session_id      UUID,                   -- which session raised this
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_questions_status ON open_questions(status);

-- ============================================================
-- KNOWLEDGE LAYER (DocAgent integrated into CHIRAN Phase 0)
-- From ARCH-003 DocAgent Architecture + ARCH-008 Section 7
-- ============================================================

-- 7. DOCUMENT NODES: Every piece of knowledge is a node
CREATE TABLE IF NOT EXISTS doc_nodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id          TEXT NOT NULL,               -- e.g. 'ARCH-001', 'ARCH-008'
    type            TEXT NOT NULL,               -- document | section | schema | agent | technology | phase
    name            TEXT NOT NULL,               -- human-readable name
    content         JSONB NOT NULL DEFAULT '{}', -- structured content (not raw prose)
    metadata        JSONB DEFAULT '{}',          -- audience, tags, freshness_score, page_count
    version         INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'current',      -- current | superseded | archived | draft
    updated_by      TEXT DEFAULT 'manual',       -- human or agent ID
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_nodes_doc_id ON doc_nodes(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_nodes_type ON doc_nodes(type);
CREATE INDEX IF NOT EXISTS idx_doc_nodes_status ON doc_nodes(status);

-- 8. DOCUMENT EDGES: Relationships between nodes
CREATE TABLE IF NOT EXISTS doc_edges (
    from_node       UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    to_node         UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,               -- depends_on | supersedes | contains | references
    metadata        JSONB DEFAULT '{}',
    PRIMARY KEY (from_node, to_node, relation)
);

-- 9. DOCUMENT VERSIONS: Change history for every node
CREATE TABLE IF NOT EXISTS doc_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id         UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    content         JSONB NOT NULL,              -- snapshot at this version
    diff_summary    TEXT,                        -- human-readable description of change
    changed_by      TEXT,
    changed_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_versions_node ON doc_versions(node_id);

-- 10. STALENESS TRACKING: Detect when docs diverge from reality
CREATE TABLE IF NOT EXISTS doc_staleness (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id          TEXT NOT NULL,               -- which document
    node_id         UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    issue           TEXT NOT NULL,               -- what's stale
    severity        TEXT DEFAULT 'warning',      -- info | warning | critical
    detected_by     TEXT DEFAULT 'staleness_checker', -- which check found it
    status          TEXT DEFAULT 'open',         -- open | resolved | ignored
    detected_at     TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_staleness_status ON doc_staleness(status);

-- 11. COST EVENTS: Track API and compute costs (Phase 0 cost monitoring)
CREATE TABLE IF NOT EXISTS cost_events (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT,                        -- which agent incurred the cost
    division        TEXT,                        -- which division
    provider        TEXT NOT NULL,               -- 'anthropic' | 'twilio' | 'railway' | 'elevenlabs' | etc.
    operation       TEXT,                        -- 'claude_api_call' | 'twilio_call_minute' | etc.
    cost_usd        NUMERIC(10,6) NOT NULL,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_events_date ON cost_events(created_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_division ON cost_events(division);
"""


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

# --- Helpers for Postgres → Pydantic type conversion ---
def _to_str(v):
    """Convert UUID objects (or any value) to string."""
    if v is None:
        return v
    return str(v)

def _to_list(v):
    """Convert JSON text strings from Postgres to Python lists."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return []
    if v is None:
        return []
    return v

def _to_dict(v):
    """Convert JSON text strings from Postgres to Python dicts."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return {}
    if v is None:
        return {}
    return v

# --- Deployment State ---
class DeploymentStatus(str, Enum):
    planned = "planned"
    in_progress = "in_progress"
    deployed = "deployed"
    deprecated = "deprecated"

class DeploymentCreate(BaseModel):
    component: str = Field(..., examples=["data-collection-pipeline"])
    status: DeploymentStatus = DeploymentStatus.planned
    environment: str = Field(default="production")
    version: str | None = None
    details: dict = Field(default_factory=dict)

class DeploymentOut(DeploymentCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    deployed_at: datetime | None = None
    updated_at: datetime

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)

    @field_validator('details', mode='before')
    @classmethod
    def _fix_details(cls, v):
        return _to_dict(v)

# --- Decisions ---
class DecisionCreate(BaseModel):
    title: str = Field(..., examples=["FastAPI over OpenClaw"])
    category: str = Field(default="technical")
    decision: str = Field(..., examples=["Use FastAPI for all backend services"])
    reasoning: str = Field(..., examples=["Better latency control for realtime voice pipeline"])
    alternatives: list[dict] = Field(default_factory=list, examples=[[
        {"name": "OpenClaw", "rejected_because": "Personal assistant, not production telephony"},
        {"name": "Node.js + Fastify", "rejected_because": "Python ecosystem better for AI/ML"}
    ]])
    status: str = Field(default="active")
    revisit_after: datetime | None = None

class DecisionOut(DecisionCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    session_id: str | None = None
    decided_at: datetime

    @field_validator('id', 'session_id', mode='before')
    @classmethod
    def _fix_ids(cls, v):
        return _to_str(v)

    @field_validator('alternatives', mode='before')
    @classmethod
    def _fix_alternatives(cls, v):
        return _to_list(v)

# --- Sprint Items ---
class SprintItemCreate(BaseModel):
    title: str
    description: str | None = None
    priority: int = Field(default=3, ge=1, le=4)
    status: str = Field(default="todo")
    blocker: str | None = None

class SprintItemUpdate(BaseModel):
    status: str | None = None
    blocker: str | None = None
    priority: int | None = None

class SprintItemOut(SprintItemCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)

# --- Sessions ---
class SessionCreate(BaseModel):
    title: str = Field(..., examples=["Phase 0 Step 2 Backend Deploy"])
    summary: str = Field(..., examples=["Deployed dashboard APIs, SSE events, JWT middleware"])
    key_outputs: list[dict] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    problems_hit: list[dict] = Field(default_factory=list)
    next_actions: list[dict] = Field(default_factory=list)
    context_for_next: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    duration_mins: int | None = None

class SessionOut(SessionCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    session_number: int
    started_at: datetime
    ended_at: datetime | None = None

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)

    @field_validator('key_outputs', 'problems_hit', 'next_actions', mode='before')
    @classmethod
    def _fix_json_lists(cls, v):
        return _to_list(v)

    @field_validator('decisions_made', 'tools_used', mode='before')
    @classmethod
    def _fix_arrays(cls, v):
        if v is None:
            return []
        return v

# --- Code State ---
class CodeStateCreate(BaseModel):
    file_path: str
    purpose: str
    status: str = Field(default="active")
    notes: str | None = None

class CodeStateOut(CodeStateCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    last_modified: datetime

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)

# --- Open Questions ---
class OpenQuestionCreate(BaseModel):
    question: str
    context: str | None = None
    category: str = Field(default="technical")

class OpenQuestionResolve(BaseModel):
    answer: str

class OpenQuestionOut(OpenQuestionCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: str
    answer: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)


# =============================================================================
# APP LIFECYCLE
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create pool + schema + MCP session manager. Shutdown: close pool."""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    print(f"🧠 CHIRAN Context Service ready on port {PORT}")
    
    # Start MCP session manager if available
    try:
        from mcp.server.fastmcp import FastMCP as _MCP
        async with mcp_server.session_manager.run():
            yield
    except Exception:
        yield
    
    await db_pool.close()


app = FastAPI(
    title="CHIRAN Context Service",
    description="Lightweight super-orchestrator for Fono development continuity",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# MCP SERVER — Official MCP SDK with Streamable HTTP transport
# =============================================================================
# Uses mcp.server.fastmcp.FastMCP with stateless_http=True
# Claude.ai, Claude Desktop, and Claude Code connect to /mcp
# Tools are explicitly defined (not auto-generated from REST endpoints)

try:
    from mcp.server.fastmcp import FastMCP as MCPServer
    
    mcp_server = MCPServer("Chiran", stateless_http=True)
    
    @mcp_server.tool()
    async def generate_handoff(max_sessions: int = 3, include_code: bool = False) -> str:
        """Load full Fono project context. CALL THIS AT THE START OF EVERY CONVERSATION.
        Returns deployment state, decisions, sprint priorities, recent sessions, open questions, and doc health."""
        import urllib.request, urllib.error
        try:
            url = f"http://localhost:{PORT}/api/v1/context/handoff?max_sessions={max_sessions}&include_code={str(include_code).lower()}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("handoff", "Error: no handoff content")
        except Exception as e:
            return f"Error generating handoff: {e}"
    
    @mcp_server.tool()
    async def save_session(title: str, summary: str, next_actions: str = "[]", context_for_next: str = "", tools_used: str = "claude_chat", duration_mins: int = 60) -> str:
        """Save a session summary at the END of every conversation. Records what was accomplished, next actions, and context for the next session."""
        import urllib.request
        try:
            body = json.dumps({
                "title": title, "summary": summary,
                "next_actions": json.loads(next_actions) if next_actions.startswith("[") else [{"action": next_actions}],
                "context_for_next": context_for_next,
                "tools_used": [t.strip() for t in tools_used.split(",")],
                "duration_mins": duration_mins,
            }).encode()
            req = urllib.request.Request(f"http://localhost:{PORT}/api/v1/sessions", data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return f"Session #{data.get('session_number', '?')} saved: {title}"
        except Exception as e:
            return f"Error saving session: {e}"
    
    @mcp_server.tool()
    async def record_decision(title: str, decision: str, reasoning: str, category: str = "technical", alternatives: str = "[]") -> str:
        """Record a technical or strategic decision with reasoning and rejected alternatives. Prevents Claude from re-suggesting rejected ideas in future sessions."""
        import urllib.request
        try:
            body = json.dumps({
                "title": title, "category": category, "decision": decision,
                "reasoning": reasoning,
                "alternatives": json.loads(alternatives) if alternatives.startswith("[") else [],
            }).encode()
            req = urllib.request.Request(f"http://localhost:{PORT}/api/v1/decisions", data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return f"Decision recorded: {title}"
        except Exception as e:
            return f"Error recording decision: {e}"
    
    @mcp_server.tool()
    async def list_decisions(status: str = "active") -> str:
        """List all active decisions. Check this before suggesting any technical approach to avoid re-suggesting rejected ideas."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://localhost:{PORT}/api/v1/decisions?status={status}", timeout=10) as resp:
                decisions = json.loads(resp.read().decode())
                if not decisions:
                    return "No active decisions recorded."
                lines = []
                for d in decisions:
                    lines.append(f"- {d['title']}: {d['decision']} (Why: {d['reasoning']})")
                return "\n".join(lines)
        except Exception as e:
            return f"Error listing decisions: {e}"
    
    @mcp_server.tool()
    async def list_sprint(status: str = "") -> str:
        """List current sprint items and priorities. Shows what to work on next."""
        import urllib.request
        try:
            url = f"http://localhost:{PORT}/api/v1/sprint"
            if status:
                url += f"?status={status}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                items = json.loads(resp.read().decode())
                if not items:
                    return "No active sprint items."
                prio_map = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
                lines = []
                for s in items:
                    blocker = f" ⚠️ BLOCKED: {s['blocker']}" if s.get('blocker') else ""
                    lines.append(f"[{s['status']}] {prio_map.get(s['priority'], '?')}: {s['title']}{blocker}")
                return "\n".join(lines)
        except Exception as e:
            return f"Error listing sprint: {e}"
    
    @mcp_server.tool()
    async def list_deployments() -> str:
        """List all deployment states — what's live, in progress, or planned."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://localhost:{PORT}/api/v1/deployments", timeout=10) as resp:
                deps = json.loads(resp.read().decode())
                if not deps:
                    return "No deployments recorded."
                lines = []
                for d in deps:
                    lines.append(f"{'✅' if d['status']=='deployed' else '📋'} {d['component']} [{d['status']}] {d.get('version', '')}")
                return "\n".join(lines)
        except Exception as e:
            return f"Error listing deployments: {e}"
    
    @mcp_server.tool()
    async def list_open_questions() -> str:
        """List unresolved questions. Check this before making assumptions."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://localhost:{PORT}/api/v1/questions?status=open", timeout=10) as resp:
                questions = json.loads(resp.read().decode())
                if not questions:
                    return "No open questions."
                lines = []
                for q in questions:
                    lines.append(f"? {q['question']}")
                    if q.get('context'):
                        lines.append(f"  Context: {q['context']}")
                return "\n".join(lines)
        except Exception as e:
            return f"Error listing questions: {e}"
    
    @mcp_server.tool()
    async def search_docs(query: str) -> str:
        """Search the document knowledge graph. Find which architecture docs mention a specific topic."""
        import urllib.request, urllib.parse
        try:
            encoded = urllib.parse.quote(query)
            with urllib.request.urlopen(f"http://localhost:{PORT}/api/v1/docs/search?q={encoded}", timeout=10) as resp:
                data = json.loads(resp.read().decode())
                results = data.get("results", [])
                if not results:
                    return f"No documents found matching '{query}'."
                lines = [f"Found {len(results)} results for '{query}':"]
                for r in results:
                    lines.append(f"- [{r['doc_id']}] {r['name']} ({r['type']})")
                return "\n".join(lines)
        except Exception as e:
            return f"Error searching docs: {e}"
    
    @mcp_server.tool()
    async def get_doc_manifest() -> str:
        """Get the live document manifest — all architecture docs, their versions, and status."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://localhost:{PORT}/api/v1/docs/manifest", timeout=10) as resp:
                data = json.loads(resp.read().decode())
                docs = data.get("documents", [])
                lines = [f"Document Manifest ({len(docs)} documents):"]
                for d in docs:
                    lines.append(f"- {d['doc_id']}: {d['name']} [v{d.get('version', '?')}] ({d.get('status', '?')})")
                return "\n".join(lines)
        except Exception as e:
            return f"Error getting manifest: {e}"

    # Mount MCP Streamable HTTP app at /mcp
    app.mount("/mcp", mcp_server.streamable_http_app())
    print("🔌 MCP server mounted at /mcp (Streamable HTTP)")

except ImportError:
    print("⚠️  mcp SDK not installed — MCP endpoint disabled. REST API still works.")
    print("   Install with: pip install mcp")


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.get("/health")
async def health(pool: asyncpg.Pool = Depends(get_db)):
    """Health check with DB connectivity test."""
    row = await pool.fetchval("SELECT count(*) FROM sessions")
    return {
        "status": "healthy",
        "service": "chiran-context",
        "total_sessions": row,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =============================================================================
# 🔑 THE CRITICAL ENDPOINT: HANDOFF CONTEXT GENERATOR
# =============================================================================

@app.get("/api/v1/context/handoff")
async def generate_handoff(
    pool: asyncpg.Pool = Depends(get_db),
    max_sessions: int = Query(default=3, description="How many recent sessions to include"),
    include_code: bool = Query(default=False, description="Include code state map"),
):
    """
    THE MOST IMPORTANT ENDPOINT.
    
    Generates a structured handoff document that you paste into a new Claude session.
    This is what eliminates context loss.
    
    Usage: Copy the output and paste it at the start of every new Claude chat.
    """
    
    # 1. Current deployment state
    deployments = await pool.fetch(
        "SELECT component, status, environment, version, details, deployed_at "
        "FROM deployment_state ORDER BY component"
    )
    
    # 2. Active decisions (never re-suggest these)
    decisions = await pool.fetch(
        "SELECT title, category, decision, reasoning, alternatives, status "
        "FROM decisions WHERE status = 'active' ORDER BY decided_at DESC"
    )
    
    # 3. Current sprint
    sprint = await pool.fetch(
        "SELECT title, description, priority, status, blocker "
        "FROM sprint_items WHERE status IN ('todo', 'in_progress', 'blocked') "
        "ORDER BY priority, created_at"
    )
    
    # 4. Recent sessions
    sessions = await pool.fetch(
        "SELECT session_number, title, summary, key_outputs, next_actions, "
        "context_for_next, problems_hit, started_at "
        "FROM sessions ORDER BY started_at DESC LIMIT $1",
        max_sessions
    )
    
    # 5. Open questions
    questions = await pool.fetch(
        "SELECT question, context, category "
        "FROM open_questions WHERE status = 'open' ORDER BY created_at"
    )
    
    # 6. Code state (optional)
    code_map = []
    if include_code:
        code_map = await pool.fetch(
            "SELECT file_path, purpose, status, notes "
            "FROM code_state WHERE status = 'active' ORDER BY file_path"
        )
    
    # 7. Document health (Knowledge Layer)
    doc_staleness = await pool.fetch(
        "SELECT doc_id, issue, severity FROM doc_staleness WHERE status = 'open' ORDER BY severity DESC LIMIT 10"
    )
    doc_count = await pool.fetchval(
        "SELECT count(*) FROM doc_nodes WHERE type = 'document' AND status = 'current'"
    )
    
    # 8. Cost summary (last 7 days)
    cost_total = await pool.fetchval(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_events WHERE created_at > now() - interval '7 days'"
    )
    
    # Build the handoff document
    handoff = _build_handoff_document(
        deployments, decisions, sprint, sessions, questions, code_map,
        doc_staleness, doc_count, float(cost_total),
    )
    
    return {
        "handoff": handoff,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "deployments": len(deployments),
            "active_decisions": len(decisions),
            "sprint_items": len(sprint),
            "recent_sessions": len(sessions),
            "open_questions": len(questions),
            "documents_tracked": doc_count,
            "open_staleness_issues": len(doc_staleness),
            "cost_7d_usd": float(cost_total),
        },
        "usage": "Copy the 'handoff' field content and paste at the start of a new Claude session."
    }


def _build_handoff_document(
    deployments, decisions, sprint, sessions, questions, code_map,
    doc_staleness=None, doc_count=0, cost_7d=0.0,
) -> str:
    """Build the structured handoff document for Claude."""
    
    lines = []
    lines.append("=" * 70)
    lines.append("FONO (formerly VoiceOrder) — CHIRAN CONTEXT HANDOFF")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 70)
    lines.append("")
    
    # --- DEPLOYMENT STATE ---
    lines.append("## WHAT'S DEPLOYED (Production State)")
    lines.append("")
    if deployments:
        for d in deployments:
            status_emoji = {
                "deployed": "✅", "in_progress": "🔧", 
                "planned": "📋", "deprecated": "⛔"
            }.get(d["status"], "❓")
            line = f"  {status_emoji} {d['component']} [{d['status']}]"
            if d["version"]:
                line += f" — {d['version']}"
            if d["details"]:
                details = json.loads(d["details"]) if isinstance(d["details"], str) else d["details"]
                if details:
                    key_info = ", ".join(f"{k}: {v}" for k, v in list(details.items())[:3])
                    line += f" ({key_info})"
            lines.append(line)
    else:
        lines.append("  (no deployments recorded yet)")
    lines.append("")
    
    # --- DECISIONS (DO NOT RE-SUGGEST) ---
    lines.append("## DECISIONS MADE (Do NOT re-suggest alternatives)")
    lines.append("")
    if decisions:
        for d in decisions:
            lines.append(f"  ✓ {d['title']}")
            lines.append(f"    Decision: {d['decision']}")
            lines.append(f"    Why: {d['reasoning']}")
            alts = json.loads(d["alternatives"]) if isinstance(d["alternatives"], str) else d["alternatives"]
            if alts:
                rejected = ", ".join(
                    a.get("name", "?") for a in alts
                )
                lines.append(f"    Rejected: {rejected}")
            lines.append("")
    else:
        lines.append("  (no decisions recorded yet)")
    lines.append("")
    
    # --- CURRENT SPRINT ---
    lines.append("## CURRENT SPRINT (Active Priorities)")
    lines.append("")
    if sprint:
        priority_labels = {1: "🔴 CRITICAL", 2: "🟠 HIGH", 3: "🟡 MEDIUM", 4: "🟢 LOW"}
        for s in sprint:
            status_marker = {"todo": "[ ]", "in_progress": "[→]", "blocked": "[✗]"}.get(s["status"], "[ ]")
            prio = priority_labels.get(s["priority"], "")
            line = f"  {status_marker} {prio}: {s['title']}"
            if s["blocker"]:
                line += f" ⚠️ BLOCKED: {s['blocker']}"
            lines.append(line)
            if s["description"]:
                lines.append(f"       {s['description']}")
    else:
        lines.append("  (no sprint items)")
    lines.append("")
    
    # --- RECENT SESSIONS ---
    lines.append("## RECENT SESSIONS (What happened recently)")
    lines.append("")
    if sessions:
        for s in sessions:
            date_str = s["started_at"].strftime("%Y-%m-%d") if s["started_at"] else "?"
            lines.append(f"  Session #{s['session_number']} ({date_str}): {s['title']}")
            lines.append(f"    Summary: {s['summary']}")
            
            next_actions = json.loads(s["next_actions"]) if isinstance(s["next_actions"], str) else s["next_actions"]
            if next_actions:
                lines.append(f"    Next actions: {json.dumps(next_actions)}")
            
            if s["context_for_next"]:
                lines.append(f"    ⚡ Context: {s['context_for_next']}")
            
            problems = json.loads(s["problems_hit"]) if isinstance(s["problems_hit"], str) else s["problems_hit"]
            if problems:
                lines.append(f"    Problems: {json.dumps(problems)}")
            lines.append("")
    else:
        lines.append("  (no sessions recorded yet)")
    lines.append("")
    
    # --- OPEN QUESTIONS ---
    lines.append("## OPEN QUESTIONS (Unresolved)")
    lines.append("")
    if questions:
        for q in questions:
            lines.append(f"  ? {q['question']}")
            if q["context"]:
                lines.append(f"    Context: {q['context']}")
    else:
        lines.append("  (no open questions)")
    lines.append("")
    
    # --- CODE STATE (optional) ---
    if code_map:
        lines.append("## CODE STATE (Active Files)")
        lines.append("")
        for c in code_map:
            lines.append(f"  {c['file_path']} — {c['purpose']}")
            if c["notes"]:
                lines.append(f"    Note: {c['notes']}")
        lines.append("")
    
    # --- DOCUMENT HEALTH (Knowledge Layer) ---
    lines.append(f"## DOCUMENT HEALTH ({doc_count} docs tracked)")
    lines.append("")
    if doc_staleness:
        for s in doc_staleness:
            sev_emoji = {"critical": "🔴", "warning": "🟡", "info": "ℹ️"}.get(s["severity"], "❓")
            lines.append(f"  {sev_emoji} [{s['doc_id']}] {s['issue']}")
    else:
        lines.append("  ✅ All documents up to date")
    lines.append("")
    
    # --- COST SUMMARY ---
    if cost_7d > 0:
        lines.append(f"## COST SUMMARY (Last 7 days: ${cost_7d:.2f})")
        lines.append("")
    
    lines.append("=" * 70)
    lines.append("END CHIRAN HANDOFF — Use this as context for the new session")
    lines.append("=" * 70)
    
    return "\n".join(lines)


# =============================================================================
# DEPLOYMENT STATE ENDPOINTS
# =============================================================================

@app.get("/api/v1/deployments", response_model=list[DeploymentOut])
async def list_deployments(
    status: str | None = None,
    pool: asyncpg.Pool = Depends(get_db),
):
    """List all deployment states, optionally filtered by status."""
    if status:
        rows = await pool.fetch(
            "SELECT * FROM deployment_state WHERE status = $1 ORDER BY component", status
        )
    else:
        rows = await pool.fetch("SELECT * FROM deployment_state ORDER BY component")
    return [dict(r) for r in rows]


@app.post("/api/v1/deployments", response_model=DeploymentOut, status_code=201)
async def upsert_deployment(
    body: DeploymentCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Create or update a deployment record. Upserts on (component, environment)."""
    deployed_at = datetime.now(timezone.utc) if body.status == DeploymentStatus.deployed else None
    
    row = await pool.fetchrow("""
        INSERT INTO deployment_state (component, status, environment, version, details, deployed_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
        ON CONFLICT (component, environment) DO UPDATE SET
            status = $2, version = $4, details = $5::jsonb,
            deployed_at = COALESCE($6, deployment_state.deployed_at),
            updated_at = now()
        RETURNING *
    """, body.component, body.status.value, body.environment,
        body.version, json.dumps(body.details), deployed_at)
    
    return dict(row)


# =============================================================================
# DECISIONS ENDPOINTS
# =============================================================================

@app.get("/api/v1/decisions", response_model=list[DecisionOut])
async def list_decisions(
    status: str = Query(default="active"),
    category: str | None = None,
    pool: asyncpg.Pool = Depends(get_db),
):
    """List decisions. Default: active only (what Claude must respect)."""
    query = "SELECT * FROM decisions WHERE status = $1"
    params = [status]
    if category:
        query += " AND category = $2"
        params.append(category)
    query += " ORDER BY decided_at DESC"
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


@app.post("/api/v1/decisions", response_model=DecisionOut, status_code=201)
async def create_decision(
    body: DecisionCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Record a new decision. This prevents Claude from re-suggesting rejected alternatives."""
    row = await pool.fetchrow("""
        INSERT INTO decisions (title, category, decision, reasoning, alternatives, status, revisit_after)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        RETURNING *
    """, body.title, body.category, body.decision, body.reasoning,
        json.dumps(body.alternatives), body.status, body.revisit_after)
    return dict(row)


@app.patch("/api/v1/decisions/{decision_id}")
async def update_decision_status(
    decision_id: str,
    status: str = Query(..., description="active | superseded | revisit"),
    pool: asyncpg.Pool = Depends(get_db),
):
    """Change a decision's status (e.g., mark as superseded)."""
    row = await pool.fetchrow(
        "UPDATE decisions SET status = $2 WHERE id = $1::uuid RETURNING id, title, status",
        decision_id, status
    )
    if not row:
        raise HTTPException(404, "Decision not found")
    return dict(row)


# =============================================================================
# SPRINT ENDPOINTS
# =============================================================================

@app.get("/api/v1/sprint", response_model=list[SprintItemOut])
async def list_sprint(
    status: str | None = None,
    pool: asyncpg.Pool = Depends(get_db),
):
    """List sprint items. No filter = all active (todo/in_progress/blocked)."""
    if status:
        rows = await pool.fetch(
            "SELECT * FROM sprint_items WHERE status = $1 ORDER BY priority, created_at", status
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM sprint_items WHERE status IN ('todo','in_progress','blocked') "
            "ORDER BY priority, created_at"
        )
    return [dict(r) for r in rows]


@app.post("/api/v1/sprint", response_model=SprintItemOut, status_code=201)
async def create_sprint_item(
    body: SprintItemCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Add a new sprint item."""
    row = await pool.fetchrow("""
        INSERT INTO sprint_items (title, description, priority, status, blocker)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
    """, body.title, body.description, body.priority, body.status, body.blocker)
    return dict(row)


@app.patch("/api/v1/sprint/{item_id}", response_model=SprintItemOut)
async def update_sprint_item(
    item_id: str,
    body: SprintItemUpdate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Update a sprint item's status, blocker, or priority."""
    # Build dynamic update
    sets = ["updated_at = now()"]
    params = []
    idx = 1
    
    if body.status is not None:
        sets.append(f"status = ${idx}")
        params.append(body.status)
        idx += 1
        if body.status == "done":
            sets.append(f"completed_at = now()")
    
    if body.blocker is not None:
        sets.append(f"blocker = ${idx}")
        params.append(body.blocker)
        idx += 1
    
    if body.priority is not None:
        sets.append(f"priority = ${idx}")
        params.append(body.priority)
        idx += 1
    
    params.append(item_id)
    query = f"UPDATE sprint_items SET {', '.join(sets)} WHERE id = ${idx}::uuid RETURNING *"
    
    row = await pool.fetchrow(query, *params)
    if not row:
        raise HTTPException(404, "Sprint item not found")
    return dict(row)


# =============================================================================
# SESSION HISTORY ENDPOINTS
# =============================================================================

@app.get("/api/v1/sessions", response_model=list[SessionOut])
async def list_sessions(
    limit: int = Query(default=10, le=50),
    pool: asyncpg.Pool = Depends(get_db),
):
    """List recent sessions, newest first."""
    rows = await pool.fetch(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT $1", limit
    )
    return [dict(r) for r in rows]


@app.post("/api/v1/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    body: SessionCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """
    Record a completed work session.
    Call this at the END of every Claude conversation.
    The 'next_actions' and 'context_for_next' fields are what make handoffs work.
    """
    row = await pool.fetchrow("""
        INSERT INTO sessions (
            title, summary, key_outputs, decisions_made, problems_hit,
            next_actions, context_for_next, tools_used, duration_mins, ended_at
        ) VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6::jsonb, $7, $8, $9, now())
        RETURNING *
    """,
        body.title, body.summary, json.dumps(body.key_outputs),
        body.decisions_made, json.dumps(body.problems_hit),
        json.dumps(body.next_actions), body.context_for_next,
        body.tools_used, body.duration_mins
    )
    return dict(row)


# =============================================================================
# CODE STATE ENDPOINTS
# =============================================================================

@app.get("/api/v1/code", response_model=list[CodeStateOut])
async def list_code_state(
    status: str = Query(default="active"),
    pool: asyncpg.Pool = Depends(get_db),
):
    """List tracked code files."""
    rows = await pool.fetch(
        "SELECT * FROM code_state WHERE status = $1 ORDER BY file_path", status
    )
    return [dict(r) for r in rows]


@app.post("/api/v1/code", response_model=CodeStateOut, status_code=201)
async def upsert_code_state(
    body: CodeStateCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Track a code file. Upserts on file_path."""
    row = await pool.fetchrow("""
        INSERT INTO code_state (file_path, purpose, status, notes)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (file_path) DO UPDATE SET
            purpose = $2, status = $3, notes = $4, last_modified = now()
        RETURNING *
    """, body.file_path, body.purpose, body.status, body.notes)
    return dict(row)


# =============================================================================
# OPEN QUESTIONS ENDPOINTS
# =============================================================================

@app.get("/api/v1/questions", response_model=list[OpenQuestionOut])
async def list_questions(
    status: str = Query(default="open"),
    pool: asyncpg.Pool = Depends(get_db),
):
    """List open questions."""
    rows = await pool.fetch(
        "SELECT * FROM open_questions WHERE status = $1 ORDER BY created_at", status
    )
    return [dict(r) for r in rows]


@app.post("/api/v1/questions", response_model=OpenQuestionOut, status_code=201)
async def create_question(
    body: OpenQuestionCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Record an open question."""
    row = await pool.fetchrow("""
        INSERT INTO open_questions (question, context, category)
        VALUES ($1, $2, $3)
        RETURNING *
    """, body.question, body.context, body.category)
    return dict(row)


@app.patch("/api/v1/questions/{question_id}/resolve", response_model=OpenQuestionOut)
async def resolve_question(
    question_id: str,
    body: OpenQuestionResolve,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Mark an open question as answered."""
    row = await pool.fetchrow("""
        UPDATE open_questions SET status = 'answered', answer = $2, resolved_at = now()
        WHERE id = $1::uuid
        RETURNING *
    """, question_id, body.answer)
    if not row:
        raise HTTPException(404, "Question not found")
    return dict(row)


# =============================================================================
# KNOWLEDGE LAYER — Document Graph (DocAgent integrated into CHIRAN Phase 0)
# =============================================================================

# --- Pydantic Models ---
class DocNodeCreate(BaseModel):
    doc_id: str = Field(..., examples=["ARCH-008"])
    type: str = Field(default="document", examples=["document", "section", "schema", "agent", "technology"])
    name: str = Field(..., examples=["Agent Platform & Division Architecture v1.0"])
    content: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    status: str = Field(default="current")

class DocNodeOut(DocNodeCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int
    updated_by: str
    created_at: datetime
    updated_at: datetime

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)

    @field_validator('content', 'metadata', mode='before')
    @classmethod
    def _fix_json_dicts(cls, v):
        return _to_dict(v)

class DocEdgeCreate(BaseModel):
    from_node_id: str
    to_node_id: str
    relation: str = Field(..., examples=["depends_on", "supersedes", "contains", "references"])

class DocStalenessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    doc_id: str
    issue: str
    severity: str
    status: str
    detected_at: datetime

    @field_validator('id', mode='before')
    @classmethod
    def _fix_id(cls, v):
        return _to_str(v)

class DocSearchResult(BaseModel):
    doc_id: str
    name: str
    type: str
    content: dict
    metadata: dict
    status: str


# --- Document Node Endpoints ---

@app.get("/api/v1/docs", response_model=list[DocNodeOut])
async def list_docs(
    type: str | None = None,
    status: str = Query(default="current"),
    doc_id: str | None = None,
    pool: asyncpg.Pool = Depends(get_db),
):
    """List document nodes. Filter by type, status, or doc_id."""
    query = "SELECT * FROM doc_nodes WHERE status = $1"
    params: list = [status]
    idx = 2
    if type:
        query += f" AND type = ${idx}"
        params.append(type)
        idx += 1
    if doc_id:
        query += f" AND doc_id = ${idx}"
        params.append(doc_id)
        idx += 1
    query += " ORDER BY doc_id, name"
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/api/v1/docs/manifest")
async def get_doc_manifest(pool: asyncpg.Pool = Depends(get_db)):
    """
    Returns the live document manifest — the single source of truth
    for what documents exist, their versions, and status.
    This replaces manually maintaining doc_manifest.json.
    """
    rows = await pool.fetch("""
        SELECT doc_id, name, type, status, version,
               metadata->>'format' as format,
               metadata->>'pages' as pages,
               metadata->>'audience' as audience,
               metadata->>'description' as description,
               metadata->>'filename' as filename,
               updated_at
        FROM doc_nodes
        WHERE type = 'document'
        ORDER BY doc_id
    """)
    return {
        "manifest_version": "live",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_documents": len(rows),
        "documents": [dict(r) for r in rows],
    }


@app.post("/api/v1/docs", response_model=DocNodeOut, status_code=201)
async def create_doc_node(
    body: DocNodeCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Create a document node in the knowledge graph."""
    row = await pool.fetchrow("""
        INSERT INTO doc_nodes (doc_id, type, name, content, metadata, status)
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
        RETURNING *
    """, body.doc_id, body.type, body.name, json.dumps(body.content),
        json.dumps(body.metadata), body.status)
    return dict(row)


@app.get("/api/v1/docs/search")
async def search_docs(
    q: str = Query(..., description="Search term — searches doc names and content"),
    pool: asyncpg.Pool = Depends(get_db),
):
    """
    Search the document graph by keyword.
    Claude uses this to find relevant documentation before answering questions.
    Example: 'Comprehension Guard' returns the agent definition, its section in ARCH-001, related schemas.
    """
    rows = await pool.fetch("""
        SELECT doc_id, name, type, content, metadata, status
        FROM doc_nodes
        WHERE status = 'current'
          AND (
            name ILIKE '%' || $1 || '%'
            OR doc_id ILIKE '%' || $1 || '%'
            OR content::text ILIKE '%' || $1 || '%'
          )
        ORDER BY
            CASE WHEN name ILIKE '%' || $1 || '%' THEN 0 ELSE 1 END,
            doc_id
        LIMIT 20
    """, q)
    return {
        "query": q,
        "results": [dict(r) for r in rows],
        "count": len(rows),
    }


# --- Document Edges ---

@app.post("/api/v1/docs/edges", status_code=201)
async def create_doc_edge(
    body: DocEdgeCreate,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Create a relationship between two document nodes."""
    await pool.execute("""
        INSERT INTO doc_edges (from_node, to_node, relation)
        VALUES ($1::uuid, $2::uuid, $3)
        ON CONFLICT DO NOTHING
    """, body.from_node_id, body.to_node_id, body.relation)
    return {"status": "created", "relation": body.relation}


@app.get("/api/v1/docs/{doc_id}/related")
async def get_related_docs(
    doc_id: str,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Get all documents related to a given doc_id (via edges)."""
    rows = await pool.fetch("""
        SELECT DISTINCT dn2.doc_id, dn2.name, dn2.type, de.relation
        FROM doc_nodes dn1
        JOIN doc_edges de ON dn1.id = de.from_node
        JOIN doc_nodes dn2 ON de.to_node = dn2.id
        WHERE dn1.doc_id = $1
        UNION
        SELECT DISTINCT dn2.doc_id, dn2.name, dn2.type, de.relation
        FROM doc_nodes dn1
        JOIN doc_edges de ON dn1.id = de.to_node
        JOIN doc_nodes dn2 ON de.from_node = dn2.id
        WHERE dn1.doc_id = $1
    """, doc_id)
    return {"doc_id": doc_id, "related": [dict(r) for r in rows]}


# --- Staleness Detection ---

@app.get("/api/v1/docs/staleness", response_model=list[DocStalenessOut])
async def list_staleness(
    status: str = Query(default="open"),
    pool: asyncpg.Pool = Depends(get_db),
):
    """List staleness issues. Default: open issues only."""
    rows = await pool.fetch(
        "SELECT * FROM doc_staleness WHERE status = $1 ORDER BY severity DESC, detected_at",
        status,
    )
    return [dict(r) for r in rows]


@app.post("/api/v1/docs/staleness/check")
async def run_staleness_check(pool: asyncpg.Pool = Depends(get_db)):
    """
    Run staleness detection across all documents.
    Compares doc_nodes content against actual system state from Memory Layer.
    Call this periodically or after significant changes.
    """
    issues_found = []

    # Check 1: Agent count consistency
    agent_count_node = await pool.fetchrow("""
        SELECT id, doc_id, content FROM doc_nodes
        WHERE type = 'section' AND name ILIKE '%agent count%' AND status = 'current'
        LIMIT 1
    """)
    actual_deployments = await pool.fetchval("SELECT count(*) FROM deployment_state")
    actual_decisions = await pool.fetchval("SELECT count(*) FROM decisions WHERE status = 'active'")

    # Check 2: Deployment state vs documentation
    deployed = await pool.fetch(
        "SELECT component, status FROM deployment_state WHERE status = 'deployed'"
    )
    doc_deployments = await pool.fetch(
        "SELECT doc_id, name, content FROM doc_nodes WHERE type = 'section' AND name ILIKE '%deploy%'"
    )

    # Check 3: Documents with no updates in 30+ days despite system changes
    stale_docs = await pool.fetch("""
        SELECT doc_id, name, updated_at FROM doc_nodes
        WHERE type = 'document' AND status = 'current'
          AND updated_at < now() - interval '30 days'
    """)
    for doc in stale_docs:
        issue = f"Document {doc['doc_id']} ({doc['name']}) not updated in 30+ days"
        await pool.execute("""
            INSERT INTO doc_staleness (doc_id, node_id, issue, severity)
            VALUES ($1, NULL, $2, 'warning')
        """, doc["doc_id"], issue)
        issues_found.append(issue)

    # Check 4: Open questions that reference docs
    open_q = await pool.fetchval("SELECT count(*) FROM open_questions WHERE status = 'open'")

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "issues_found": len(issues_found),
        "issues": issues_found,
        "stats": {
            "total_documents": await pool.fetchval("SELECT count(*) FROM doc_nodes WHERE type = 'document'"),
            "total_sections": await pool.fetchval("SELECT count(*) FROM doc_nodes WHERE type = 'section'"),
            "open_staleness_issues": await pool.fetchval("SELECT count(*) FROM doc_staleness WHERE status = 'open'"),
            "deployed_components": len(deployed),
            "active_decisions": actual_decisions,
            "open_questions": open_q,
        },
    }


@app.patch("/api/v1/docs/staleness/{issue_id}/resolve")
async def resolve_staleness(
    issue_id: str,
    pool: asyncpg.Pool = Depends(get_db),
):
    """Mark a staleness issue as resolved."""
    row = await pool.fetchrow("""
        UPDATE doc_staleness SET status = 'resolved', resolved_at = now()
        WHERE id = $1::uuid
        RETURNING id, doc_id, issue, status
    """, issue_id)
    if not row:
        raise HTTPException(404, "Staleness issue not found")
    return dict(row)


# --- Cost Events (Phase 0 cost monitoring) ---

@app.post("/api/v1/costs", status_code=201)
async def log_cost_event(
    agent_id: str = Query(default="manual"),
    division: str = Query(default="general"),
    provider: str = Query(..., description="anthropic | twilio | railway | elevenlabs | etc"),
    operation: str = Query(default="api_call"),
    cost_usd: float = Query(...),
    pool: asyncpg.Pool = Depends(get_db),
):
    """Log a cost event. Every API call and compute cost should be tracked."""
    await pool.execute("""
        INSERT INTO cost_events (agent_id, division, provider, operation, cost_usd)
        VALUES ($1, $2, $3, $4, $5)
    """, agent_id, division, provider, operation, cost_usd)
    return {"status": "logged"}


@app.get("/api/v1/costs/summary")
async def cost_summary(
    days: int = Query(default=7, description="Look back N days"),
    pool: asyncpg.Pool = Depends(get_db),
):
    """Cost summary for CHIRAN briefing. Aggregates by provider and division."""
    by_provider = await pool.fetch("""
        SELECT provider, SUM(cost_usd) as total, COUNT(*) as events
        FROM cost_events WHERE created_at > now() - make_interval(days => $1)
        GROUP BY provider ORDER BY total DESC
    """, days)
    by_division = await pool.fetch("""
        SELECT division, SUM(cost_usd) as total, COUNT(*) as events
        FROM cost_events WHERE created_at > now() - make_interval(days => $1)
        GROUP BY division ORDER BY total DESC
    """, days)
    total = await pool.fetchval("""
        SELECT COALESCE(SUM(cost_usd), 0) FROM cost_events
        WHERE created_at > now() - make_interval(days => $1)
    """, days)
    return {
        "period_days": days,
        "total_usd": float(total),
        "by_provider": [dict(r) for r in by_provider],
        "by_division": [dict(r) for r in by_division],
    }


# =============================================================================
# ENHANCED HANDOFF: Include doc health and cost summary
# =============================================================================
# (The generate_handoff endpoint already exists above — we add doc health
#  to the _build_handoff_document function by updating the GET /handoff endpoint
#  to also query doc staleness and costs)


# =============================================================================
# SEED: Populate with current Fono state
# =============================================================================

@app.post("/api/v1/seed")
async def seed_current_state(pool: asyncpg.Pool = Depends(get_db)):
    """
    One-time seed with Fono's current state as of March 2026.
    Idempotent — safe to call multiple times.
    """
    
    # --- Deployments ---
    deployments = [
        ("data-collection-pipeline", "deployed", "production", "phase0-step2",
         {"phone": "+18557793783", "storage": "cloudflare-r2", "notifications": "whatsapp",
          "voice": "elevenlabs-zara-prerecorded", "secrets": "doppler"}),
        ("api-service", "deployed", "production", "phase0-step2",
         {"features": "dashboard-apis, sse-events, jwt-middleware, event-bus",
          "host": "railway", "db": "postgresql"}),
        ("spice-garden-tenant", "deployed", "production", "phase0",
         {"tenant_uuid": "ef29", "status": "configured"}),
        ("owner-dashboard-frontend", "planned", "production", None,
         {"scope": "call-management, callbacks, status-tracking, analytics"}),
        ("cash-register-kiosk", "planned", "production", None,
         {"scope": "order-display, kitchen-view"}),
        ("restaurant-signup-flow", "planned", "production", None,
         {"scope": "self-service-onboarding"}),
        ("voice-pipeline-phase1", "planned", "production", None,
         {"scope": "twilio-websocket, stt, tts, conversation-agent, comprehension-guard"}),
    ]
    
    for comp, status, env, ver, details in deployments:
        await pool.execute("""
            INSERT INTO deployment_state (component, status, environment, version, details)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (component, environment) DO UPDATE SET
                status = $2, version = $4, details = $5::jsonb, updated_at = now()
        """, comp, status, env, ver, json.dumps(details))
    
    # --- Decisions ---
    decisions = [
        ("FastAPI over OpenClaw", "technical",
         "Use FastAPI for all backend services",
         "Better latency control for realtime voice pipeline. Python ecosystem is lingua franca for AI/ML. Native async, Pydantic validation.",
         [{"name": "OpenClaw", "rejected_because": "Personal assistant, not production telephony. No multi-tenant. No Tenglish support."},
          {"name": "Node.js + Fastify", "rejected_because": "Python ecosystem better for AI/ML tooling"}]),
        ("Railway over AWS", "infrastructure",
         "Deploy on Railway for current phase",
         "Simplicity at current scale. Team size = 1. Can migrate to AWS ECS/GCP Cloud Run at scale.",
         [{"name": "AWS ECS Fargate", "rejected_because": "Overkill for solo developer phase"},
          {"name": "GCP Cloud Run", "rejected_because": "Same — revisit at Phase 3"}]),
        ("Doppler over AWS Secrets Manager", "infrastructure",
         "Use Doppler for secrets management",
         "Better developer experience for solo developer. Easy Railway integration.",
         [{"name": "AWS Secrets Manager", "rejected_because": "Revisit when moving to AWS"}]),
        ("ElevenLabs pre-recorded over Twilio TTS", "technical",
         "Use pre-recorded ElevenLabs Zara voice clips instead of Twilio's robotic TTS",
         "Voice quality dramatically better. Pre-recorded clips for Phase 0 consent flows.",
         [{"name": "Twilio TTS", "rejected_because": "Robotic sounding, poor experience"}]),
        ("WhatsApp over email for restaurant comms", "product",
         "Use WhatsApp as primary notification channel for restaurant owners",
         "Higher engagement rate in target Telugu restaurant demographic. Owners check WhatsApp constantly.",
         [{"name": "Email", "rejected_because": "Lower open rate for target demographic"},
          {"name": "SMS only", "rejected_because": "Less rich, no images/formatting"}]),
        ("STT provider decision DEFERRED", "provider",
         "Do NOT commit to any STT provider until real Phase 0 recordings are benchmarked",
         "Synthetic data cannot capture authentic Tenglish ordering patterns. All provider claims unproven for code-mixing.",
         [{"name": "Sarvam AI", "rejected_because": "NOT rejected — needs benchmarking with real data"},
          {"name": "Deepgram", "rejected_because": "NOT rejected — needs benchmarking with real data"}]),
        ("OpenAI Realtime API rejected for voice pipeline", "technical",
         "Do not use OpenAI Realtime API for the voice ordering pipeline",
         "No Tenglish support. Black box model removes control over Comprehension Guard / phonetic matching. Cost too high (~$0.30-0.50/call vs target <$0.10).",
         [{"name": "OpenAI Realtime API", "rejected_because": "No Tenglish, no control, cost 3-5x target"}]),
    ]
    
    for title, cat, dec, reason, alts in decisions:
        await pool.execute("""
            INSERT INTO decisions (title, category, decision, reasoning, alternatives)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT DO NOTHING
        """, title, cat, dec, reason, json.dumps(alts))
    
    # --- Open Questions ---
    questions = [
        ("Which STT provider for Tenglish?",
         "Must benchmark Sarvam vs Deepgram vs others against real Phase 0 recordings. Code-mixing performance unproven.",
         "provider"),
        ("Frontend framework for owner dashboard?",
         "Need to decide React/Next.js vs simpler approach. Dashboard is the next build priority.",
         "technical"),
        ("When to start Phase 1 voice pipeline build?",
         "Blocked on: enough Phase 0 recordings for STT benchmarking. Need ~50+ real calls.",
         "planning"),
    ]
    
    for q, ctx, cat in questions:
        await pool.execute("""
            INSERT INTO open_questions (question, context, category)
            VALUES ($1, $2, $3)
        """, q, ctx, cat)
    
    # --- Sprint Items ---
    sprint_items = [
        ("Build owner dashboard frontend", "Interactive dashboard for call management, callbacks, analytics", 1, "todo", None),
        ("Build cash register kiosk views", "Kitchen/counter display for incoming orders", 2, "todo", None),
        ("Build restaurant signup flow", "Self-service onboarding for new restaurants", 2, "todo", None),
        ("Collect more Phase 0 recordings", "Need 50+ real Tenglish restaurant calls for STT benchmarking", 1, "in_progress", None),
        ("Benchmark STT providers", "Test Sarvam vs Deepgram against real recordings", 1, "blocked", "Not enough Phase 0 recordings yet"),
    ]
    
    for title, desc, prio, status, blocker in sprint_items:
        await pool.execute("""
            INSERT INTO sprint_items (title, description, priority, status, blocker)
            VALUES ($1, $2, $3, $4, $5)
        """, title, desc, prio, status, blocker)
    
    # --- Knowledge Layer: Document Graph ---
    docs = [
        ("ARCH-001", "document", "AI Voice Assistant - Complete Architecture v3.0",
         {"agent_count": 16, "track": "A", "key_sections": ["Executive Summary", "System Architecture", "Dual-Provider Speech", "15-Agent Table", "Comprehension Guard", "Supervisor Agent", "Order Delivery", "Restaurant Interface", "Companion App"]},
         {"format": "PDF", "pages": "10", "version": "3.0", "audience": "cto, developer, investor", "filename": "AI_Voice_Assistant_Architecture_v3.pdf", "description": "Condensed architecture blueprint. 16 product agents, dual-provider speech, California Telugu launch."}),
        ("ARCH-002", "document", "AI Voice Assistant - Full Detailed Architecture v3.0",
         {"sections_count": 26, "track": "A"},
         {"format": "DOCX", "pages": "87", "version": "3.0", "audience": "cto, developer", "filename": "AI_Voice_Assistant_Complete_Architecture_v3_0.docx", "description": "Complete 26-section architecture with database schemas, API specs, folder structures, conversation state models."}),
        ("ARCH-003", "document", "Documentation Intelligence Agent - Addendum v3.1",
         {"agent": "DocAgent", "sub_agents": ["Schema Doc", "API Doc", "Prompt Doc", "Compliance Doc", "Onboarding Doc"]},
         {"format": "PDF", "pages": "11", "version": "3.1", "audience": "cto, developer", "filename": "DocAgent_Architecture_Addendum_v3.1.pdf", "description": "16th agent architecture. Document Graph model, access control, agent-facing REST API, evolution roadmap."}),
        ("ARCH-004", "document", "Architecture Supplement v2.2 - Supervisor, Order Delivery, Restaurant Portal",
         {"agents": ["Supervisor", "Order Delivery", "Upsell", "Post-Call"]},
         {"format": "DOCX", "pages": "20", "version": "2.2", "audience": "cto, developer", "filename": "Architecture_Supplement_v2.2_Supervisor_OrderDelivery_RestaurantPortal.docx", "description": "Deep-dive into Supervisor Agent, Order Delivery Pipeline, Restaurant Interface. Superseded by v3.0 but retained as reference.", "status_note": "superseded_by_v3"}),
        ("ARCH-006", "document", "IAD Governance Layer Architecture v1.0",
         {"agents": ["Authorization Agent", "Trust & Reputation Agent", "Audit & Forensics Agent", "Contract Enforcer Agent"], "iad_count": 4, "framework": "Google DeepMind arXiv 2602.11865"},
         {"format": "DOCX", "pages": "28", "version": "1.0", "audience": "cto, developer", "filename": "VoiceOrder_IAD_Governance_Layer_ARCH006_v1.0.docx", "description": "Full IAD governance layer. 4 agents: Authorization (DCTs), Trust, Audit, Contract Enforcer."}),
        ("ARCH-007", "document", "SDLC Organizational Agent Architecture v1.0",
         {"departments": {"engineering": 7, "marketing": 5, "social_media": 5}, "track_b_total": 17},
         {"format": "DOCX", "pages": "22", "version": "1.0", "audience": "cto, developer", "filename": "VoiceOrder_SDLC_OrgAgents_v1.0.docx", "description": "Track B SDLC org agents. 17 agents across Engineering, Marketing, Social Media departments."}),
        ("ARCH-008", "document", "Agent Platform & Division Architecture v1.0",
         {"divisions": ["Product", "Software", "Marketing & Growth"], "planned_divisions": ["Sales", "Operations", "Finance", "Data", "Security"], "chiran_role": "COO", "claude_role": "tool"},
         {"format": "DOCX", "pages": "~40", "version": "1.0", "audience": "cto, developer", "filename": "ARCH-008_Agent_Platform_Division_Architecture_v1.0.docx", "description": "Master architecture. Division model, CHIRAN as COO, Product + Software Division deep dives, inter-division protocols, agent platform runtime, scaling path 38 to 500+ agents."}),
        ("DEV-001", "document", "Claude Code Kickoff Brief - Phase 1 MVP",
         {"prompts": 12, "weeks": 4, "delivers": "Track A voice pipeline end-to-end"},
         {"format": "PDF", "pages": "12", "version": "1.0", "audience": "developer, claude_code", "filename": "Claude_Code_Kickoff_Brief_Phase1_MVP.pdf", "description": "12 ready-to-paste prompts for Claude Code covering 4-week MVP build of Track A voice pipeline."}),
        ("DEV-002", "document", "Claude Code Kickoff Brief - IAD Governance Layer Phase 1",
         {"prompts": 5, "delivers": "Immutable audit chain across all agents"},
         {"format": "PDF", "pages": "10", "version": "1.0", "audience": "developer, claude_code", "filename": "VoiceOrder_IAD_ClaudeCode_Brief_DEV002_v1.0.pdf", "description": "5 paste-ready prompts for Claude Code to build IAD Audit Agent."}),
        ("DATA-001", "document", "Data Collection Pipeline v3.2",
         {"pipeline": "live", "phone": "+18557793783"},
         {"format": "PDF", "pages": "?", "version": "3.2", "audience": "developer", "filename": "Data_Collection_Pipeline_v3_2.pdf", "description": "Data collection pipeline design and implementation guide."}),
    ]
    
    doc_node_ids = {}
    for doc_id, dtype, name, content, metadata in docs:
        row = await pool.fetchrow("""
            INSERT INTO doc_nodes (doc_id, type, name, content, metadata, status)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
            ON CONFLICT DO NOTHING
            RETURNING id
        """, doc_id, dtype, name, json.dumps(content), json.dumps(metadata),
            "current" if "status_note" not in metadata else "superseded")
        if row:
            doc_node_ids[doc_id] = str(row["id"])
    
    # --- Document Edges (relationships) ---
    edges = [
        ("ARCH-008", "ARCH-001", "depends_on"),
        ("ARCH-008", "ARCH-003", "depends_on"),
        ("ARCH-008", "ARCH-006", "depends_on"),
        ("ARCH-008", "ARCH-007", "depends_on"),
        ("ARCH-001", "ARCH-004", "supersedes"),
        ("ARCH-006", "ARCH-001", "depends_on"),
        ("ARCH-006", "ARCH-007", "depends_on"),
        ("DEV-001", "ARCH-001", "references"),
        ("DEV-002", "ARCH-006", "references"),
    ]
    
    for from_doc, to_doc, relation in edges:
        if from_doc in doc_node_ids and to_doc in doc_node_ids:
            await pool.execute("""
                INSERT INTO doc_edges (from_node, to_node, relation)
                VALUES ($1::uuid, $2::uuid, $3)
                ON CONFLICT DO NOTHING
            """, doc_node_ids[from_doc], doc_node_ids[to_doc], relation)
    
    return {
        "status": "seeded",
        "memory_layer": {
            "deployments": len(deployments),
            "decisions": len(decisions),
            "questions": len(questions),
            "sprint_items": len(sprint_items),
        },
        "knowledge_layer": {
            "documents": len(docs),
            "edges": len([e for e in edges if e[0] in doc_node_ids and e[1] in doc_node_ids]),
        },
    }


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
