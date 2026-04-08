"""Pydantic model instantiation and validation smoke tests."""

import pytest
from request_models import (
    AnimationRequest,
    ComparisonRequest,
    GeointRequest,
    OrchestrateRequest,
    QueryRequest,
    SessionResetRequest,
    SignMosaicUrlRequest,
    VisionRequest,
)


def test_query_request_minimal() -> None:
    assert QueryRequest(query="show me Paris").query == "show me Paris"


def test_geoint_request_defaults() -> None:
    m = GeointRequest(latitude=48.8, longitude=2.3)
    assert m.radius_miles == 5.0


def test_geoint_latitude_out_of_range() -> None:
    with pytest.raises(Exception):
        GeointRequest(latitude=91.0, longitude=0.0)


def test_geoint_longitude_out_of_range() -> None:
    with pytest.raises(Exception):
        GeointRequest(latitude=0.0, longitude=181.0)


def test_session_reset_conversation_id_alias() -> None:
    m = SessionResetRequest(conversation_id="abc")
    assert m.session_id == "abc"


def test_comparison_request_user_context_alias() -> None:
    m = ComparisonRequest(user_context="show changes")
    assert m.user_query == "show changes"


def test_animation_request_defaults() -> None:
    m = AnimationRequest(
        latitude=37.0,
        longitude=-120.0,
        start_date="2023-01-01",
        end_date="2023-06-01",
    )
    assert m.collection_id == "sentinel-2-l2a"


def test_orchestrate_request_default_modules() -> None:
    m = OrchestrateRequest(latitude=40.0, longitude=-74.0)
    assert "terrain" in m.modules


def test_sign_mosaic_url_request() -> None:
    m = SignMosaicUrlRequest(url="https://example.com/mosaic")
    assert m.url == "https://example.com/mosaic"


def test_vision_request_empty_tile_urls() -> None:
    assert VisionRequest(latitude=34.0, longitude=-118.0).tile_urls == []
