"""Pydantic request models for all POST endpoints in routers/."""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from stac_pydantic.api import Search

# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class GeointRequest(BaseModel):
    """Base for all geoint endpoints that require coordinates."""

    model_config = ConfigDict(populate_by_name=True)

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    screenshot: Optional[str] = Field(None, max_length=5_000_000)
    session_id: Optional[str] = None
    user_query: Optional[str] = None
    radius_miles: float = Field(default=5.0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_user_query(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "user_context" in data
            and not data.get("user_query")
        ):
            data["user_query"] = data.pop("user_context")
        return data


# ---------------------------------------------------------------------------
# Core API endpoints
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    """POST /api/query — main natural-language satellite query."""

    model_config = ConfigDict(populate_by_name=True)

    query: Optional[str] = None
    user_query: Optional[str] = None
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    pin: Optional[Dict[str, Any]] = None
    model: str = "gpt-5"
    has_satellite_data: bool = False
    # Map/vision context sent by the frontend
    map_bounds: Optional[Dict[str, Any]] = None
    imagery_url: Optional[str] = None
    imagery_base64: Optional[str] = Field(None, max_length=5_000_000)
    current_collection: Optional[str] = None
    tile_urls: List[str] = Field(default_factory=list, max_length=100)
    conversation_history: List[Any] = Field(default_factory=list, max_length=100)
    messages: List[Any] = Field(default_factory=list, max_length=100)
    # Dashboard context and "Show on map" flag (issue #2)
    dashboard_context: Optional["DashboardContext"] = None
    search_current_view: bool = False
    current_view_collection: Optional["RenderableCollection"] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Merge query / user_query → query
        if not data.get("query") and data.get("user_query"):
            data["query"] = data["user_query"]
        # Merge session_id / conversation_id → session_id
        if not data.get("session_id") and data.get("conversation_id"):
            data["session_id"] = data["conversation_id"]
        return data


class SignMosaicUrlRequest(BaseModel):
    """POST /api/sign-mosaic-url — sign a Planetary Computer mosaic URL."""

    url: str


class StacSearchRequest(Search):
    """POST /api/stac-search — standard STAC API search against Planetary Computer."""


class VedaSearchRequest(Search):
    """POST /api/veda-search — STAC API search against NASA VEDA endpoint."""


class StructuredSearchRequest(BaseModel):
    """POST /api/structured-search — collection + location + optional datetime."""

    collection: str
    location: str
    datetime: Optional[str] = None
    datetime_start: Optional[str] = None
    datetime_end: Optional[str] = None


class SessionResetRequest(BaseModel):
    """POST /api/session-reset — reset conversation context for a session."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: Optional[str] = None
    conversation_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and not data.get("session_id")
            and data.get("conversation_id")
        ):
            data["session_id"] = data["conversation_id"]
        return data


class ComparisonQueryRequest(BaseModel):
    """POST /api/process-comparison-query — parse a natural-language comparison query."""

    query: str


# ---------------------------------------------------------------------------
# GEOINT endpoints
# ---------------------------------------------------------------------------


class MobilityRequest(GeointRequest):
    """POST /api/geoint/mobility — route/terrain mobility analysis."""

    latitude_b: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude_b: Optional[float] = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _validate_destination(self) -> "MobilityRequest":
        has_b = self.latitude_b is not None or self.longitude_b is not None
        both_b = self.latitude_b is not None and self.longitude_b is not None
        if has_b and not both_b:
            raise ValueError(
                "latitude_b and longitude_b must both be provided together"
            )
        return self


class TerrainRequest(GeointRequest):
    """POST /api/geoint/terrain — terrain analysis (DEM, slope, NDVI)."""


class TerrainChatRequest(BaseModel):
    """POST /api/geoint/terrain/chat — multi-turn terrain agent conversation."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    message: str
    session_id: Optional[str] = None
    screenshot: Optional[str] = Field(None, max_length=5_000_000)
    radius_km: float = Field(default=5.0)


class VisionRequest(GeointRequest):
    """POST /api/geoint/vision — satellite vision analysis."""

    tile_urls: List[str] = Field(default_factory=list, max_length=100)
    collection: Optional[str] = None
    map_bounds: Optional[Dict[str, Any]] = None
    stac_items: List[Dict[str, Any]] = Field(default_factory=list, max_length=100)
    analysis_type: Optional[str] = None


class VisionChatRequest(BaseModel):
    """POST /api/geoint/vision/chat — multi-turn vision agent conversation."""

    session_id: str
    message: str
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    screenshot: Optional[str] = Field(None, max_length=5_000_000)
    tile_urls: List[str] = Field(default_factory=list, max_length=100)
    collection: Optional[str] = None
    stac_items: List[Dict[str, Any]] = Field(default_factory=list, max_length=100)
    analysis_type: Optional[str] = None


class BuildingDamageRequest(GeointRequest):
    """POST /api/geoint/building-damage — building and structural damage assessment."""


class ExtremeWeatherRequest(GeointRequest):
    """POST /api/geoint/extreme-weather — climate and extreme weather analysis."""


class ComparisonRequest(BaseModel):
    """POST /api/geoint/comparison — temporal change detection (query or direct mode)."""

    model_config = ConfigDict(populate_by_name=True)

    user_query: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    before_date: Optional[str] = None
    after_date: Optional[str] = None
    screenshot: Optional[str] = Field(None, max_length=5_000_000)
    session_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_user_query(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "user_context" in data
            and not data.get("user_query")
        ):
            data["user_query"] = data.pop("user_context")
        return data


class AnimationRequest(BaseModel):
    """POST /api/geoint/animation — time-lapse animation generation."""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    start_date: str
    end_date: str
    collection_id: str = "sentinel-2-l2a"
    user_query: Optional[str] = None


class OrchestrateRequest(BaseModel):
    """POST /api/geoint/orchestrate — run multiple GEOINT agents in parallel."""

    model_config = ConfigDict(extra="allow")

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    modules: List[str] = Field(default=["terrain", "mobility"], max_length=10)
    screenshot: Optional[str] = Field(None, max_length=5_000_000)
    user_query: Optional[str] = None
    radius_miles: float = Field(default=5.0)


# ---------------------------------------------------------------------------
# Dashboard endpoints
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: List[float]) -> List[float]:
    """Require [west, south, east, north] within lon/lat ranges, min < max."""
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180):
        raise ValueError("bbox longitudes must satisfy -180 <= west < east <= 180")
    if not (-90 <= south < north <= 90):
        raise ValueError("bbox latitudes must satisfy -90 <= south < north <= 90")
    return bbox


