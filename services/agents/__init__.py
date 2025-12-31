# services/agents/__init__.py
"""
Domain-specialized agents for the Multi-Agent Planner (v8.3 Bygglaget).

Each agent extracts KnowledgeFragments from documents.
"""

from services.agents.chronologist import extract_temporal

__all__ = ['extract_temporal']












