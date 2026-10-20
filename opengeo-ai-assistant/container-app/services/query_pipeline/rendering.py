"""Map layers for the results: optical mosaic plus per-item tile URLs."""

import logging
from typing import Any, Dict, List

import aiohttp
from services.collection_detection import detect_collections
from services.pipeline_trace import log_pipeline_step
from services.query_pipeline.state import QueryState
from services.query_pipeline.tiles import (
    has_numeric_bbox,
    needs_optical_mosaic,
    optimize_tilejson_url,
    union_of_bboxes,
)
from services.stac_search import execute_direct_stac_search
from services.tile_urls import build_tile_url_params

logger = logging.getLogger(__name__)


async def register_optical_mosaic(state: QueryState, collection_id: str) -> None:
    """Register a mosaic for optical collections (Sentinel-2, Landsat, HLS).

    Without it, zooming between granules shows missing tiles (404s).
    """
    needs_mosaic = needs_optical_mosaic(collection_id)

    if needs_mosaic and state.stac_query and state.stac_query.get("bbox"):
        logger.info(
            f"[GLOBE] MOSAIC: Enabling mosaic for optical collection {collection_id}"
        )
        try:
            # Import the mosaic function
            from hybrid_rendering_system import get_mosaic_tilejson_url

            state.mosaic_result = await get_mosaic_tilejson_url(
                collections=[collection_id],
                bbox=state.stac_query.get("bbox"),
                datetime_range=state.stac_query.get("datetime"),
                query_filters=(
                    {"eo:cloud_cover": {"lt": 30}}
                    if "cloud" not in state.natural_query.lower()
                    else None
                ),
                query_context=state.natural_query,
            )

            if state.mosaic_result:
                logger.info(
                    f"[OK] MOSAIC: Successfully registered mosaic for {collection_id}"
                )
            else:
                logger.warning(
                    f"[WARN] MOSAIC: Registration failed for {collection_id}, will use item tiles"
                )
        except Exception as e:
            logger.warning(f"[WARN] MOSAIC: Error registering mosaic: {e}")
            state.mosaic_result = None
    else:
        state.mosaic_result = None
        if not needs_mosaic:
            logger.info(
                f"[MAP] MOSAIC: Skipping mosaic for {collection_id} (not an optical collection)"
            )


def summarize_tile_coverage(
    state: QueryState,
    top_features: List[Dict[str, Any]],
    all_bboxes: List[List[float]],
) -> None:
    """Trace the tile URLs and widen stac_params bbox to their union."""
    # Calculate skipped features (features with invalid bbox)
    skipped_count = len(top_features) - len(state.all_tile_urls)

    if state.all_tile_urls:
        logger.info(
            f"[MAP] Generated {len(state.all_tile_urls)} tile URLs for bbox coverage (skipped {skipped_count} with invalid bbox)"
        )

        # [SEARCH] PIPELINE LOG: Tile URLs Output
        log_pipeline_step(
            state.pipeline_session_id,
            "TILE_URLS",
            "OUTPUT",
            {
                "tile_count": len(state.all_tile_urls),
                "skipped_invalid_bbox": skipped_count,
                "tile_ids": [
                    t.get("item_id") for t in state.all_tile_urls[:5]
                ],  # First 5 for brevity
                "has_more": len(state.all_tile_urls) > 5,
            },
        )

        # [OK] Calculate union bbox covering ALL tiles
        if all_bboxes:
            union_bbox = union_of_bboxes(all_bboxes)
            logger.info(f"[MAP] Union bbox calculated: {union_bbox}")
            logger.info(
                f"   Coverage: {union_bbox[2] - union_bbox[0]:.2f}° × {union_bbox[3] - union_bbox[1]:.2f}°"
            )

            # [SEARCH] PIPELINE LOG: Union BBox
            log_pipeline_step(
                state.pipeline_session_id,
                "UNION_BBOX",
                "OUTPUT",
                {
                    "bbox": union_bbox,
                    "width_deg": round(union_bbox[2] - union_bbox[0], 2),
                    "height_deg": round(union_bbox[3] - union_bbox[1], 2),
                },
            )

            # [OK] Update stac_params with union bbox for response metadata
            if state.stac_params:
                state.stac_params["bbox"] = union_bbox
                logger.info(f"[OK] Updated stac_params bbox to union: {union_bbox}")


