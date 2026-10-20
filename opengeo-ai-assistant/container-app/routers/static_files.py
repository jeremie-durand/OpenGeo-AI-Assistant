"""Root and static JSON metadata endpoints consumed by the frontend."""

import logging
import os

from fastapi import APIRouter
from services.pc_metadata import PC_METADATA_AVAILABLE, load_pc_metadata

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
async def root() -> dict:
    """API root — frontend is served by Nginx."""
    return {
        "message": "OpenGeo AI Assistant API is running",
        "status": "ok",
        "version": "1.0.0",
    }


@router.get("/pc_collections_metadata.json")
async def serve_pc_collections_metadata():
    """Serve PC collections metadata JSON (from unified config)"""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    metadata = load_pc_metadata()

    if metadata and metadata.get("categories"):
        return JSONResponse(content=metadata)

    raise HTTPException(status_code=404, detail="Collections metadata not found")


@router.get("/stac_collections.json")
async def serve_stac_collections():
    """Serve STAC collections JSON for frontend dropdown, generated from pc_rendering_config.json"""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    if not PC_METADATA_AVAILABLE:
        raise HTTPException(status_code=503, detail="Collections metadata not loaded")

    pc_metadata = load_pc_metadata()
    categories = pc_metadata.get("categories", [])

    frontend_collections = []
    for category in categories:
        for collection in category.get("collections", []):
            frontend_collections.append(
                {
                    "collection_id": collection.get("id"),
                    "description": collection.get(
                        "description", "No description available"
                    ),
                    "category": category.get("name", "Other"),
                }
            )

    frontend_collections.sort(key=lambda x: (x["category"], x["collection_id"]))
    return JSONResponse(content=frontend_collections)


@router.get("/pc_rendering_config.json")
async def serve_pc_rendering_config():
    """Serve unified PC rendering config JSON (contains descriptions AND rendering params)"""
    from fastapi.responses import FileResponse

    # Look in the app directory (parent of routers/)
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = os.path.join(app_dir, "pc_rendering_config.json")
    logger.info(f"[SEARCH] Looking for pc_rendering_config.json at: {json_path}")
    logger.info(f"[DIR] File exists: {os.path.exists(json_path)}")
    if os.path.exists(json_path):
        logger.info(f"[OK] Serving pc_rendering_config.json from {json_path}")
        return FileResponse(json_path, media_type="application/json")
    from fastapi import HTTPException

    logger.error(f"[FAIL] PC rendering config not found at {json_path}")
    raise HTTPException(status_code=404, detail="PC rendering config not found")