def _validate_renderable(collection: str) -> str:
    """Reject collections the map cannot render."""
    from services.dashboard_catalog import renderable_collection_ids

    if collection not in renderable_collection_ids():
        raise ValueError(f"collection '{collection}' is not renderable")
    return collection


Bbox = Annotated[
    List[Annotated[float, Field(allow_inf_nan=False)]],
    Field(min_length=4, max_length=4),
    AfterValidator(_validate_bbox),
]
RenderableCollection = Annotated[
    str, Field(max_length=100), AfterValidator(_validate_renderable)
]


class DashboardStatsRequest(BaseModel):
    """POST /api/dashboard/stats — availability stats of one collection in a bbox."""

    collection: RenderableCollection
    bbox: Bbox


class DashboardMonth(BaseModel):
    """Scene count of one month in the dashboard context."""

    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    count: int = Field(ge=0)


class DashboardContext(BaseModel):
    """Focused collection and its stats as shown on the dashboard.

    Every field is numeric, a date or a validated id, so no client free text
    reaches the LLM prompt through this context.
    """

    collection: str = Field(max_length=100)
    bbox: Bbox
    scene_count: int = Field(ge=0)
    capped: bool = False
    monthly: List[DashboardMonth] = Field(default_factory=list, max_length=12)
    cloud_cover: Optional[List[Annotated[int, Field(ge=0)]]] = Field(
        default=None, max_length=5
    )
    latest_datetime: Optional[datetime] = None


QueryRequest.model_rebuild()