def build_item_tile_urls(
    state: QueryState, collections: List[str], collection_id: str
) -> None:
    """Build optimized tilejson URLs for up to 20 features.

    Expression-based SAR collections keep the STAC tilejson URL unchanged.
    """
    all_bboxes: List[List[float]] = []
    if state.features and len(state.features) > 0:
        # Process up to 20 features for full bbox coverage
        top_features = state.features[:20]
        logger.info(
            f"[MAP] Generating tile URLs for {len(top_features)} features (of {len(state.features)} total) for {collection_id}"
        )

        for feature in top_features:
            tilejson_asset = feature.get("assets", {}).get("tilejson", {})
            if tilejson_asset and "href" in tilejson_asset:
                feature_bbox = feature.get("bbox")

                if not feature_bbox or len(feature_bbox) != 4:
                    logger.warning(
                        f"[WARN] Skipping feature {feature.get('id')} - invalid bbox: {feature_bbox}"
                    )
                    continue

                if any(
                    v is None or not isinstance(v, (int, float)) for v in feature_bbox
                ):
                    logger.warning(
                        f"[WARN] Skipping feature {feature.get('id')} - bbox contains null values: {feature_bbox}"
                    )
                    continue

                feat_collection_id = feature.get(
                    "collection", collections[0] if collections else "unknown"
                )

                # [ART] Apply quality optimization to tilejson URL
                original_url = tilejson_asset["href"]

                # [SHIELD] EXPRESSION-BASED COLLECTIONS: Keep original STAC tilejson URL
                # Collections like alos-palsar-mosaic use expressions (HH;HV;HH/HV) with multiple
                # rescale values. The STAC tilejson already has the correct rendering params.
                # Modifying the URL would strip the multi-rescale values and break rendering.
                expression_collections = [
                    "alos-palsar-mosaic",
                    "sentinel-1-grd",
                    "sentinel-1-rtc",
                ]
                if (
                    feat_collection_id in expression_collections
                    and "expression=" in original_url
                ):
                    logger.info(
                        f"[SHIELD] Keeping original STAC tilejson for expression-based collection {feat_collection_id}"
                    )
                    state.all_tile_urls.append(
                        {
                            "item_id": feature.get("id"),
                            "bbox": feature_bbox,
                            "tilejson_url": original_url,
                        }
                    )
                    if feature_bbox and len(feature_bbox) == 4:
                        all_bboxes.append(feature_bbox)
                    continue

                # Build quality parameters for this collection
                quality_params = build_tile_url_params(
                    feat_collection_id, state.natural_query
                )

                # Enhance the URL with quality parameters
                if quality_params:
                    optimized_url = optimize_tilejson_url(original_url, quality_params)

                    logger.info(
                        f"[ART] Optimized tile URL for {feature.get('id')}: {quality_params}"
                    )
                else:
                    optimized_url = original_url
                    logger.info(
                        f"[WARN] No quality params generated for {feature.get('id')}"
                    )

                state.all_tile_urls.append(
                    {
                        "item_id": feature.get("id"),
                        "bbox": feature_bbox,
                        "tilejson_url": optimized_url,  # [+] Use optimized URL
                    }
                )

                # [OK] Collect bbox for union calculation
                if feature_bbox and len(feature_bbox) == 4:
                    all_bboxes.append(feature_bbox)

        summarize_tile_coverage(state, top_features, all_bboxes)


