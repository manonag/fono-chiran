"""
Managed Agents Router -- CHIRAN dispatches tasks to Anthropic Managed Agents.

Endpoints:
  POST /managed-agents/dispatch  -- create agent, session, send task with CHIRAN context
  GET  /managed-agents/status/{session_id} -- poll session events and output
"""

import json
import os
from typing import Optional

import asyncpg
import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from managed_agents.sdk import (
    ManagedAgentsClient,
    SDLC_AGENT_TEMPLATES,
    ENVIRONMENT_TEMPLATES,
)


CHIRAN_MCP_URL = os.getenv(
    "CHIRAN_MCP_URL",
    "https://fono-chiran-production.up.railway.app/mcp-server/mcp",
)

SKILLS_REGISTRY_PATH = os.getenv("SKILLS_REGISTRY_PATH", "skills_registry.json")

router = APIRouter(prefix="/managed-agents", tags=["managed-agents"])


# -- Shared DB dependency (same pool as main app) ---------------------------
# This gets overridden when the router is included in the main app.
# We store a reference so endpoints can access the pool.

_db_pool_ref = None


def set_db_pool(pool):
    """Called from main.py after lifespan creates the pool."""
    global _db_pool_ref
    _db_pool_ref = pool


async def get_db() -> asyncpg.Pool:
    if _db_pool_ref is None:
        raise HTTPException(status_code=503, detail="Database not ready")
    return _db_pool_ref


# -- Request/Response models -------------------------------------------------

class DispatchRequest(BaseModel):
    agent_template: str = "developer"
    project: str = "askben"
    task: str
    environment: str = "python-backend"
    skill_ids: Optional[list[str]] = None


class DispatchResponse(BaseModel):
    session_id: str
    agent_id: str
    environment_id: str
    status: str = "dispatched"
    trace_url: str


class SessionStatusResponse(BaseModel):
    session_id: str
    status: Optional[str] = None
    event_count: int = 0
    tools_used: list[str] = []
    output: str = ""


# -- Context loaders (lightweight DB queries) --------------------------------

async def _load_handoff(pool: asyncpg.Pool, project: str) -> str:
    """Build a compact context summary for the agent."""
    sections = []

    # Deployments
    rows = await pool.fetch(
        "SELECT component, status, environment, version "
        "FROM deployment_state WHERE project = $1 ORDER BY component",
        project,
    )
    if rows:
        lines = [f"  - {r['component']}: {r['status']} ({r['environment']} v{r['version']})" for r in rows]
        sections.append("### Deployments\n" + "\n".join(lines))

    # Sprint
    rows = await pool.fetch(
        "SELECT title, status, priority FROM sprint_items "
        "WHERE project = $1 AND status IN ('todo','in_progress','blocked') "
        "ORDER BY priority",
        project,
    )
    if rows:
        prio = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
        lines = [f"  - [{r['status']}] {prio.get(r['priority'], '?')}: {r['title']}" for r in rows]
        sections.append("### Sprint\n" + "\n".join(lines))

    # Recent sessions
    rows = await pool.fetch(
        "SELECT title, summary, next_actions FROM sessions "
        "WHERE project = $1 ORDER BY started_at DESC LIMIT 2",
        project,
    )
    if rows:
        lines = []
        for r in rows:
            lines.append(f"  - {r['title']}: {r['summary']}")
            if r['next_actions']:
                try:
                    actions = json.loads(r['next_actions']) if isinstance(r['next_actions'], str) else r['next_actions']
                    for a in actions[:3]:
                        action_text = a.get("action", str(a)) if isinstance(a, dict) else str(a)
                        lines.append(f"    Next: {action_text}")
                except (json.JSONDecodeError, TypeError):
                    pass
        sections.append("### Recent Sessions\n" + "\n".join(lines))

    # Open questions
    rows = await pool.fetch(
        "SELECT question FROM open_questions WHERE status = 'open' AND project = $1",
        project,
    )
    if rows:
        lines = [f"  - {r['question']}" for r in rows]
        sections.append("### Open Questions\n" + "\n".join(lines))

    return "\n\n".join(sections) if sections else "(No context found for this project)"


async def _load_decisions(pool: asyncpg.Pool, project: str) -> str:
    rows = await pool.fetch(
        "SELECT title, decision, reasoning FROM decisions "
        "WHERE status = 'active' AND project = $1 ORDER BY decided_at DESC",
        project,
    )
    if not rows:
        return "(No active decisions)"
    lines = [f"- {r['title']}: {r['decision']} (Why: {r['reasoning']})" for r in rows]
    return "\n".join(lines)


