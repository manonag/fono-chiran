"""
Managed Agents SDK -- thin HTTP client for the Anthropic Managed Agents API.

Wraps agent creation, environment setup, session management, and message dispatch.
All calls use the managed-agents-2026-04-01 beta header.
"""

import os
from typing import Optional

import requests


API_BASE = "https://api.anthropic.com/v1"

BETA_HEADERS = "managed-agents-2026-04-01,skills-2025-10-02"


# -- Agent templates for SDLC roles -----------------------------------------

SDLC_AGENT_TEMPLATES = {
    "developer": {
        "name": "CHIRAN Developer Agent",
        "system": (
            "You are a senior software engineer dispatched by CHIRAN, the Fono platform orchestrator. "
            "You receive project context (handoff, decisions, principles) at the start of every task. "
            "Follow the project's coding conventions, respect recorded decisions, and use CHIRAN MCP tools "
            "to record any new decisions you make. Write production-quality code with tests. "
            "When done, summarize what you built and any open questions."
        ),
    },
    "product-specialist": {
        "name": "CHIRAN Product Specialist",
        "system": (
            "You are a product analyst dispatched by CHIRAN. You review requirements, write specs, "
            "identify edge cases, and produce acceptance criteria. Use CHIRAN MCP tools to check "
            "existing decisions and record new ones. Be precise and structured."
        ),
    },
    "reviewer": {
        "name": "CHIRAN Code Reviewer",
        "system": (
            "You are a code reviewer dispatched by CHIRAN. Review code for correctness, security, "
            "performance, and adherence to project conventions. Check CHIRAN for relevant decisions "
            "and principles. Provide actionable feedback with line references."
        ),
    },
}


# -- Environment templates ---------------------------------------------------

ENVIRONMENT_TEMPLATES = {
    "python-backend": {
        "name": "Python Backend",
        "packages": [
            "fastapi>=0.115.0",
            "pytest>=8.0.0",
            "asyncpg>=0.30.0",
            "pydantic>=2.0.0",
            "requests>=2.31.0",
        ],
    },
    "nextjs-frontend": {
        "name": "Next.js Frontend",
        "packages": [
            "next@14",
            "typescript",
            "tailwindcss",
            "recharts",
        ],
    },
    "minimal": {
        "name": "Minimal",
        "packages": [],
    },
}


class ManagedAgentsClient:
    """HTTP client for the Anthropic Managed Agents API (beta)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required. Set it as an environment variable or pass it directly."
            )
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": BETA_HEADERS,
            "content-type": "application/json",
        })

    # -- Agents --------------------------------------------------------------

    def create_agent(
        self,
        name: str,
        system_prompt: str,
        model: str = "claude-sonnet-4-6",
        tools: Optional[list] = None,
        mcp_servers: Optional[list] = None,
        skill_ids: Optional[list] = None,
    ) -> dict:
        """Create a managed agent with optional tools, MCP servers, and skills."""
        payload: dict = {
            "name": name,
            "model": model,
            "description": system_prompt,
        }
        if tools:
            payload["tools"] = tools
        if mcp_servers:
            payload["mcp_servers"] = mcp_servers
        if skill_ids:
            payload["skills"] = [{"skill_id": sid} for sid in skill_ids]

        resp = self.session.post(f"{API_BASE}/agents", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_agent(self, agent_id: str) -> dict:
        resp = self.session.get(f"{API_BASE}/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()

    def list_agents(self) -> dict:
        resp = self.session.get(f"{API_BASE}/agents")
        resp.raise_for_status()
        return resp.json()

    # -- Environments --------------------------------------------------------

    def create_environment(
        self,
        name: str,
        packages: Optional[list] = None,
    ) -> dict:
        """Create a sandboxed execution environment."""
        payload: dict = {"name": name}
        if packages:
            payload["packages"] = packages

        resp = self.session.post(f"{API_BASE}/environments", json=payload)
        resp.raise_for_status()
        return resp.json()

    # -- Sessions ------------------------------------------------------------

    def create_session(
        self,
        agent_id: str,
        environment_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict:
        """Create a new session for an agent."""
        payload: dict = {"agent_id": agent_id}
        if environment_id:
            payload["environment_id"] = environment_id
        if title:
            payload["title"] = title

        resp = self.session.post(f"{API_BASE}/sessions", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_session(self, session_id: str) -> dict:
        resp = self.session.get(f"{API_BASE}/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    # -- Messages ------------------------------------------------------------

    def send_message(self, session_id: str, content: str) -> dict:
        """Send a user message to a session and return the response."""
        payload = {
            "role": "user",
            "content": content,
        }
        resp = self.session.post(
            f"{API_BASE}/sessions/{session_id}/messages",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    # -- Events (session history) --------------------------------------------

    def get_events(self, session_id: str) -> list:
        """Fetch all events for a session."""
        resp = self.session.get(f"{API_BASE}/sessions/{session_id}/events")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # -- Skills --------------------------------------------------------------

    def upload_skill(self, name: str, content: str, metadata: Optional[dict] = None) -> dict:
        """Upload a skill definition."""
        payload: dict = {
            "name": name,
            "content": content,
        }
        if metadata:
            payload["metadata"] = metadata

        resp = self.session.post(f"{API_BASE}/skills", json=payload)
        resp.raise_for_status()
        return resp.json()

    def list_skills(self) -> dict:
        resp = self.session.get(f"{API_BASE}/skills")
        resp.raise_for_status()
        return resp.json()