async def retry_untiled_features_on_pc(
    state: QueryState, collections: List[str]
) -> None:
    """Re-run the search on Planetary Computer when local items lack tilejson."""
    if (
        not state.all_tile_urls
        and not state.mosaic_result
        and state.features
        and state.stac_endpoint != "planetary_computer"
    ):
        logger.info(
            "[SIGNAL] Local STAC features have no tilejson assets — retrying against Planetary Computer"
        )
        pc_collections = detect_collections(state.natural_query)
        pc_stac_query = {**state.stac_query, "collections": pc_collections}
        pc_stac_response = await execute_direct_stac_search(
            pc_stac_query, "planetary_computer", original_query=state.natural_query
        )
        if pc_stac_response.get("success"):
            pc_features = pc_stac_response.get("results", {}).get("features", [])
            if pc_features:
                logger.info(
                    f"[OK] PC fallback returned {len(pc_features)} renderable features"
                )
                state.features = pc_features
                state.stac_response = pc_stac_response
                # Re-run tile URL generation for PC features
                state.all_tile_urls = []
                collection_id = pc_features[0].get(
                    "collection", collections[0] if collections else ""
                )
                for feature in pc_features[:20]:
                    tilejson_asset = feature.get("assets", {}).get("tilejson", {})
                    if tilejson_asset and "href" in tilejson_asset:
                        feature_bbox = feature.get("bbox")
                        if not has_numeric_bbox(feature_bbox):
                            continue
                        feat_collection_id = feature.get("collection", collection_id)
                        optimized_url = optimize_tilejson_url(
                            tilejson_asset["href"],
                            build_tile_url_params(
                                feat_collection_id, state.natural_query
                            ),
                        )
                        state.all_tile_urls.append(
                            {
                                "item_id": feature.get("id"),
                                "bbox": feature_bbox,
                                "tilejson_url": optimized_url,
                            }
                        )
                # Re-register mosaic for PC features
                if (
                    not state.mosaic_result
                    and state.stac_query
                    and state.stac_query.get("bbox")
                ):
                    pc_collection_id = (
                        pc_features[0].get("collection", "") if pc_features else ""
                    )
                    if needs_optical_mosaic(pc_collection_id):
                        try:
                            from hybrid_rendering_system import (
                                get_mosaic_tilejson_url,
                            )

                            state.mosaic_result = await get_mosaic_tilejson_url(
                                collections=[pc_collection_id],
                                bbox=state.stac_query.get("bbox"),
                                datetime_range=state.stac_query.get("datetime"),
                                query_filters=(
                                    {"eo:cloud_cover": {"lt": 30}}
                                    if "cloud" not in state.natural_query.lower()
                                    else None
                                ),
                                query_context=state.natural_query,
                            )
                            if state.mosaic_result:
                                logger.info(
                                    f"[OK] MOSAIC: PC fallback mosaic registered for {pc_collection_id}"
                                )
                        except aiohttp.ClientError as e:
                            logger.warning(
                                f"[WARN] MOSAIC: PC fallback mosaic registration network error: {e}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"[WARN] MOSAIC: PC fallback mosaic registration failed: {e}"
                            )


async def render_tiles(state: QueryState) -> None:
    """Produce map layers: an optical mosaic plus item tile URLs.

    Multi-tile rendering suits DEM, MODIS composites and global collections;
    optical imagery (Sentinel-2, HLS, Landsat) uses the mosaic and best tiles.
    """
    state.all_tile_urls = []
    state.mosaic_result = None
    collections = state.stac_query.get("collections", []) if state.stac_query else []
    collection_id = collections[0].lower() if collections else ""

    # [SEARCH] DEBUG: Log what collections are in the features vs what was requested
    if state.features:
        feature_collections = list(
            set(f.get("collection", "unknown") for f in state.features)
        )
        logger.info(
            f"[SEARCH] COLLECTION DEBUG: Requested={collections}, Features contain={feature_collections}"
        )
        if set(feature_collections) != set(collections):
            logger.warning(
                "[WARN] MISMATCH: STAC returned different collections than requested!"
            )

    await register_optical_mosaic(state, collection_id)

    # [MAP] TILE RENDERING: Generate individual tile URLs as fallback or supplement
    # The STAC query already returns features that cover the query bbox
    # We generate tile URLs for all features (up to 20) to ensure full area coverage
    # [ART] OPTIMIZATION: Generate optimized URLs for all features
    build_item_tile_urls(state, collections, collection_id)

    # If local STAC returned features but none had tilejson assets (i.e. they are
    # raw local STAC items without PC-style tile endpoints), retry against PC so
    # the user still gets renderable imagery.
    await retry_untiled_features_on_pc(state, collections)
