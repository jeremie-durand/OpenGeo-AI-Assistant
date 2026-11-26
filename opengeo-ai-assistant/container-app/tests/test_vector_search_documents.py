"""Collection documents: hashing, extents, antimeridian and datetime filters."""

from datetime import datetime, timezone

import pytest
from services.vector_search import documents as vd

QUEBEC = (-79.8, 45.0, -57.1, 62.6)
TEXAS = (-106.6, 25.8, -93.5, 36.5)
PACIFIC = (170.0, -50.0, -170.0, -30.0)


def utc(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def doc(**overrides: object) -> vd.CollectionDocument:
    values = {"id": "mtbs", "title": "MTBS", "description": "Burn severity"}
    values.update(overrides)
    return vd.CollectionDocument(**values)  # type: ignore[arg-type]


def test_same_text_same_hash() -> None:
    assert doc().content_hash == doc().content_hash


def test_changed_text_changes_hash() -> None:
    assert doc().content_hash != doc(description="Fire perimeters").content_hash


def test_keywords_are_part_of_the_text() -> None:
    assert doc(keywords=("fire",)).content_hash != doc().content_hash


def test_extent_or_dates_alone_leave_hash_unchanged() -> None:
    moved = doc(bbox=TEXAS, start=utc(1984), end=utc(2022))
    assert moved.content_hash == doc().content_hash


def test_with_embedding_keeps_identity() -> None:
    embedded = doc().with_embedding([0.1, 0.2])
    assert embedded == doc() and embedded.embedding == [0.1, 0.2]


def test_empty_extent_is_global() -> None:
    assert vd.extent_intersects(None, QUEBEC)


def test_regional_extent_filter() -> None:
    assert not vd.extent_intersects(TEXAS, QUEBEC)
    assert vd.extent_intersects(QUEBEC, (-72.0, 45.3, -71.8, 45.5))


def test_extent_crossing_antimeridian_matches_both_sides() -> None:
    assert vd.extent_intersects(PACIFIC, (-175.0, -45.0, -172.0, -40.0))
    assert vd.extent_intersects(PACIFIC, (172.0, -45.0, 175.0, -40.0))
    assert not vd.extent_intersects(PACIFIC, (0.0, -45.0, 10.0, -40.0))


def test_lon_ranges_split() -> None:
    assert vd.lon_ranges(170.0, -170.0) == [(170.0, 180.0), (-180.0, -170.0)]


def test_normalize_bbox() -> None:
    assert vd.normalize_bbox([-180, -90, 180, 90]) is None
    assert vd.normalize_bbox(None) is None
    assert vd.normalize_bbox([1, 2, 3, 4]) == (1.0, 2.0, 3.0, 4.0)


def test_mongo_encoding_of_empty_extent_and_dates_matches_everything() -> None:
    extent = vd.mongo_extent(None)
    start, end = vd.mongo_dates(None, None)
    assert vd.extent_intersects(extent, QUEBEC)
    assert vd.extent_intersects(extent, PACIFIC)
    assert vd.time_overlaps((start, end), (utc(1900), utc(1901)))
    assert vd.time_overlaps((start, end), (utc(2090), None))


def test_empty_dates_are_open_ended() -> None:
    assert vd.time_overlaps((None, None), (utc(2001), utc(2001, 12, 31)))
    assert vd.time_overlaps((utc(2000), None), (utc(2030), None))


@pytest.mark.parametrize(
    "document, query, expected",
    [
        ((utc(2015), None), (utc(2001), utc(2001, 12, 31)), False),
        ((utc(1984), utc(2000)), (utc(2001), utc(2002)), False),
        ((utc(1984), utc(2001)), (utc(2001), utc(2002)), True),
        ((utc(2002), utc(2003)), (utc(2001), utc(2002)), True),
        ((utc(2015), None), (None, utc(2016)), True),
    ],
)
def test_interval_overlap_edges(document, query, expected) -> None:
    assert vd.time_overlaps(document, query) is expected


def test_parse_instant_and_intervals() -> None:
    start, end = vd.parse_datetime_filter("2020-06-01T12:00:00Z")
    assert start == end == datetime(2020, 6, 1, 12, tzinfo=timezone.utc)
    assert vd.parse_datetime_filter("2001-01-01/2001-12-31") == (
        utc(2001),
        datetime(2001, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert vd.parse_datetime_filter("../2020-12-31T23:59:59Z")[0] is None
    assert vd.parse_datetime_filter("2020-01-01T00:00:00+02:00/..")[1] is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "last summer",
        "2020-13-01",
        "2020-01-01 10:00",
        "../..",
        "2021-01-01/2020-01-01",
    ],
)
def test_parse_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        vd.parse_datetime_filter(value)
