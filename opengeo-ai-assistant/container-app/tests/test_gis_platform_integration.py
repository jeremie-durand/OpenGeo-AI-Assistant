"""
Tests for structured GIS feature data routing and generic GIS platform integration.

Fixes covered:
  Fix 1 — Generic structured-feature pre-check in RouterAgent.route_query()
  Fix 2 — query_gis_feature kernel tool in RouterAgentTools
  Fix 3 — GIS_FEATURE_QUERY rule in ROUTER_AGENT_INSTRUCTIONS
  Fix 4 — gis_feature_query action handler in fastapi_app.py
"""

import json
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SESSION_ID = "test-session-001"

# Simulated LLM response that misclassifies a numeric feature ID as a location.
_BUGGY_LLM = {"has_location": True, "location": "4842", "has_collection": False}


def _agent_with_buggy_llm():
    """Return a RouterAgent whose LLM always returns the navigate-to misclassification."""
    from geoint.router_agent import RouterAgent

    agent = RouterAgent()
    agent._classify_query_with_llm = AsyncMock(return_value=_BUGGY_LLM)
    return agent


# ===========================================================================
# Fix 1 — Generic structured-feature pre-check
# ===========================================================================


class TestStructuredFeatureDataRouting:
    """
    Queries that contain a GIS feature-ID field (e.g. "Parcel ID: 123",
    "Feature ID: abc", "FID: 7") or an attribute block ("Recorded attributes:")
    carry structured platform data and must route to contextual, regardless of
    which platform sent them.
    """

    # ── Feature-ID pattern tests ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_parcel_id_field_routes_to_contextual(self):
        """'Parcel ID: <value>' pattern must trigger contextual routing."""
        agent = _agent_with_buggy_llm()
        query = (
            "Parcel ID: 4842\n"
            "Dataset: bdppad_v03_an_2023_s_20250120\n"
            "Recorded attributes:\n  typpar: PAC\n  suphec: 8.2\n"
            "Please provide an agronomic analysis of this parcel."
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] == "contextual", (
            f"Expected 'contextual', got '{result['action_type']}'. "
            "'Parcel ID: <value>' must not be treated as a navigation target."
        )

    @pytest.mark.asyncio
    async def test_feature_id_field_routes_to_contextual(self):
        """'Feature ID: <value>' from any GIS platform must route contextual."""
        agent = _agent_with_buggy_llm()
        query = (
            "Feature ID: F-20391\n"
            "Layer: cadastre_urban_2024\n"
            "Attributes:\n  landuse: residential\n  area_sqm: 1200\n"
            "What is the zoning assessment for this feature?"
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] == "contextual"

    @pytest.mark.asyncio
    async def test_object_id_field_routes_to_contextual(self):
        """'Object ID: <value>' (Esri convention) must route contextual."""
        agent = _agent_with_buggy_llm()
        query = (
            "Object ID: 789\n"
            "Properties:\n  zone: AG-3\n  area_ha: 4.7\n"
            "Please provide a land-use analysis."
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] == "contextual"

    @pytest.mark.asyncio
    async def test_fid_field_routes_to_contextual(self):
        """Short 'FID: <value>' pattern must route contextual."""
        agent = _agent_with_buggy_llm()
        query = (
            "FID: 42\n"
            "Attributes: area_sqm=12000, zone=AG\n"
            "What can you tell me about this feature?"
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] == "contextual"

    # ── Attribute-block pattern tests ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_recorded_attributes_block_routes_to_contextual(self):
        """Presence of 'Recorded attributes:' alone must trigger contextual routing."""
        agent = _agent_with_buggy_llm()
        query = (
            "Selected feature from the platform.\n"
            "Recorded attributes:\n  typpar: PAC\n  suphec: 8.2\n"
            "Provide analysis."
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] == "contextual"

    @pytest.mark.asyncio
    async def test_attributes_block_routes_to_contextual(self):
        """Presence of 'Attributes:' block must trigger contextual routing."""
        agent = _agent_with_buggy_llm()
        query = (
            "Selected parcel.\n"
            "Attributes:\n  area: 3.2 ha\n  crop: wheat\n"
            "Give me an agronomic assessment."
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] == "contextual"

    # ── Routing reason ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_routing_reason_is_structured_feature_data(self):
        """Pre-check must set routing_reason='structured_feature_data_context'."""
        agent = _agent_with_buggy_llm()
        query = (
            "Parcel ID: 4842\nRecorded attributes:\n  suphec: 8.2\n"
            "Please analyse."
        )
        result = await agent.route_query(query, _SESSION_ID)
        assert result.get("routing_reason") == "structured_feature_data_context", (
            f"Got routing_reason='{result.get('routing_reason')}', "
            "expected 'structured_feature_data_context'."
        )

    # ── Non-regression: normal queries must not be affected ───────────────────

    @pytest.mark.asyncio
    async def test_bare_location_query_still_navigates(self):
        """A plain location query must not be caught by the feature pre-check."""
        from geoint.router_agent import RouterAgent

        agent = RouterAgent()
        result = await agent.route_query("Show me Paris", _SESSION_ID)
        assert result["action_type"] == "navigate_to"

    @pytest.mark.asyncio
    async def test_educational_question_with_id_in_text_is_not_caught(self):
        """A short educational question that mentions 'parcel' must not be misrouted."""
        from geoint.router_agent import RouterAgent

        agent = RouterAgent()
        # No "Parcel ID: <value>" pattern — just the word "parcel" in a question.
        query = "What is a parcel in land registry?"
        result = await agent.route_query(query, _SESSION_ID)
        assert result["action_type"] != "contextual" or result.get(
            "routing_reason"
        ) != "structured_feature_data_context", (
            "A bare educational question must not hit the structured-feature pre-check."
        )


