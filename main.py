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
-- CHIRAN Context Service Schema — Multi-Project
-- Supports multiple products: Fono, SalesPilot, future projects
-- ============================================================

-- 0. PROJECTS: Registry of all products CHIRAN manages
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,            -- 'fono', 'salespilot'
    name            TEXT NOT NULL,               -- 'Fono', 'SalesPilot'
    description     TEXT,
    status          TEXT DEFAULT 'active',       -- active | paused | archived
    metadata        JSONB DEFAULT '{}',          -- tech stack, repo URLs, etc.
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Seed default projects if not exist
INSERT INTO projects (id, name, description, metadata) VALUES
    ('fono', 'Fono', 'AI Voice Assistant for South Indian Telugu restaurants in California',
     '{"repo": "manonag/fono-backend", "frontend": "manonag/fono-frontend", "stack": "FastAPI + PostgreSQL + Twilio + Railway"}'::jsonb),
    ('salespilot', 'SalesPilot', 'Field sales route optimization with ML-predicted deal priority scoring',
     '{"repo": "planned", "stack": "FastAPI + PostgreSQL + XGBoost + OR-Tools", "context": "Kanaka SJSU DL project"}'::jsonb),
    ('platform', 'Platform', 'Shared infrastructure across all products — CHIRAN, IAD, agent architecture',
     '{"scope": "chiran, iad-governance, agent-platform, shared-sdlc"}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- 1. DEPLOYMENT STATE: What's actually running in production
CREATE TABLE IF NOT EXISTS deployment_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    component       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planned',
    environment     TEXT NOT NULL DEFAULT 'production',
    version         TEXT,
    details         JSONB NOT NULL DEFAULT '{}',
    deployed_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(project, component, environment)
);

-- 2. DECISIONS: What was decided and WHY
CREATE TABLE IF NOT EXISTS decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    title           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'technical',
    decision        TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    alternatives    JSONB DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'active',
    session_id      UUID,
    decided_at      TIMESTAMPTZ DEFAULT now(),
    revisit_after   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_decisions_category ON decisions(category);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project);

-- 3. SPRINT STATE: Current priorities and blockers
CREATE TABLE IF NOT EXISTS sprint_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    title           TEXT NOT NULL,
    description     TEXT,
    priority        INTEGER NOT NULL DEFAULT 3,
    status          TEXT NOT NULL DEFAULT 'todo',
    blocker         TEXT,
    depends_on      UUID[],
    session_id      UUID,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sprint_status ON sprint_items(status);
CREATE INDEX IF NOT EXISTS idx_sprint_priority ON sprint_items(priority);
CREATE INDEX IF NOT EXISTS idx_sprint_project ON sprint_items(project);

-- 4. SESSION HISTORY: Compressed summaries of every Claude work session
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    session_number  SERIAL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    key_outputs     JSONB DEFAULT '[]',
    decisions_made  UUID[],
    problems_hit    JSONB DEFAULT '[]',
    next_actions    JSONB DEFAULT '[]',
    context_for_next TEXT,
    tools_used      TEXT[],
    duration_mins   INTEGER,
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

-- 5. CODE STATE: Track what files/modules exist and their purpose
CREATE TABLE IF NOT EXISTS code_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    file_path       TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    last_modified   TIMESTAMPTZ DEFAULT now(),
    session_id      UUID,
    notes           TEXT,
    UNIQUE(project, file_path)
);

CREATE INDEX IF NOT EXISTS idx_code_state_status ON code_state(status);
CREATE INDEX IF NOT EXISTS idx_code_state_project ON code_state(project);

-- 6. OPEN QUESTIONS: Things that need to be resolved
CREATE TABLE IF NOT EXISTS open_questions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    question        TEXT NOT NULL,
    context         TEXT,
    category        TEXT DEFAULT 'technical',
    status          TEXT NOT NULL DEFAULT 'open',
    answer          TEXT,
    session_id      UUID,
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_questions_status ON open_questions(status);
CREATE INDEX IF NOT EXISTS idx_questions_project ON open_questions(project);

-- ============================================================
-- KNOWLEDGE LAYER (DocAgent integrated into CHIRAN Phase 0)
-- ============================================================

