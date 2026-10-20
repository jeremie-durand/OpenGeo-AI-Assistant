"""User-facing response text for empty STAC results and keyword fallbacks."""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def generate_contextual_empty_response(
    query: str, collections: List[str], diagnostics: Dict[str, Any]
) -> str:
    """
    Generate helpful response when no results found, with context-aware suggestions
    based on where the filtering pipeline failed.

    Args:
        query: Original user query
        collections: Collections searched
        diagnostics: {
            "raw_count": int,              # Results from STAC API
            "spatial_filtered_count": int, # After spatial overlap filter
            "final_count": int,            # After tile selection
            "stac_query": dict,            # Full STAC query with filters
            "failure_stage": str           # Where it failed
        }
    """
    raw_count = diagnostics.get("raw_count", 0)
    spatial_count = diagnostics.get("spatial_filtered_count", 0)
    final_count = diagnostics.get("final_count", 0)
    stac_query = diagnostics.get("stac_query", {})

    response_parts = []

    # SCENARIO 1: STAC API returned nothing (most common)
    if raw_count == 0:
        response_parts.append(
            f"I searched for satellite data related to '{query}', but didn't find any available imagery matching your criteria."
        )

        # Analyze STAC query filters to provide specific suggestions
        suggestions = []

        # Check datetime constraints
        datetime_str = stac_query.get("datetime", "")
        if datetime_str and "/" in datetime_str:
            start, end = datetime_str.split("/")
            start_date = start[:10] if len(start) >= 10 else start
            end_date = end[:10] if len(end) >= 10 else end

            # Calculate date range in days
            try:
                from datetime import datetime as dt

                start_dt = dt.fromisoformat(start_date)
                end_dt = dt.fromisoformat(end_date)
                days = (end_dt - start_dt).days

                if days < 90:
                    suggestions.append(
                        f"**Expand the date range**: Currently searching {days} days ({start_date} to {end_date}). Try expanding to 6 months or 1 year for more results."
                    )
            except (ValueError, TypeError) as e:
                logger.warning("Failed to parse date range for suggestion: %s", e)
            except Exception as e:
                logger.warning(
                    "Unexpected error parsing date range for suggestion: %s", e
                )

        # Check cloud cover constraints
        cloud_filter = stac_query.get("filter", {})
        if "eo:cloud_cover" in str(cloud_filter):
            cloud_threshold = None
            # Try to extract threshold from filter
            if isinstance(cloud_filter, dict):
                try:
                    # Handle different filter structures
                    filter_str = str(cloud_filter)
                    if "lt" in filter_str and "10" in filter_str:
                        cloud_threshold = 10
                    elif "lt" in filter_str and "20" in filter_str:
                        cloud_threshold = 20
                except Exception as e:
                    logger.warning(
                        "Failed to extract cloud cover threshold from filter: %s", e
                    )

            if cloud_threshold:
                suggestions.append(
                    f"**Relax cloud cover filter**: Currently requiring <{cloud_threshold}% clouds. Try allowing up to {cloud_threshold + 20}% for more options."
                )

        # Check collections
        if collections:
            collection_names = ", ".join(collections)
            suggestions.append(
                f"**Verify data availability**: Searched {collection_names} - confirm this data exists for your location and time period."
            )

        # Check location (if we can detect potential issues)
        query_lower = query.lower()
        if any(
            term in query_lower
            for term in ["ocean", "sea", "water", "pacific", "atlantic"]
        ):
            suggestions.append(
                "**Check location**: Some collections (like NAIP) only cover land areas, not oceans."
            )

        # Add suggestions
        if suggestions:
            response_parts.append("\n**Suggestions to find results:**")
            for suggestion in suggestions:
                response_parts.append(f"- {suggestion}")
        else:
            response_parts.append(
                "\n**Try:** adjusting your search area, expanding the time range, or checking if the location name is spelled correctly."
            )

    # SCENARIO 2: STAC returned results, but spatial filter removed them
    elif raw_count > 0 and spatial_count == 0:
        response_parts.append(
            f"I found {raw_count} satellite images in the catalog, but none had sufficient coverage of your requested area."
        )
        response_parts.append("\n**This usually means:**")
        response_parts.append(
            "- The imagery tiles only partially overlap your location (less than 10% coverage)"
        )
        response_parts.append(
            "- Your search area might be at the edge of the satellite's coverage zone"
        )
        response_parts.append(
            "\n**Try:** expanding your search area or choosing a nearby location with better coverage."
        )

    # SCENARIO 3: Spatial filter passed, but tile selection removed everything (unlikely)
    elif spatial_count > 0 and final_count == 0:
        response_parts.append(
            f"I found {spatial_count} satellite images covering your area, but they didn't meet quality thresholds."
        )
        response_parts.append(
            "\n**Try:** relaxing quality filters or accepting imagery with higher cloud cover."
        )

    # SCENARIO 4: Unknown failure
    else:
        response_parts.append(
            f"I couldn't find satellite data matching your query for '{query}'."
        )
        response_parts.append(
            "\n**Try:** adjusting your search parameters or rephrasing your query."
        )

    return " ".join(response_parts)


