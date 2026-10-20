"""Pure helpers shared by the /api/query pipeline stages."""

from services.query_pipeline.response import summarize_stac_items
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
