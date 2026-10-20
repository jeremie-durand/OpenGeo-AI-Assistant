"""GEOINT mobility and terrain analysis endpoints, including terrain chat."""

import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from request_models import MobilityRequest, TerrainChatRequest, TerrainRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/geoint/mobility")
async def geoint_mobility_analysis(body: MobilityRequest):
    """
    GEOINT Mobility Analysis Endpoint

    Thin wrapper around mobility_analysis_agent.
    Called when user drops pin with GEOINT toggle enabled.
    Supports two-point A->B traversability analysis.

    Request body:
    {
        "latitude": float,       # Point A latitude (start)
        "longitude": float,      # Point A longitude (start)
        "latitude_b": float,     # Point B latitude (destination, optional)
        "longitude_b": float,    # Point B longitude (destination, optional)
        "screenshot": str (optional - base64 screenshot),
        "user_query": str (optional - user context),
        "user_context": str (optional - legacy field name)
    }
    """
    try:
        logger.info("[MEDAL] GEOINT Mobility endpoint called")

        latitude = body.latitude
        longitude = body.longitude
        latitude_b = body.latitude_b
        longitude_b = body.longitude_b
        screenshot_base64 = body.screenshot
        user_query = body.user_query

        logger.info(f"Validated coordinates: ({latitude}, {longitude})")
        if latitude_b is not None and longitude_b is not None:
            logger.info(f"Point B coordinates: ({latitude_b}, {longitude_b})")

        # Call mobility_analysis_agent (new agent-based architecture)
        from geoint.agents import mobility_analysis_agent

        analysis_result = await mobility_analysis_agent(
            latitude=latitude,
            longitude=longitude,
            screenshot_base64=screenshot_base64,
            user_query=user_query,
            include_vision=True,
            latitude_b=latitude_b,
            longitude_b=longitude_b,
        )

        logger.info("Mobility agent completed")

        return {
            "status": "success",
            "result": analysis_result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FAIL] GEOINT Mobility Analysis failed: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="GEOINT mobility analysis failed")