async def _load_principles(pool: asyncpg.Pool, project: str) -> str:
    rows = await pool.fetch(
        "SELECT category, text FROM principles "
        "WHERE active = true AND (project = $1 OR project IS NULL) "
        "ORDER BY category, id",
        project,
    )
    if not rows:
        return "(No active principles)"
    lines = [f"- [{r['category']}] {r['text']}" for r in rows]
    return "\n".join(lines)


def _load_skill_ids(project: str) -> list[str]:
    """Load skill IDs from the registry file for platform + project-specific skills."""
    try:
        with open(SKILLS_REGISTRY_PATH) as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    ids = []
    # Always include platform skills
    if "platform" in registry:
        ids.append(registry["platform"])
    # Include project-specific skills
    if project in registry:
        ids.append(registry[project])
    return ids


# -- Endpoints ---------------------------------------------------------------

@router.post("/dispatch", response_model=DispatchResponse)
async def dispatch_task(req: DispatchRequest, pool: asyncpg.Pool = Depends(get_db)):
    """
    CHIRAN dispatches a task to a Managed Agent.

    1. Loads project context from CHIRAN DB (handoff, decisions, principles)
    2. Loads relevant skills (platform + project-specific)
    3. Creates agent with skills + CHIRAN MCP attached
    4. Creates environment
    5. Creates session and sends task with context injected
    6. Returns session_id for polling
    """
    # Validate inputs
    if req.agent_template not in SDLC_AGENT_TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown agent_template '{req.agent_template}'. "
                   f"Available: {list(SDLC_AGENT_TEMPLATES.keys())}",
        )
    if req.environment not in ENVIRONMENT_TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown environment '{req.environment}'. "
                   f"Available: {list(ENVIRONMENT_TEMPLATES.keys())}",
        )

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY not configured. Add it to Doppler.",
        )

    # Load CHIRAN context
    handoff = await _load_handoff(pool, req.project)
    decisions = await _load_decisions(pool, req.project)
    principles = await _load_principles(pool, req.project)

    # Resolve skills
    skill_ids = req.skill_ids or _load_skill_ids(req.project)

    # Create client
    client = ManagedAgentsClient(api_key=api_key)

    try:
        # Create agent
        template = SDLC_AGENT_TEMPLATES[req.agent_template]
        agent = client.create_agent(
            name=template["name"],
            system_prompt=template["system"],
            model="claude-sonnet-4-6",
            tools=[
                {"type": "agent_toolset_20260401"},
                {"type": "mcp_toolset", "mcp_server_name": "chiran"},
            ],
            mcp_servers=[
                {
                    "type": "url",
                    "url": CHIRAN_MCP_URL,
                    "name": "chiran",
                }
            ],
            skill_ids=skill_ids if skill_ids else None,
        )

        # Create environment
        env_template = ENVIRONMENT_TEMPLATES[req.environment]
        env = client.create_environment(
            name=env_template["name"],
        )

        # Create session
        session = client.create_session(
            agent_id=agent["id"],
            environment_id=env["id"],
            title=f"CHIRAN: {req.project} - {req.task[:40]}",
        )

        # Build message with CHIRAN context layers
        message = f"""## Project Context ({req.project})
{handoff}

## Active Decisions
{decisions}

## Platform Principles
{principles}

## Task
{req.task}
"""

        # Send task
        client.send_message(session["id"], message)

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        url = str(e.response.url) if e.response is not None else "unknown"
        try:
            body = e.response.json() if e.response is not None else {}
        except (ValueError, AttributeError):
            body = {"raw": e.response.text if e.response is not None else str(e)}
        raise HTTPException(
            status_code=status,
            detail={
                "error": "Managed Agents API error",
                "api_status": status,
                "api_url": url,
                "api_response": body,
            },
        )
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach Managed Agents API: {e}",
        )

    return DispatchResponse(
        session_id=session["id"],
        agent_id=agent["id"],
        environment_id=env["id"],
        status="dispatched",
        trace_url=f"https://platform.claude.com/sessions/{session['id']}",
    )


@router.get("/status/{session_id}", response_model=SessionStatusResponse)
async def get_session_status(session_id: str):
    """Fetch session status, events, and output."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured.")

    client = ManagedAgentsClient(api_key=api_key)

    try:
        session = client.get_session(session_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Session not found: {e}")

    try:
        events = client.get_events(session_id)
    except Exception:
        events = []

    # Extract text output and tools used from events
    text_output = []
    tools_used = set()
    for event in events:
        event_type = event.get("type", "")
        if event_type == "agent.message":
            for block in event.get("content", []):
                if block.get("type") == "text":
                    text_output.append(block["text"])
        elif event_type in ("agent.tool_use", "agent.mcp_tool_use"):
            tools_used.add(event.get("name", "unknown"))

    return SessionStatusResponse(
        session_id=session_id,
        status=session.get("status"),
        event_count=len(events),
        tools_used=sorted(tools_used),
        output="\n".join(text_output),
    )
