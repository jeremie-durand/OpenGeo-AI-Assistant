"""TileSelector date handling for items with and without properties.datetime."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from tile_selector import TileSelector


def _modis_tile(item_id: str, start: str) -> Dict[str, Any]:
    """MODIS items carry a null datetime and a start/end range."""
    return {
        "id": item_id,
        "bbox": [-80.0, 40.0, -65.0, 50.0],
        "properties": {"datetime": None, "start_datetime": start},
    }


def _tile(item_id: str, when: Optional[str]) -> Dict[str, Any]:
    return {"id": item_id, "bbox": [0, 0, 1, 1], "properties": {"datetime": when}}


def test_modis_tiles_are_grouped_by_start_datetime() -> None:
    groups = TileSelector._group_tiles_by_acquisition_date(
        [
            _modis_tile("sep", "2026-09-06T00:00:00Z"),
            _modis_tile("aug", "2026-08-21T00:00:00Z"),
        ]
    )
    assert {date: [t["id"] for t in tiles] for date, tiles in groups.items()} == {
        "2026-09-06": ["sep"],
        "2026-08-21": ["aug"],
    }


def test_datetime_wins_over_start_datetime() -> None:
    tile = _tile("s2", "2026-10-01T10:00:00Z")
    tile["properties"]["start_datetime"] = "2026-01-01T00:00:00Z"
    assert list(TileSelector._group_tiles_by_acquisition_date([tile])) == ["2026-10-01"]


def test_tiles_without_any_date_are_unknown() -> None:
    groups = TileSelector._group_tiles_by_acquisition_date([_tile("x", None)])
    assert list(groups) == ["unknown"]


def _recency(start: datetime) -> float:
    tile = _modis_tile("t", start.isoformat())
    return TileSelector._score_tile(tile)["recency"]


def test_modis_recency_uses_start_datetime() -> None:
    now = datetime.now(timezone.utc)
    assert _recency(now - timedelta(days=3)) > _recency(now - timedelta(days=400))


# Old enough that both dates hit the recency floor, so their scores always tie.
MIXED_DATES = [
    _modis_tile("aug", "2015-08-21T00:00:00Z"),
    _modis_tile("sep", "2015-09-06T00:00:00Z"),
]
SMALL_VIEW = [-72.0, 45.3, -71.8, 45.5]


def test_small_view_keeps_only_the_newest_modis_date() -> None:
    selected = TileSelector.select_best_tiles(MIXED_DATES, query_bbox=SMALL_VIEW)
    assert [t["id"] for t in selected] == ["sep"]


def test_large_view_keeps_modis_tiles_of_every_date() -> None:
    large_view = [-80.0, 40.0, -65.0, 50.0]
    selected = TileSelector.select_best_tiles(MIXED_DATES, query_bbox=large_view)
    assert sorted(t["id"] for t in selected) == ["aug", "sep"]


def test_small_view_keeps_the_newest_period_of_any_range_collection() -> None:
    annual = [
        {**_modis_tile(year, f"{year}-01-01T00:00:00Z"), "collection": "io-lulc"}
        for year in ("2016", "2017")
    ]
    selected = TileSelector.select_best_tiles(annual, query_bbox=SMALL_VIEW)
    assert [t["id"] for t in selected] == ["2017"]