@router.post("/api/geoint/terrain")
async def geoint_terrain_analysis(body: TerrainRequest):
    """
    GEOINT Terrain Analysis - Unified endpoint for initial analysis AND follow-ups

    This endpoint now supports:
    1. Initial terrain analysis (no session_id) - One-shot analysis using terrain_analysis_agent
    2. Follow-up questions (with session_id) - Uses TerrainAgent with conversation memory

    Request body:
    {
        "latitude": float,
        "longitude": float,
        "screenshot": str (optional - base64 screenshot),
        "user_query": str (optional - user context),
        "user_context": str (optional - legacy field name),
        "radius_miles": float (optional - defaults to 5.0),
        "session_id": str (optional - provide for follow-up questions to maintain context)
    }

    Response includes session_id that can be used for follow-up questions.
    """
    import uuid

    request_id = f"terrain-{datetime.now(timezone.utc).timestamp()}"
    try:
        logger.info(
            f"[MTN] [{request_id}] ============================================================"
        )
        logger.info(f"[MTN] [{request_id}] TERRAIN ENDPOINT CALLED")
        logger.info(
            f"[MTN] [{request_id}] ============================================================"
        )

        latitude = body.latitude
        longitude = body.longitude
        screenshot = body.screenshot
        user_query = body.user_query
        radius_miles = body.radius_miles
        session_id = body.session_id

        screenshot_size = len(screenshot) if screenshot else 0
        logger.info(
            f"[MTN] [{request_id}] Screenshot size: {screenshot_size} chars ({screenshot_size / 1024:.1f} KB)"
        )
        logger.info(f"[MTN] [{request_id}] Session ID: {session_id or 'NEW SESSION'}")

        # ===== CRITICAL DEBUG: Screenshot validation =====
        if screenshot:
            logger.info(f"[MTN] [{request_id}] Screenshot received: YES")
            logger.info(
                f"[MTN] [{request_id}] Screenshot starts with: {screenshot[:50]}"
            )

            # Check if it has data URL prefix (should be removed by frontend)
            if screenshot.startswith("data:image"):
                logger.warning(
                    f"[MTN] [{request_id}] [WARN] Screenshot has data URL prefix (will be handled)"
                )
            else:
                logger.info(
                    f"[MTN] [{request_id}] Screenshot is pure base64 (correct format)"
                )

            # Validate minimum size (empty canvas produces very small PNGs)
            if screenshot_size < 5000:
                logger.warning(
                    f"[MTN] [{request_id}] [WARN] Screenshot is very small ({screenshot_size} chars) - may be blank canvas!"
                )
            else:
                logger.info(
                    f"[MTN] [{request_id}] Screenshot size looks good (>{screenshot_size / 1024:.1f}KB)"
                )
        else:
            logger.warning(
                f"[MTN] [{request_id}] [WARN] NO SCREENSHOT PROVIDED - will use STAC fallback"
            )
        # ===== END CRITICAL DEBUG =====

        logger.info(f"[MTN] [{request_id}] Validating parameters...")

        logger.info(f"[MTN] [{request_id}] [OK] Parameters valid")
        logger.info(f"[MTN] [{request_id}] Coordinates: ({latitude}, {longitude})")
        logger.info(f"[MTN] [{request_id}] Radius: {radius_miles} miles")
        logger.info(f"[MTN] [{request_id}] User query: {user_query}")

        import time

        agent_start = time.time()

        # ========================================================================
        # DECISION: Use TerrainAgent (with memory) if session_id provided OR user_query exists
        # Otherwise use one-shot terrain_analysis_agent for initial analysis
        # ========================================================================

        radius_km = radius_miles * 1.609

        if not session_id:
            session_id = str(uuid.uuid4())
            logger.info(f"[MTN] [{request_id}] Created new session: {session_id}")

        message = (
            user_query
            or "Provide a comprehensive terrain analysis of this location including elevation, slope, and terrain classification."
        )

        # Try Agent Service first, fall back to direct tools if PE-blocked
        agent_result = None
        try:
            if user_query or session_id:
                logger.info(
                    f"[MTN] [{request_id}] USING TERRAIN AGENT (with memory and tools)"
                )
                from geoint.terrain_agent import get_terrain_agent

                agent = get_terrain_agent()
                agent_result = await asyncio.wait_for(
                    agent.chat(
                        session_id=session_id,
                        user_message=message,
                        latitude=latitude,
                        longitude=longitude,
                        screenshot_base64=screenshot,
                        radius_km=radius_km,
                    ),
                    timeout=240.0,
                )
            else:
                logger.info(
                    f"[MTN] [{request_id}] USING LEGACY ONE-SHOT AGENT (no memory)"
                )
                from geoint.agents import terrain_analysis_agent

                agent_result = await asyncio.wait_for(
                    terrain_analysis_agent(
                        latitude=latitude,
                        longitude=longitude,
                        screenshot_base64=screenshot,
                        user_query=user_query,
                        radius_miles=radius_miles,
                    ),
                    timeout=240.0,
                )

            # Check if response contains PE error
            resp_text = str(agent_result.get("response", ""))
            if (
                "403" in resp_text
                or "Public access is disabled" in resp_text
                or "private endpoint" in resp_text.lower()
            ):
                logger.warning(
                    f"[MTN] [{request_id}] Agent returned PE error, falling back to direct tools"
                )
                agent_result = None
        except asyncio.TimeoutError:
            agent_elapsed = time.time() - agent_start
            logger.error(
                f"[MTN] [{request_id}] [FAIL] AGENT TIMEOUT after {agent_elapsed:.1f}s"
            )
            raise HTTPException(
                status_code=504,
                detail=f"Terrain analysis timed out after {agent_elapsed:.1f}s. Please try again.",
            )
        except Exception as agent_err:
            logger.warning(
                f"[MTN] [{request_id}] Agent failed ({agent_err}), falling back to direct tools"
            )
            agent_result = None

        return {
            "status": "success",
            "result": {
                "analysis": agent_result.get("response", "")
                or agent_result.get("analysis", ""),
                "tool_calls": agent_result.get("tool_calls", []),
                "message_count": agent_result.get("message_count", 1),
            },
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"[MTN] [{request_id}] ============================================================"
        )
        logger.error(f"[MTN] [{request_id}] [FAIL] TERRAIN ENDPOINT FAILED")
        logger.error(
            f"[MTN] [{request_id}] ============================================================"
        )
        logger.error(f"[MTN] [{request_id}] Error: {e}")
        logger.error(f"[MTN] [{request_id}] Traceback:")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail=f"Terrain analysis failed: {str(e)}"
        )


