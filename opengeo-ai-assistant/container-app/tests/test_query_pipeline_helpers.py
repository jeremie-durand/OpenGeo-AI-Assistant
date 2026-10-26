"""Pure helpers shared by the /api/query pipeline stages."""

from types import SimpleNamespace
from typing import List

from services.query_pipeline.response import summarize_stac_items
from services.query_pipeline.search import filter_modis_tiles
from services.query_pipeline.tiles import (
    has_numeric_bbox,
    needs_optical_mosaic,
    optimize_tilejson_url,
    union_of_bboxes,
)

ITEM = {
    "id": "a",
    "collection": "c",
    "bbox": [0, 0, 1, 1],
    "properties": {"datetime": "d", "eo:cloud_cover": 3, "cloud_cover": 4, "x": 1},
    "assets": {"visual": {"href": "h", "type": "t", "roles": ["data"]}},
    "links": [],
}


def test_optimize_replaces_rendering_params() -> None:
    url = "https://t.test/tj.json?collection=c&item=i&assets=visual&nodata=0"
    assert (
        optimize_tilejson_url(url, "assets=B04&rescale=0,3000")
        == "https://t.test/tj.json?collection=c&item=i&assets=B04&rescale=0,3000"
    )


def test_optimize_url_without_query_string() -> None:
    assert optimize_tilejson_url("https://t.test/tj.json", "a=1") == (
        "https://t.test/tj.json?a=1"
    )


def test_optimize_when_every_param_is_replaced() -> None:
    assert optimize_tilejson_url("https://t.test/tj.json?assets=x", "assets=y") == (
        "https://t.test/tj.json?assets=y"
    )


def test_optimize_without_quality_params_keeps_url() -> None:
    url = "https://t.test/tj.json?assets=x"
    assert optimize_tilejson_url(url, "") == url
    assert optimize_tilejson_url(url, None) == url


def test_needs_optical_mosaic() -> None:
    assert needs_optical_mosaic("sentinel-2-l2a")
    assert needs_optical_mosaic("hls2-s30")
    assert not needs_optical_mosaic("modis-13Q1-061")
    assert not needs_optical_mosaic("cop-dem-glo-30")


def test_has_numeric_bbox() -> None:
    assert has_numeric_bbox([1, 2.5, 3, 4])
    assert not has_numeric_bbox(None)
    assert not has_numeric_bbox([1, 2, 3])
    assert not has_numeric_bbox([1, None, 3, 4])
    assert not has_numeric_bbox([1, "2", 3, 4])


def test_union_of_bboxes() -> None:
    assert union_of_bboxes([[0, 0, 1, 1], [-1, 0.5, 0.5, 2]]) == [-1, 0, 1, 2]


def test_summarize_stac_items_keeps_vision_fields() -> None:
    assert summarize_stac_items([ITEM], include_cloud_cover_alias=True) == [
        {
            "id": "a",
            "collection": "c",
            "bbox": [0, 0, 1, 1],
            "properties": {"datetime": "d", "eo:cloud_cover": 3, "cloud_cover": 4},
            "assets": {"visual": {"href": "h", "type": "t"}},
        }
    ]
    (short,) = summarize_stac_items([ITEM], include_cloud_cover_alias=False)
    assert list(short["properties"]) == ["datetime", "eo:cloud_cover"]


def test_summarize_stac_items_keeps_first_ten() -> None:
    assert len(summarize_stac_items([ITEM] * 12, include_cloud_cover_alias=True)) == 10


SMALL_VIEW = [-72.0, 45.3, -71.8, 45.5]
TILE_AROUND_VIEW = [-93.3, 40.0, -65.3, 50.0]


def _kept_modis_tiles(tile_bboxes: List[List[float]], bbox: List[float]) -> List[str]:
    state = SimpleNamespace(search_diagnostics={})
    tiles = [{"id": str(i), "bbox": b} for i, b in enumerate(tile_bboxes)]
    kept = filter_modis_tiles(state, tiles, bbox, True)
    return [t["id"] for t in kept]


def test_modis_filter_keeps_tile_containing_a_small_view() -> None:
    assert _kept_modis_tiles([TILE_AROUND_VIEW], SMALL_VIEW) == ["0"]


def test_modis_filter_drops_tile_not_containing_the_view_center() -> None:
    assert _kept_modis_tiles([[10, 10, 11, 11]], SMALL_VIEW) == []


def test_modis_filter_keeps_tiles_centered_in_a_large_view() -> None:
    large = [-125.0, 32.0, -114.0, 42.0]
    assert _kept_modis_tiles([[-122, 35, -118, 39], [-112, 30, -100, 40]], large) == [
        "0"
    ]


def test_modis_filter_keeps_only_the_closest_overlapping_tile() -> None:
    west_tile, east_tile = [-90.0, 40.0, -65.0, 50.0], [-75.0, 40.0, -52.0, 50.0]
    assert _kept_modis_tiles([west_tile, east_tile], SMALL_VIEW) == ["0"]


def test_modis_filter_skips_a_view_across_the_antimeridian() -> None:
    fiji = [177.0, -19.0, -179.0, -16.0]
    assert _kept_modis_tiles([[170, -20, 180, -10], [10, 10, 11, 11]], fiji) == [
        "0",
        "1",
    ]
