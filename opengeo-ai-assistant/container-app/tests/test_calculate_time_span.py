"""Tests for the human-readable time span used in comparison analysis."""

import pytest
from geoint.agents import _calculate_time_span


@pytest.mark.parametrize(
    ("before_date", "after_date", "expected"),
    [
        ("2024-01-15", "2024-01-20", "5 days"),
        ("2024-01-15T00:00:00Z", "2024-01-15T05:00:00Z", "5 hours"),
        ("2020-01-15", "2024-01-15", "4 years"),
    ],
)
def test_matching_date_formats(
    before_date: str, after_date: str, expected: str
) -> None:
    assert _calculate_time_span(before_date, after_date) == expected


@pytest.mark.parametrize(
    ("before_date", "after_date", "expected"),
    [
        ("2024-01-15T00:00:00Z", "2024-02-15", "1 months"),
        ("2024-01-15", "2024-01-20T00:00:00+00:00", "5 days"),
    ],
)
def test_one_date_with_time_zone_is_treated_as_utc(
    before_date: str, after_date: str, expected: str
) -> None:
    assert _calculate_time_span(before_date, after_date) == expected


def test_offset_is_applied_when_both_dates_have_time_zones() -> None:
    span = _calculate_time_span(
        "2024-01-15T00:00:00+00:00", "2024-01-15T05:00:00-02:00"
    )

    assert span == "7 hours"


def test_unparseable_date_returns_fallback() -> None:
    assert _calculate_time_span("bad", "2024-01-15") == "unknown time span"
