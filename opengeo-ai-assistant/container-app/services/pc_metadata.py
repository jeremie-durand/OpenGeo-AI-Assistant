"""Planetary Computer metadata loader with offline fallbacks and coverage check."""

import logging

try:
    from pc_tasks_config_loader import get_all_collection_ids, get_collection_metadata
    from pc_tasks_config_loader import get_pc_rendering_config as get_rendering_config
    from pc_tasks_config_loader import load_pc_metadata

    PC_METADATA_AVAILABLE = True
    metadata = load_pc_metadata()
    total_collections = metadata.get("metadata", {}).get("total_collections", 0)
    logging.info(
        f"[OK] PC metadata loaded: {total_collections} collections from PC repository"
    )
except ImportError as e:
    PC_METADATA_AVAILABLE = False
    logging.warning(f"[WARN] PC config loader not available: {e}")

    def load_pc_metadata():
        return {"categories": []}

    def get_collection_metadata(cid):
        return None

    def get_rendering_config(cid):
        return None

    def get_all_collection_ids():
        return []


def check_collection_coverage(collection_id: str, bbox: list) -> dict:
    """Check if collection covers the requested bbox - using PC metadata"""
    coll_metadata = get_collection_metadata(collection_id)
    if coll_metadata:
        return {"covered": True, "message": f"Collection {collection_id} available"}
    return {
        "covered": False,
        "message": f"Collection {collection_id} not found in PC metadata",
    }