def generate_fallback_response(
    query: str, features: List[Dict], collections: List[str]
) -> str:
    """
    Enhanced fallback response generation from Router Function App
    """
    try:
        feature_count = len(features)

        # Build response components
        response_parts = []

        # Opening with feature count
        if feature_count == 0:
            response_parts.append(
                f"I searched for satellite data related to '{query}', but didn't find any available imagery for the specified area and time period."
            )
            response_parts.append(
                "Try adjusting your search area or time range, or check if the location name is spelled correctly."
            )
        elif feature_count == 1:
            response_parts.append(
                f"Found 1 satellite image matching your query for '{query}'."
            )
        else:
            response_parts.append(
                f"Found {feature_count} satellite images matching your query for '{query}'."
            )

        # Add temporal context if we have features
        if feature_count > 0:
            try:
                dates = []
                for feature in features:
                    properties = feature.get("properties", {})
                    if "datetime" in properties:
                        dates.append(properties["datetime"])
                    elif "start_datetime" in properties:
                        dates.append(properties["start_datetime"])

                if dates:
                    dates.sort()
                    earliest = dates[0][:10] if dates else "unknown"
                    latest = dates[-1][:10] if len(dates) > 1 else earliest

                    if earliest == latest:
                        response_parts.append(f"The imagery is from {earliest}.")
                    else:
                        response_parts.append(
                            f"The imagery spans from {earliest} to {latest}."
                        )
            except Exception as e:
                logger.warning(f"Could not extract temporal info: {e}")

        # Collection-specific insights
        if collections and feature_count > 0:
            # Map collections to friendly names (matching Router Function App)
            collection_descriptions = {
                "modis-14A1-061": "MODIS fire detection (daily global coverage)",
                "modis-14A2-061": "MODIS fire detection (8-day composite)",
                "modis-MCD64A1-061": "MODIS burned area mapping",
                "cop-dem-glo-30": "Copernicus 30m elevation data",
                "cop-dem-glo-90": "Copernicus 90m elevation data",
                "sentinel-2-l2a": "Sentinel-2 (high-resolution 10m optical)",
                "landsat-c2-l2": "Landsat (30m optical with thermal)",
                "modis-09A1-061": "MODIS (daily global coverage)",
                "sentinel-1-grd": "Sentinel-1 SAR (radar, all-weather)",
            }
            described_collections = [
                collection_descriptions.get(c, c) for c in collections
            ]

            if len(described_collections) == 1:
                response_parts.append(
                    f"The data comes from {described_collections[0]} satellite."
                )
            else:
                response_parts.append(
                    f"The data includes imagery from: {', '.join(described_collections)}."
                )

        # Query-specific context with more detail
        query_lower = query.lower()
        if any(
            term in query_lower
            for term in ["elevation", "dem", "topography", "terrain"]
        ):
            response_parts.append(
                "This elevation data can be used for terrain analysis, slope calculations, watershed mapping, and 3D visualization."
            )
        elif any(
            term in query_lower
            for term in ["optical", "rgb", "satellite", "imagery", "urban", "city"]
        ):
            response_parts.append(
                "This optical imagery is perfect for visual analysis, urban planning, change detection, and land cover mapping. You can see buildings, roads, vegetation, and water features clearly."
            )
        elif any(term in query_lower for term in ["radar", "sar"]):
            response_parts.append(
                "This radar data can penetrate clouds and is excellent for all-weather monitoring, including surface changes and ground deformation."
            )
        elif any(term in query_lower for term in ["fire", "wildfire", "burn"]):
            response_parts.append(
                "This thermal and optical data is ideal for fire monitoring, burn area assessment, and recovery tracking."
            )
        else:
            response_parts.append(
                "This satellite data provides comprehensive coverage suitable for environmental monitoring and analysis."
            )

        response_parts.append(
            "All data is ready for analysis and has been loaded on the map for interactive exploration."
        )

        final_response = " ".join(response_parts)
        logger.info(
            f"Generated fallback response ({len(final_response)} chars): {final_response[:100]}..."
        )

        return final_response

    except (KeyError, AttributeError, TypeError) as e:
        logger.error(f"Data error in fallback response generation: {e}")
        return f"Found {len(features)} satellite images for '{query}'. The imagery shows excellent coverage of the requested area and is ready for analysis on the map."
    except Exception as e:
        logger.error(f"Error in fallback response generation: {e}")
        return f"Found {len(features)} satellite images for '{query}'. The imagery shows excellent coverage of the requested area and is ready for analysis on the map."