# ===========================================================================
# Fix 3a — query_gis_feature kernel tool in RouterAgentTools
# ===========================================================================


class TestQueryGisFeatureTool:
    """RouterAgentTools must expose a query_gis_feature kernel function."""

    def test_query_gis_feature_method_exists(self):
        """RouterAgentTools must have a query_gis_feature method."""
        from geoint.router_agent import RouterAgentTools

        tools = RouterAgentTools()
        assert hasattr(tools, "query_gis_feature"), (
            "RouterAgentTools is missing the query_gis_feature method (Fix 3a)"
        )

    def test_query_gis_feature_sets_pending_action(self):
        """Calling query_gis_feature must set _pending_action to gis_feature_query."""
        from geoint.router_agent import RouterAgentTools

        tools = RouterAgentTools()
        tools.query_gis_feature(
            collection="bdppad_v03_an_2023_s_20250120",
            feature_id="4842",
            session_id=_SESSION_ID,
        )

        action = tools.get_pending_action()
        assert action is not None
        assert action["action_type"] == "gis_feature_query"
        assert action["collection"] == "bdppad_v03_an_2023_s_20250120"
        assert action["feature_id"] == "4842"

    def test_query_gis_feature_returns_json_status(self):
        """query_gis_feature must return valid JSON with status='routed'."""
        from geoint.router_agent import RouterAgentTools

        tools = RouterAgentTools()
        raw = tools.query_gis_feature(
            collection="bdppad_v03_an_2023_s_20250120",
            feature_id="4842",
            session_id=_SESSION_ID,
        )
        parsed = json.loads(raw)
        assert parsed["status"] == "routed"
        assert parsed["action"] == "gis_feature_query"


# ===========================================================================
# Fix 3b — GIS_FEATURE_QUERY routing rule in ROUTER_AGENT_INSTRUCTIONS
# ===========================================================================


class TestRouterAgentInstructions:
    """ROUTER_AGENT_INSTRUCTIONS must document the GIS_FEATURE_QUERY routing rule."""

    def test_instructions_contain_gis_feature_query_rule(self):
        """ROUTER_AGENT_INSTRUCTIONS must mention GIS_FEATURE_QUERY."""
        from geoint.router_agent import ROUTER_AGENT_INSTRUCTIONS

        assert "GIS_FEATURE_QUERY" in ROUTER_AGENT_INSTRUCTIONS, (
            "ROUTER_AGENT_INSTRUCTIONS is missing the GIS_FEATURE_QUERY routing rule (Fix 3b)"
        )

    def test_instructions_mention_query_gis_feature_tool(self):
        """ROUTER_AGENT_INSTRUCTIONS must reference the query_gis_feature tool."""
        from geoint.router_agent import ROUTER_AGENT_INSTRUCTIONS

        assert "query_gis_feature" in ROUTER_AGENT_INSTRUCTIONS