-- 7. DOCUMENT NODES
CREATE TABLE IF NOT EXISTS doc_nodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    doc_id          TEXT NOT NULL,
    type            TEXT NOT NULL,
    name            TEXT NOT NULL,
    content         JSONB NOT NULL DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    version         INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'current',
    updated_by      TEXT DEFAULT 'manual',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_nodes_doc_id ON doc_nodes(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_nodes_type ON doc_nodes(type);
CREATE INDEX IF NOT EXISTS idx_doc_nodes_status ON doc_nodes(status);
CREATE INDEX IF NOT EXISTS idx_doc_nodes_project ON doc_nodes(project);

-- 8. DOCUMENT EDGES
CREATE TABLE IF NOT EXISTS doc_edges (
    from_node       UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    to_node         UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',
    PRIMARY KEY (from_node, to_node, relation)
);

-- 9. DOCUMENT VERSIONS
CREATE TABLE IF NOT EXISTS doc_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id         UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    content         JSONB NOT NULL,
    diff_summary    TEXT,
    changed_by      TEXT,
    changed_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_versions_node ON doc_versions(node_id);

-- 10. STALENESS TRACKING
CREATE TABLE IF NOT EXISTS doc_staleness (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    doc_id          TEXT NOT NULL,
    node_id         UUID REFERENCES doc_nodes(id) ON DELETE CASCADE,
    issue           TEXT NOT NULL,
    severity        TEXT DEFAULT 'warning',
    detected_by     TEXT DEFAULT 'staleness_checker',
    status          TEXT DEFAULT 'open',
    detected_at     TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_staleness_status ON doc_staleness(status);

-- 11. COST EVENTS
CREATE TABLE IF NOT EXISTS cost_events (
    id              BIGSERIAL PRIMARY KEY,
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    agent_id        TEXT,
    division        TEXT,
    provider        TEXT NOT NULL,
    operation       TEXT,
    cost_usd        NUMERIC(10,6) NOT NULL,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_events_date ON cost_events(created_at);
CREATE INDEX IF NOT EXISTS idx_cost_events_division ON cost_events(division);
CREATE INDEX IF NOT EXISTS idx_cost_events_project ON cost_events(project);

-- 12. TASK QUEUE: Agent-executable tasks with briefs
CREATE TABLE IF NOT EXISTS tasks (
    id              SERIAL PRIMARY KEY,
    project         TEXT NOT NULL DEFAULT 'fono' REFERENCES projects(id),
    title           TEXT NOT NULL,
    brief           TEXT,
    priority        INTEGER NOT NULL DEFAULT 3,
    status          TEXT NOT NULL DEFAULT 'draft',
    assigned_to     TEXT DEFAULT 'claude_code',
    depends_on      INTEGER[],
    blocked_reason  TEXT,
    result          TEXT,
    error           TEXT,
    created_by      TEXT DEFAULT 'claude_chat',
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
"""

# Migration SQL — adds project column to existing tables if not present
MIGRATION_SQL = """
-- Add project column to existing tables (idempotent)
DO $$
BEGIN
    -- Ensure projects table exists first (for FK references)
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'projects') THEN
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            status TEXT DEFAULT 'active', metadata JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT now()
        );
    END IF;
    
    -- Seed default projects
    INSERT INTO projects (id, name, description) VALUES
        ('fono', 'Fono', 'AI Voice Assistant for Telugu restaurants'),
        ('salespilot', 'SalesPilot', 'Field sales route optimization'),
        ('platform', 'Platform', 'Shared infrastructure — CHIRAN, IAD, agent architecture')
    ON CONFLICT (id) DO NOTHING;

    -- Add project column to each table if missing, default to 'fono'
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='deployment_state' AND column_name='project') THEN
        ALTER TABLE deployment_state ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
        -- Drop old unique constraint and add new one with project
        ALTER TABLE deployment_state DROP CONSTRAINT IF EXISTS deployment_state_component_environment_key;
        ALTER TABLE deployment_state ADD CONSTRAINT deployment_state_project_component_env_key UNIQUE(project, component, environment);
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='decisions' AND column_name='project') THEN
        ALTER TABLE decisions ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sprint_items' AND column_name='project') THEN
        ALTER TABLE sprint_items ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sessions' AND column_name='project') THEN
        ALTER TABLE sessions ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='code_state' AND column_name='project') THEN
        ALTER TABLE code_state ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
        ALTER TABLE code_state DROP CONSTRAINT IF EXISTS code_state_file_path_key;
        ALTER TABLE code_state ADD CONSTRAINT code_state_project_file_path_key UNIQUE(project, file_path);
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='open_questions' AND column_name='project') THEN
        ALTER TABLE open_questions ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='doc_nodes' AND column_name='project') THEN
        ALTER TABLE doc_nodes ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='doc_staleness' AND column_name='project') THEN
        ALTER TABLE doc_staleness ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cost_events' AND column_name='project') THEN
        ALTER TABLE cost_events ADD COLUMN project TEXT NOT NULL DEFAULT 'fono';
    END IF;