@router.post("/api/geoint/terrain/chat")
async def geoint_terrain_chat(body: TerrainChatRequest):
    """
    [BOT] GEOINT Terrain Agent Chat - Multi-turn conversation with memory

    A real AI agent that:
    - Remembers context from previous messages in the session
    - Calls terrain analysis tools (DEM, NDVI, slope, etc.) as needed
    - Synthesizes tool results into coherent answers

    Request body:
    {
        "session_id": str (optional - will create new session if not provided),
        "message": str (required - user's question),
        "latitude": float (required),
        "longitude": float (required),
        "screenshot": str (optional - base64 screenshot),
        "radius_km": float (optional - defaults to 5.0)
    }

    Returns:
    {
        "response": str (agent's answer),
        "tool_calls": list (tools the agent invoked),
        "session_id": str (use this for follow-up questions),
        "message_count": int (messages in this session)
    }
    """
    import uuid

    request_id = f"terrain-chat-{datetime.now(timezone.utc).timestamp()}"

    try:
        logger.info(
            f"[MSG] [{request_id}] ============================================================"
        )
        logger.info(f"[MSG] [{request_id}] TERRAIN AGENT CHAT ENDPOINT")
        logger.info(
            f"[MSG] [{request_id}] ============================================================"
        )

        session_id = body.session_id or str(uuid.uuid4())
        message = body.message
        latitude = body.latitude
        longitude = body.longitude
        screenshot = body.screenshot
        radius_km = body.radius_km

        logger.info(f"[MSG] [{request_id}] Session: {session_id}")
        logger.info(f"[MSG] [{request_id}] Message: {message[:100]}...")
        logger.info(f"[MSG] [{request_id}] Location: ({latitude}, {longitude})")
        logger.info(
            f"[MSG] [{request_id}] Screenshot: {'Yes (' + str(len(screenshot)) + ' chars)' if screenshot else 'No'}"
        )
        if screenshot:
            logger.info(
                f"[MSG] [{request_id}] Screenshot starts with: {screenshot[:60]}..."
            )

        # Try Agent Service first, fall back to direct tools if PE-blocked
        import time

        start_time = time.time()
        result = None

        try:
            from geoint.terrain_agent import get_terrain_agent

            agent = get_terrain_agent()
            result = await agent.chat(
                session_id=session_id,
                user_message=message,
                latitude=latitude,
                longitude=longitude,
                screenshot_base64=screenshot,
                radius_km=radius_km,
            )
            resp_text = str(result.get("response", ""))
            if (
                "403" in resp_text
                or "Public access is disabled" in resp_text
                or "private endpoint" in resp_text.lower()
            ):
                logger.warning(
                    f"[MSG] [{request_id}] Agent returned PE error, falling back to direct tools"
                )
                result = None
        except Exception as agent_err:
            logger.warning(
                f"[MSG] [{request_id}] Agent failed ({agent_err}), falling back to direct tools"
            )
            result = None

        if result is None:
            logger.info(
                f"[MSG] [{request_id}] Using direct terrain tool fallback (PE lockdown)"
            )
            from geoint.terrain_tools import (
                analyze_flood_risk,
                find_flat_areas,
                get_elevation_analysis,
                get_slope_analysis,
            )

            tool_results = {}
            for name, fn in [
                ("elevation", get_elevation_analysis),
                ("slope", get_slope_analysis),
                ("flat_areas", find_flat_areas),
                ("flood_risk", analyze_flood_risk),
            ]:
                try:
                    tool_results[name] = fn(latitude, longitude, radius_km)
                except Exception:
                    pass

            tool_summary = "\n\n".join(
                [
                    f"### {k.replace('_', ' ').title()}\n{v}"
                    for k, v in tool_results.items()
                    if v
                ]
            )

            # Synthesize with _terrain_client
            synthesis_prompt = f"User question: {message}\n\nTerrain data for ({latitude:.4f}, {longitude:.4f}):\n{tool_summary}"
            try:
                from semantic_translator import get_llm_client

                _terrain_client = get_llm_client(model=os.getenv("LLM_MODEL", "gpt-5"))
                synth_resp = await _terrain_client.chat.completions.create(
                    model=os.getenv("LLM_MODEL", "gpt-5"),
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a terrain analysis expert. Synthesize the provided DEM/terrain tool data into a clear, comprehensive answer to the user's question.",
                        },
                        {"role": "user", "content": synthesis_prompt},
                    ],
                    temperature=1.0,
                    max_completion_tokens=2000,
                )
                response_text = synth_resp.choices[0].message.content
            except Exception:
                response_text = (
                    tool_summary or "Terrain analysis completed with limited data."
                )

            result = {
                "response": response_text,
                "tool_calls": list(tool_results.keys()),
                "session_id": session_id,
                "message_count": 1,
            }

        elapsed = time.time() - start_time
        logger.info(f"[MSG] [{request_id}] [OK] Agent responded in {elapsed:.2f}s")
        logger.info(
            f"[MSG] [{request_id}] Tool calls: {len(result.get('tool_calls', []))}"
        )

        return {
            "status": "success",
            **result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MSG] [{request_id}] [FAIL] Chat endpoint failed: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Terrain chat failed")


@router.get("/api/geoint/terrain/chat/{session_id}/history")
async def get_terrain_chat_history(session_id: str):
    """Get conversation history for a terrain agent session."""
    try:
        from geoint.terrain_agent import get_terrain_agent

        agent = get_terrain_agent()
        history = await agent.get_session_history(session_id)

        return {
            "status": "success",
            "session_id": session_id,
            "history": history,
            "message_count": len(history),
        }
    except Exception as e:
        logger.error(f"Terrain chat history error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/geoint/terrain/chat/{session_id}")
async def clear_terrain_chat_session(session_id: str):
    """Clear a terrain agent session (reset memory)."""
    try:
        from geoint.terrain_agent import get_terrain_agent

        agent = get_terrain_agent()
        cleared = await agent.clear_session(session_id)

        return {
            "status": "success" if cleared else "not_found",
            "session_id": session_id,
            "cleared": cleared,
        }
    except Exception as e:
        logger.error(f"Terrain chat clear error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
