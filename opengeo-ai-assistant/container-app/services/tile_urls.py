"""TiTiler URL parameter building and tilejson URL clean-up for map rendering."""

import logging
from typing import Any, Dict

from hybrid_rendering_system import HybridRenderingSystem

logger = logging.getLogger(__name__)


def build_tile_url_params(collection_id: str, query_context: str = None) -> str:
    """
    Build optimal TiTiler URL parameters using the Hybrid Rendering System.

    This provides intelligent rendering for 113+ STAC collections through:
    1. Explicit configurations for known collections
    2. Smart pattern matching for collection families
    3. Dynamic STAC metadata inference
    4. Safe fallbacks for unknown collections
    """
    return HybridRenderingSystem.build_titiler_url_params(collection_id, query_context)


def clean_tilejson_urls(stac_results: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build TiTiler tilejson URLs for ALL collections using PC rendering configs.

    ALL collections go through TiTiler API with correct render parameters from
    planetary-computer-tasks repository configs. This includes:
    - Optical imagery (RGB): TiTiler with assets + color_formula from PC configs
    - Colormap data (SAR, DEM, MODIS): TiTiler with colormap_name + rescale from PC configs
    - Single-band data: TiTiler with appropriate single-band params from PC configs

    We ALWAYS build TiTiler URLs from scratch using the official PC configs,
    never relying on STAC API's default tilejson URLs.
    """
    logger.info(
        f"[CLEAN] clean_tilejson_urls() called with stac_results type: {type(stac_results)}"
    )
    logger.info(
        f"[CLEAN] stac_results keys: {list(stac_results.keys()) if isinstance(stac_results, dict) else 'Not a dict'}"
    )

    try:
        features = stac_results.get("features", [])
        if not features:
            return stac_results

        cleaned_features = []
        for i, feature in enumerate(features):
            collection_id = feature.get("collection", "unknown")
            item_id = feature.get("id", "unknown")

            cleaned_feature = feature.copy()
            config = HybridRenderingSystem.get_render_config(collection_id)

            if "assets" in cleaned_feature and cleaned_feature["assets"]:
                cleaned_assets = {}
                has_tilejson = False

                for asset_name, asset_data in cleaned_feature["assets"].items():
                    cleaned_asset = asset_data.copy()

                    if asset_name == "tilejson" and "href" in cleaned_asset:
                        has_tilejson = True
                        original_url = cleaned_asset["href"]

                        # Expression-based collections: keep original STAC tilejson URL
                        expression_collections = [
                            "alos-palsar-mosaic",
                            "sentinel-1-grd",
                            "sentinel-1-rtc",
                        ]
                        if (
                            collection_id in expression_collections
                            and "expression=" in original_url
                        ):
                            pass  # Keep original URL
                        elif config:
                            titiler_url = (
                                HybridRenderingSystem.build_titiler_tilejson_url(
                                    item_id, collection_id
                                )
                            )
                            cleaned_asset["href"] = titiler_url

                    cleaned_assets[asset_name] = cleaned_asset

                # Generate tilejson URL for collections that don't have it in STAC response
                if not has_tilejson and config:
                    titiler_url = HybridRenderingSystem.build_titiler_tilejson_url(
                        item_id, collection_id
                    )
                    cleaned_assets["tilejson"] = {
                        "href": titiler_url,
                        "type": "application/json",
                        "roles": ["tiles"],
                        "title": "TileJSON for visualization (auto-generated)",
                    }
                elif not has_tilejson:
                    logger.warning(
                        f"[CLEAN] No tilejson and no PC config for {collection_id}/{item_id}"
                    )

                cleaned_feature["assets"] = cleaned_assets

            cleaned_features.append(cleaned_feature)

        # Return cleaned results
        cleaned_results = stac_results.copy()
        cleaned_results["features"] = cleaned_features

        collection_name = (
            cleaned_features[0].get("collection") if cleaned_features else "N/A"
        )
        logger.info(
            f"[CLEAN] URL cleaning done — {len(cleaned_features)} features, collection={collection_name}"
        )
        return cleaned_results

    except (ValueError, KeyError) as e:
        logger.error(f"[CLEAN] [FAIL] Data error cleaning tilejson URLs: {e}")
        return stac_results
    except Exception as e:
        logger.error(f"[CLEAN] [FAIL] Error cleaning tilejson URLs: {e}")
        return stac_results


def _enhance_tilejson_url(url: str, collection_id: str) -> str:
    """
    Enhance a tilejson URL with correct rendering parameters from HybridRenderingSystem.

    This function ensures that SAR, MODIS, and other non-optical collections get the
    correct assets, rescale, colormap, and other parameters they need to render properly.

    Args:
        url: Cleaned tilejson URL
        collection_id: STAC collection ID

    Returns:
        Enhanced URL with optimal rendering parameters
    """
    try:
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

        # Get optimal rendering configuration from HybridRenderingSystem
        render_config = HybridRenderingSystem.get_render_config(collection_id)

        # Parse the URL
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # Convert multi-value params to single values (parse_qs returns lists)
        params = {
            k: v[0] if isinstance(v, list) and len(v) == 1 else v
            for k, v in params.items()
        }

        # Inject optimal rendering parameters based on config
        config_dict = render_config.to_dict()

        # Add assets if specified (critical for SAR, MODIS)
        if config_dict.get("assets"):
            assets = config_dict["assets"]
            if isinstance(assets, list):
                # For multi-asset (RGB), set assets parameter
                params["assets"] = ",".join(assets)
                logger.info(f"[ART] Injecting assets: {params['assets']}")
            else:
                # For single asset, set assets parameter
                params["assets"] = assets
                logger.info(f"[ART] Injecting asset: {assets}")

        # Add rescale if specified (critical for SAR and MODIS)
        if config_dict.get("rescale") is not None:
            min_val, max_val = config_dict["rescale"]
            params["rescale"] = f"{min_val},{max_val}"
            logger.info(f"[ART] Injecting rescale: {params['rescale']}")

        # Add colormap for single-band data (SAR, MODIS vegetation, elevation)
        if config_dict.get("colormap_name"):
            params["colormap_name"] = config_dict["colormap_name"]
            logger.info(f"[ART] Injecting colormap: {params['colormap_name']}")

        # Add bidx if specified
        if config_dict.get("bidx"):
            params["bidx"] = str(config_dict["bidx"])
            logger.info(f"[ART] Injecting bidx: {params['bidx']}")

        # Add resampling method
        if config_dict.get("resampling"):
            params["resampling"] = config_dict["resampling"]
            logger.info(f"[ART] Injecting resampling: {params['resampling']}")

        # Add color formula if specified (for Landsat adjustments)
        if config_dict.get("color_formula"):
            # URL encode the formula
            formula = config_dict["color_formula"].replace(" ", "+").replace(",", "%2C")
            params["color_formula"] = formula
            logger.info(
                f"[ART] Injecting color_formula: {config_dict['color_formula']}"
            )

        # Rebuild the URL with enhanced parameters
        # Convert params back to list format for urlencode
        encoded_params = []
        for key, value in params.items():
            if isinstance(value, list):
                for v in value:
                    encoded_params.append((key, v))
            else:
                encoded_params.append((key, value))

        new_query = urlencode(encoded_params)
        enhanced_url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

        return enhanced_url

    except (ValueError, AttributeError) as e:
        logger.error(
            f"[FAIL] URL parse error enhancing tilejson URL for {collection_id}: {e}"
        )
        return url
    except Exception as e:
        logger.error(f"[FAIL] Error enhancing tilejson URL for {collection_id}: {e}")
        logger.exception("Full exception details:")
        return url
