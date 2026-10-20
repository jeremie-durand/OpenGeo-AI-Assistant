"""Final /api/query response assembly and session bookkeeping."""

from typing import Any, Dict, List


def summarize_stac_items(
    features: List[Dict[str, Any]], include_cloud_cover_alias: bool
) -> List[Dict[str, Any]]:
    """Keep what the vision agent needs (incl. asset href/type) of 10 items."""
    items = []
    for f in features[:10]:
        properties = {
            "datetime": f.get("properties", {}).get("datetime"),
            "eo:cloud_cover": f.get("properties", {}).get("eo:cloud_cover"),
        }
        if include_cloud_cover_alias:
            properties["cloud_cover"] = f.get("properties", {}).get("cloud_cover")
        items.append(
            {
                "id": f.get("id"),
                "collection": f.get("collection"),
                "bbox": f.get("bbox"),
                "properties": properties,
                "assets": {
                    k: {"href": v.get("href"), "type": v.get("type")}
                    for k, v in (f.get("assets") or {}).items()
                },
            }
        )
    return items