END $$;
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
    """Startup: create pool + migrate + schema + MCP session manager. Shutdown: close pool."""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db_pool.acquire() as conn:
        # Run migration first (adds project column to existing tables)
        await conn.execute(MIGRATION_SQL)
        # Then run full schema (for fresh installs)
        await conn.execute(SCHEMA_SQL)
    print(f"🧠 CHIRAN Context Service v0.3 (multi-project) ready on port {PORT}")
    
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
# Tools query the database directly (no localhost HTTP self-calls)
# This avoids async deadlocks and is faster

try:
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.transport_security import TransportSecuritySettings
    
    mcp_server = MCPServer(
        "Chiran",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    
    async def _get_pool():
        """Get the database pool, raising a clear error if not ready."""
        if db_pool is None:
            raise RuntimeError("Database pool not initialized")
        return db_pool
    
    def _project_filter(project: str) -> str:
        """Return SQL WHERE clause for project filtering."""
        if project == "all":
            return ""
        return f"project = '{project}'"
    
    @mcp_server.tool()
    async def generate_handoff(project: str = "fono", max_sessions: int = 3, include_code: bool = False) -> str:
        """Load project context. CALL THIS AT THE START OF EVERY CONVERSATION.
        Set project='fono', 'salespilot', 'platform', or 'all' for cross-project view.
        Returns deployment state, decisions, sprint priorities, recent sessions, open questions, and doc health."""
        try:
            pool = await _get_pool()
            pf = f"AND project = $1" if project != "all" else ""
            pf_where = f"WHERE project = $1" if project != "all" else ""
            params = [project] if project != "all" else []
            
            # Adjust param indices based on whether project filter is used
            if project != "all":
                deployments = await pool.fetch(
                    f"SELECT component, status, environment, version, details, deployed_at "
                    f"FROM deployment_state WHERE project = $1 ORDER BY component", project)
                decisions = await pool.fetch(
                    f"SELECT title, category, decision, reasoning, alternatives, status "
                    f"FROM decisions WHERE status = 'active' AND project = $1 ORDER BY decided_at DESC", project)
                sprint = await pool.fetch(
                    f"SELECT title, description, priority, status, blocker "
                    f"FROM sprint_items WHERE status IN ('todo', 'in_progress', 'blocked') AND project = $1 "
                    f"ORDER BY priority, created_at", project)
                sessions = await pool.fetch(
                    f"SELECT session_number, title, summary, key_outputs, next_actions, "
                    f"context_for_next, problems_hit, started_at "
                    f"FROM sessions WHERE project = $1 ORDER BY started_at DESC LIMIT $2",
                    project, max_sessions)
                questions = await pool.fetch(
                    f"SELECT question, context, category "
                    f"FROM open_questions WHERE status = 'open' AND project = $1 ORDER BY created_at", project)
                code_map = []
                if include_code:
                    code_map = await pool.fetch(
                        f"SELECT file_path, purpose, status, notes "
                        f"FROM code_state WHERE status = 'active' AND project = $1 ORDER BY file_path", project)
                doc_staleness = await pool.fetch(
                    f"SELECT doc_id, issue, severity FROM doc_staleness WHERE status = 'open' AND project = $1 ORDER BY severity DESC LIMIT 10", project)
                doc_count = await pool.fetchval(
                    f"SELECT count(*) FROM doc_nodes WHERE type = 'document' AND status = 'current' AND project = $1", project)
                cost_total = await pool.fetchval(
                    f"SELECT COALESCE(SUM(cost_usd), 0) FROM cost_events WHERE created_at > now() - interval '7 days' AND project = $1", project)
                tasks = await pool.fetch(
                    "SELECT * FROM tasks WHERE project = $1 AND status NOT IN ('done', 'failed') ORDER BY priority, created_at", project)
                done_tasks = await pool.fetch(
                    "SELECT * FROM tasks WHERE project = $1 AND status IN ('done', 'failed') ORDER BY completed_at DESC LIMIT 5", project)
            else:
                deployments = await pool.fetch("SELECT component, status, environment, version, details, deployed_at FROM deployment_state ORDER BY project, component")
                decisions = await pool.fetch("SELECT title, category, decision, reasoning, alternatives, status FROM decisions WHERE status = 'active' ORDER BY decided_at DESC")
                sprint = await pool.fetch("SELECT title, description, priority, status, blocker FROM sprint_items WHERE status IN ('todo', 'in_progress', 'blocked') ORDER BY priority, created_at")
                sessions = await pool.fetch("SELECT session_number, title, summary, key_outputs, next_actions, context_for_next, problems_hit, started_at FROM sessions ORDER BY started_at DESC LIMIT $1", max_sessions)
                questions = await pool.fetch("SELECT question, context, category FROM open_questions WHERE status = 'open' ORDER BY created_at")
                code_map = []
                doc_staleness = await pool.fetch("SELECT doc_id, issue, severity FROM doc_staleness WHERE status = 'open' ORDER BY severity DESC LIMIT 10")
                doc_count = await pool.fetchval("SELECT count(*) FROM doc_nodes WHERE type = 'document' AND status = 'current'")
                cost_total = await pool.fetchval("SELECT COALESCE(SUM(cost_usd), 0) FROM cost_events WHERE created_at > now() - interval '7 days'")
                tasks = await pool.fetch("SELECT * FROM tasks WHERE status NOT IN ('done', 'failed') ORDER BY priority, created_at")
                done_tasks = await pool.fetch("SELECT * FROM tasks WHERE status IN ('done', 'failed') ORDER BY completed_at DESC LIMIT 5")

            handoff = _build_handoff_document(
                deployments, decisions, sprint, sessions, questions, code_map,
                doc_staleness, doc_count, float(cost_total), project_name=project.upper(),
                tasks=list(tasks) + list(done_tasks),
            )
            return handoff
        except Exception as e:
            return f"Error generating handoff: {e}"
    
    @mcp_server.tool()
    async def save_session(title: str, summary: str, project: str = "fono", next_actions: str = "[]", context_for_next: str = "", tools_used: str = "claude_chat", duration_mins: int = 60) -> str:
        """Save a session summary at the END of every conversation. Set project='fono' or 'salespilot'."""
        try:
            pool = await _get_pool()
            parsed_actions = json.loads(next_actions) if next_actions.startswith("[") else [{"action": next_actions}]
            parsed_tools = [t.strip() for t in tools_used.split(",")]
            row = await pool.fetchrow("""
                INSERT INTO sessions (
                    project, title, summary, key_outputs, decisions_made, problems_hit,
                    next_actions, context_for_next, tools_used, duration_mins, ended_at
                ) VALUES ($1, $2, $3, '[]'::jsonb, '{}', '[]'::jsonb, $4::jsonb, $5, $6, $7, now())
                RETURNING session_number
            """, project, title, summary, json.dumps(parsed_actions), context_for_next,
                parsed_tools, duration_mins)
            return f"[{project}] Session #{row['session_number']} saved: {title}"
        except Exception as e:
            return f"Error saving session: {e}"
    
    @mcp_server.tool()
    async def record_decision(title: str, decision: str, reasoning: str, project: str = "fono", category: str = "technical", alternatives: str = "[]") -> str:
        """Record a technical or strategic decision. Set project='fono', 'salespilot', or 'platform' for shared decisions."""
        try:
            pool = await _get_pool()
            parsed_alts = json.loads(alternatives) if alternatives.startswith("[") else []
            await pool.execute("""
                INSERT INTO decisions (project, title, category, decision, reasoning, alternatives, status)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'active')
            """, project, title, category, decision, reasoning, json.dumps(parsed_alts))
            return f"[{project}] Decision recorded: {title}"
        except Exception as e:
            return f"Error recording decision: {e}"
    
    @mcp_server.tool()
    async def list_decisions(project: str = "fono", status: str = "active") -> str:
        """List decisions for a project. Use project='all' for cross-project view."""
        try:
            pool = await _get_pool()
            if project == "all":
                rows = await pool.fetch(
                    "SELECT project, title, decision, reasoning FROM decisions WHERE status = $1 ORDER BY decided_at DESC", status)
            else:
                rows = await pool.fetch(
                    "SELECT project, title, decision, reasoning FROM decisions WHERE status = $1 AND project = $2 ORDER BY decided_at DESC", status, project)
            if not rows:
                return f"No {status} decisions for {project}."
            lines = []
            for d in rows:
                prefix = f"[{d['project']}] " if project == "all" else ""
                lines.append(f"- {prefix}{d['title']}: {d['decision']} (Why: {d['reasoning']})")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing decisions: {e}"
    
    @mcp_server.tool()
    async def list_sprint(project: str = "fono", status: str = "") -> str:
        """List sprint items for a project. Use project='all' for cross-project view."""
        try:
            pool = await _get_pool()
            if project == "all":
                if status:
                    rows = await pool.fetch("SELECT id, project, title, priority, status, blocker FROM sprint_items WHERE status = $1 ORDER BY priority, created_at", status)
                else:
                    rows = await pool.fetch("SELECT id, project, title, priority, status, blocker FROM sprint_items WHERE status IN ('todo','in_progress','blocked') ORDER BY priority, created_at")
            else:
                if status:
                    rows = await pool.fetch("SELECT id, project, title, priority, status, blocker FROM sprint_items WHERE status = $1 AND project = $2 ORDER BY priority, created_at", status, project)
                else:
                    rows = await pool.fetch("SELECT id, project, title, priority, status, blocker FROM sprint_items WHERE status IN ('todo','in_progress','blocked') AND project = $1 ORDER BY priority, created_at", project)
            if not rows:
                return f"No active sprint items for {project}."
            prio_map = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
            lines = []
            for s in rows:
                prefix = f"[{s['project']}] " if project == "all" else ""
                blocker = f" ⚠️ BLOCKED: {s['blocker']}" if s.get('blocker') else ""
                lines.append(f"  {s['id']} [{s['status']}] {prio_map.get(s['priority'], '?')}: {prefix}{s['title']}{blocker}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing sprint: {e}"

    @mcp_server.tool()
    async def create_sprint_item(title: str, project: str = "fono", priority: int = 3, description: str = "", status: str = "todo", blocker: str = "") -> str:
        """Create a sprint item. Priority 1-4 (1=CRITICAL, 2=HIGH, 3=MEDIUM, 4=LOW). Status: todo/in_progress/blocked/done."""
        try:
            pool = await _get_pool()
            row = await pool.fetchrow("""
                INSERT INTO sprint_items (project, title, description, priority, status, blocker)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, title, priority, status
            """, project, title, description or None, priority, status, blocker or None)
            prio_map = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
            return f"[{project}] Sprint item created: {row['id']} \"{row['title']}\" (priority: {prio_map.get(row['priority'], '?')}, status: {row['status']})"
        except Exception as e:
            return f"Error creating sprint item: {e}"

    @mcp_server.tool()
    async def update_sprint_item(item_id: str, status: str = "", priority: int = 0, blocker: str = "", description: str = "") -> str:
        """Update a sprint item. item_id is the UUID from list_sprint. Set blocker='clear' to remove a blocker."""
        try:
            pool = await _get_pool()
            existing = await pool.fetchrow("SELECT * FROM sprint_items WHERE id = $1::uuid", item_id)
            if not existing:
                return f"Sprint item {item_id} not found."

            sets = ["updated_at = now()"]
            params = []
            idx = 1

            if status:
                sets.append(f"status = ${idx}")
                params.append(status)
                idx += 1
                if status == "done":
                    sets.append("completed_at = now()")

            if priority > 0:
                sets.append(f"priority = ${idx}")
                params.append(priority)
                idx += 1

            if blocker:
                if blocker.lower() in ("clear", "none", "resolved"):
                    sets.append("blocker = NULL")
                else:
                    sets.append(f"blocker = ${idx}")
                    params.append(blocker)
                    idx += 1

            if description:
                sets.append(f"description = ${idx}")
                params.append(description)
                idx += 1

            if len(sets) == 1:
                return "No fields to update. Provide status, priority, blocker, or description."

            params.append(item_id)
            query = f"UPDATE sprint_items SET {', '.join(sets)} WHERE id = ${idx}::uuid RETURNING id, title, status, priority"
            row = await pool.fetchrow(query, *params)

            prio_map = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
            return f"Sprint item updated: {row['id']} \"{row['title']}\" → status={row['status']}, priority={prio_map.get(row['priority'], '?')}"
        except Exception as e:
            return f"Error updating sprint item: {e}"

    @mcp_server.tool()
    async def delete_sprint_item(item_id: str) -> str:
        """Delete a sprint item by its UUID."""
        try:
            pool = await _get_pool()
            row = await pool.fetchrow("DELETE FROM sprint_items WHERE id = $1::uuid RETURNING id, title", item_id)
            if not row:
                return f"Sprint item {item_id} not found."
            return f"Sprint item deleted: {row['id']} \"{row['title']}\""
        except Exception as e:
            return f"Error deleting sprint item: {e}"

    @mcp_server.tool()
    async def list_deployments(project: str = "fono") -> str:
        """List deployment states for a project. Use project='all' for cross-project view."""
        try:
            pool = await _get_pool()
            if project == "all":
                rows = await pool.fetch("SELECT project, component, status, version FROM deployment_state ORDER BY project, component")
            else:
                rows = await pool.fetch("SELECT project, component, status, version FROM deployment_state WHERE project = $1 ORDER BY component", project)
            if not rows:
                return f"No deployments for {project}."
            lines = []
            for d in rows:
                prefix = f"[{d['project']}] " if project == "all" else ""
                lines.append(f"{'✅' if d['status']=='deployed' else '📋'} {prefix}{d['component']} [{d['status']}] {d['version'] or ''}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing deployments: {e}"
    
    @mcp_server.tool()
    async def list_open_questions(project: str = "fono") -> str:
        """List unresolved questions for a project. Use project='all' for cross-project view."""
        try:
            pool = await _get_pool()
            if project == "all":
                rows = await pool.fetch("SELECT project, question, context FROM open_questions WHERE status = 'open' ORDER BY created_at")
            else:
                rows = await pool.fetch("SELECT project, question, context FROM open_questions WHERE status = 'open' AND project = $1 ORDER BY created_at", project)
            if not rows:
                return f"No open questions for {project}."
            lines = []
            for q in rows:
                prefix = f"[{q['project']}] " if project == "all" else ""
                lines.append(f"? {prefix}{q['question']}")
                if q.get('context'):
                    lines.append(f"  Context: {q['context']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing questions: {e}"
    
    @mcp_server.tool()
    async def search_docs(query: str, project: str = "fono") -> str:
        """Search the document knowledge graph for a project."""
        try:
            pool = await _get_pool()
            if project == "all":
                rows = await pool.fetch("""
                    SELECT doc_id, name, type FROM doc_nodes
                    WHERE status = 'current'
                      AND (name ILIKE '%' || $1 || '%' OR doc_id ILIKE '%' || $1 || '%'
                           OR content::text ILIKE '%' || $1 || '%')
                    ORDER BY CASE WHEN name ILIKE '%' || $1 || '%' THEN 0 ELSE 1 END, doc_id
                    LIMIT 20
                """, query)
            else:
                rows = await pool.fetch("""
                    SELECT doc_id, name, type FROM doc_nodes
                    WHERE status = 'current' AND project = $2
                      AND (name ILIKE '%' || $1 || '%' OR doc_id ILIKE '%' || $1 || '%'
                           OR content::text ILIKE '%' || $1 || '%')
                    ORDER BY CASE WHEN name ILIKE '%' || $1 || '%' THEN 0 ELSE 1 END, doc_id
                    LIMIT 20
                """, query, project)
            if not rows:
                return f"No documents found matching '{query}' in {project}."
            lines = [f"Found {len(rows)} results for '{query}':"]
            for r in rows:
                lines.append(f"- [{r['doc_id']}] {r['name']} ({r['type']})")
            return "\n".join(lines)
        except Exception as e:
            return f"Error searching docs: {e}"
    
    @mcp_server.tool()
    async def get_doc_manifest(project: str = "fono") -> str:
        """Get the live document manifest for a project."""
        try:
            pool = await _get_pool()
            if project == "all":
                rows = await pool.fetch("SELECT project, doc_id, name, version, status FROM doc_nodes WHERE type = 'document' ORDER BY project, doc_id")
            else:
                rows = await pool.fetch("SELECT project, doc_id, name, version, status FROM doc_nodes WHERE type = 'document' AND project = $1 ORDER BY doc_id", project)
            lines = [f"Document Manifest ({len(rows)} documents):"]
            for d in rows:
                prefix = f"[{d['project']}] " if project == "all" else ""
                lines.append(f"- {prefix}{d['doc_id']}: {d['name']} [v{d.get('version', '?')}] ({d.get('status', '?')})")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting manifest: {e}"
    
    @mcp_server.tool()
    async def list_projects() -> str:
        """List all projects CHIRAN manages."""
        try:
            pool = await _get_pool()
            rows = await pool.fetch("SELECT id, name, description, status FROM projects ORDER BY id")
            if not rows:
                return "No projects registered."
            lines = ["CHIRAN manages these projects:"]
            for p in rows:
                emoji = "✅" if p['status'] == 'active' else "⏸️"
                lines.append(f"  {emoji} {p['id']}: {p['name']} — {p['description'] or 'No description'}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing projects: {e}"

    # ── TASK QUEUE MCP TOOLS ────────────────────────────────────────────────

    @mcp_server.tool()
    async def create_task(title: str, brief: str = "", project: str = "fono", priority: int = 3, assigned_to: str = "claude_code", depends_on: str = "", status: str = "draft") -> str:
        """Create a task in the CHIRAN task queue. Set priority 1-5 (1=critical). depends_on is comma-separated task IDs."""
        try:
            pool = await _get_pool()
            deps = None
            if depends_on:
                deps = [int(d.strip().replace("T-", "")) for d in depends_on.split(",") if d.strip()]
            row = await pool.fetchrow("""
                INSERT INTO tasks (project, title, brief, priority, status, assigned_to, depends_on, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'claude_chat')
                RETURNING id, title, priority, status
            """, project, title, brief or None, priority, status, assigned_to, deps)
            prio_map = {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "backlog"}
            return f"[{project}] Task created: T-{row['id']} \"{row['title']}\" (priority: {prio_map.get(row['priority'], '?')}, status: {row['status']})"
        except Exception as e:
            return f"Error creating task: {e}"

    @mcp_server.tool()
    async def list_tasks(project: str = "fono", status: str = "") -> str:
        """List tasks from the CHIRAN task queue. Filter by project and/or status. Use project='all' for cross-project."""
        try:
            pool = await _get_pool()
            if status:
                if project == "all":
                    rows = await pool.fetch("SELECT * FROM tasks WHERE status = $1 ORDER BY priority, created_at", status)
                else:
                    rows = await pool.fetch("SELECT * FROM tasks WHERE status = $1 AND project = $2 ORDER BY priority, created_at", status, project)
            else:
                if project == "all":
                    rows = await pool.fetch("SELECT * FROM tasks WHERE status NOT IN ('done', 'failed') ORDER BY priority, created_at")
                else:
                    rows = await pool.fetch("SELECT * FROM tasks WHERE status NOT IN ('done', 'failed') AND project = $1 ORDER BY priority, created_at", project)
                # Also fetch recently done (last 5)
                if project == "all":
                    done_rows = await pool.fetch("SELECT * FROM tasks WHERE status IN ('done', 'failed') ORDER BY completed_at DESC LIMIT 5")
                else:
                    done_rows = await pool.fetch("SELECT * FROM tasks WHERE status IN ('done', 'failed') AND project = $1 ORDER BY completed_at DESC LIMIT 5", project)
                rows = list(rows) + list(done_rows)

            if not rows:
                return f"No tasks found for {project}."

            prio_emoji = {1: "\U0001f534 P1", 2: "\U0001f7e0 P2", 3: "\U0001f7e1 P3", 4: "\u26aa P4", 5: "\U0001f4a4 P5"}
            groups = {}
            for t in rows:
                s = t["status"]
                if s not in groups:
                    groups[s] = []
                groups[s].append(t)

            order = ["approved", "ready", "running", "draft", "blocked", "done", "failed"]
            labels = {"approved": "APPROVED (ready to execute)", "ready": "READY (needs approval)",
                      "running": "RUNNING", "draft": "DRAFT (needs review)",
                      "blocked": "BLOCKED", "done": "RECENTLY DONE", "failed": "FAILED"}

            lines = [f"TASK QUEUE — {project}", ""]
            for s in order:
                if s not in groups:
                    continue
                lines.append(f"  {labels.get(s, s.upper())}:")
                for t in groups[s]:
                    prefix = f"[{t['project']}] " if project == "all" else ""
                    pe = prio_emoji.get(t["priority"], "?")
                    line = f"  [T-{t['id']}] {pe}: {prefix}{t['title']}"
                    if t.get("assigned_to"):
                        line += f" (assigned: {t['assigned_to']})"
                    if s == "running" and t.get("started_at"):
                        elapsed = datetime.now(timezone.utc) - t["started_at"]
                        hours = int(elapsed.total_seconds() // 3600)
                        if hours > 0:
                            line += f" started: {hours}h ago"
                    if s == "blocked" and t.get("blocked_reason"):
                        line += f" — {t['blocked_reason']}"
                    if s in ("done", "failed") and t.get("completed_at"):
                        elapsed = datetime.now(timezone.utc) - t["completed_at"]
                        hours = int(elapsed.total_seconds() // 3600)
                        line += f" (completed: {hours}h ago)"
                    if s == "done":
                        line = f"  [T-{t['id']}] \u2705 {prefix}{t['title']}"
                        if t.get("completed_at"):
                            elapsed = datetime.now(timezone.utc) - t["completed_at"]
                            hours = int(elapsed.total_seconds() // 3600)
                            line += f" (completed: {hours}h ago)"
                    lines.append(line)
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            return f"Error listing tasks: {e}"

    @mcp_server.tool()
    async def update_task(task_id: str, status: str = "", result: str = "", error: str = "", brief: str = "", priority: int = 0, blocked_reason: str = "") -> str:
        """Update a task's status, result, or other fields. task_id can be 'T-1' or '1'."""
        try:
            pool = await _get_pool()
            tid = int(task_id.replace("T-", ""))

            # Fetch current task
            task = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", tid)
            if not task:
                return f"Task T-{tid} not found."

            sets = ["updated_at = now()"]
            params = []
            idx = 1

            if status:
                sets.append(f"status = ${idx}")
                params.append(status)
                idx += 1
                if status == "running":
                    sets.append("started_at = now()")
                elif status in ("done", "failed"):
                    sets.append("completed_at = now()")

            if result:
                sets.append(f"result = ${idx}")
                params.append(result)
                idx += 1

            if error:
                sets.append(f"error = ${idx}")
                params.append(error)
                idx += 1

            if brief:
                sets.append(f"brief = ${idx}")
                params.append(brief)
                idx += 1

            if priority > 0:
                sets.append(f"priority = ${idx}")
                params.append(priority)
                idx += 1

            if blocked_reason:
                sets.append(f"blocked_reason = ${idx}")
                params.append(blocked_reason)
                idx += 1

            params.append(tid)
            query = f"UPDATE tasks SET {', '.join(sets)} WHERE id = ${idx} RETURNING id, title, status, result"
            row = await pool.fetchrow(query, *params)

            parts = [f"[{task['project']}] Task T-{row['id']} updated:"]
            if status:
                parts.append(f"status -> {status}")
            if result:
                parts.append(f"result: {result[:100]}")
            if error:
                parts.append(f"error: {error[:100]}")
            if brief:
                parts.append("brief updated")
            if priority > 0:
                parts.append(f"priority -> {priority}")
            if blocked_reason:
                parts.append(f"blocked: {blocked_reason}")

            return ", ".join(parts)
        except Exception as e:
            return f"Error updating task: {e}"

    # Mount MCP Streamable HTTP app
    # FastMCP's streamable_http_app() creates routes at /mcp internally
    # Mounting at /mcp-server makes the endpoint /mcp-server/mcp
    # which matches the Claude.ai connector URL
    app.mount("/mcp-server", mcp_server.streamable_http_app())
    print("🔌 MCP server mounted at /mcp-server (Streamable HTTP, endpoint: /mcp-server/mcp)")

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

    # 9. Task queue
    active_tasks = await pool.fetch(
        "SELECT * FROM tasks WHERE status NOT IN ('done', 'failed') ORDER BY priority, created_at"
    )
    done_tasks = await pool.fetch(
        "SELECT * FROM tasks WHERE status IN ('done', 'failed') ORDER BY completed_at DESC LIMIT 5"
    )

    # Build the handoff document
    handoff = _build_handoff_document(
        deployments, decisions, sprint, sessions, questions, code_map,
        doc_staleness, doc_count, float(cost_total),
        tasks=list(active_tasks) + list(done_tasks),
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
            "active_tasks": len(active_tasks),
            "cost_7d_usd": float(cost_total),
        },
        "usage": "Copy the 'handoff' field content and paste at the start of a new Claude session."
    }


def _build_handoff_document(
    deployments, decisions, sprint, sessions, questions, code_map,
    doc_staleness=None, doc_count=0, cost_7d=0.0, project_name="FONO",
    tasks=None,
) -> str:
    """Build the structured handoff document for Claude."""
    
    lines = []
    lines.append("=" * 70)
    if project_name == "ALL":
        lines.append("CHIRAN — CROSS-PROJECT CONTEXT HANDOFF")
    else:
        lines.append(f"{project_name} — CHIRAN CONTEXT HANDOFF")
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
                    a.get("name", "?") if isinstance(a, dict) else str(a) for a in alts
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

    # --- TASK QUEUE ---
    lines.append("## TASK QUEUE (Delegated Work Items)")
    lines.append("")
    if tasks:
        priority_emoji = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢"}
        status_icon = {
            "draft": "📝", "ready": "📦", "approved": "📦",
            "in_progress": "⚙️", "running": "⚙️",
            "blocked": "🚫", "done": "✅", "failed": "❌",
        }
        # Group: active first, then completed/failed
        active = [t for t in tasks if t["status"] not in ("done", "failed")]
        finished = [t for t in tasks if t["status"] in ("done", "failed")]

        if active:
            for t in active:
                icon = status_icon.get(t["status"], "❓")
                prio = priority_emoji.get(t["priority"], "")
                dep_str = ""
                if t.get("depends_on"):
                    dep_str = f" (depends on: {t['depends_on']})"
                blocked_str = ""
                if t.get("blocked_reason"):
                    blocked_str = f" ⚠️ {t['blocked_reason']}"
                lines.append(f"  {icon} {prio} #{t['id']} {t['title']} [{t['status']}]{dep_str}{blocked_str}")
                if t.get("brief"):
                    brief_lines = t["brief"].strip().split("\n")
                    lines.append(f"       Brief: {brief_lines[0]}" + (f" (+{len(brief_lines)-1} lines)" if len(brief_lines) > 1 else ""))

        if finished:
            lines.append("")
            lines.append("  Recently completed:")
            for t in finished:
                icon = status_icon.get(t["status"], "❓")
                result_str = ""
                if t.get("result"):
                    result_str = f" → {t['result'][:80]}"
                elif t.get("error"):
                    result_str = f" → ERROR: {t['error'][:80]}"
                lines.append(f"    {icon} #{t['id']} {t['title']}{result_str}")
    else:
        lines.append("  (no tasks queued)")
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
