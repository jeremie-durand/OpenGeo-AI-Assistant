"""Collection document contract, indexed text, and extent/datetime helpers."""

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import List, Optional, Sequence, Tuple

Bbox = Tuple[float, float, float, float]
Interval = Tuple[Optional[datetime], Optional[datetime]]

WORLD_BBOX: Bbox = (-180.0, -90.0, 180.0, 90.0)
OPEN_START = datetime(1, 1, 1, tzinfo=timezone.utc)
OPEN_END = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
OPEN_END_MARKER = ".."

SOURCE_PLANETARY_COMPUTER = "planetary_computer"
SOURCE_PRIVATE_STAC = "private_stac"
SOURCE_EXTERNAL = "external"


@dataclass(frozen=True)
class CollectionDocument:
    """One searchable STAC collection, as stored in every backend."""

    id: str
    title: str
    description: str = ""
    keywords: Tuple[str, ...] = ()
    bbox: Optional[Bbox] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    source: str = SOURCE_PLANETARY_COMPUTER
    embedding: Optional[List[float]] = field(default=None, compare=False, repr=False)

    @property
    def text(self) -> str:
        """Return the text that is embedded for this collection."""
        parts = [self.title, self.description, ", ".join(self.keywords)]
        return "\n".join(part for part in parts if part)

    @property
    def content_hash(self) -> str:
        """Return a hash of the embedded text; it decides re-embedding."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def with_embedding(self, embedding: List[float]) -> "CollectionDocument":
        """Return a copy of this document carrying *embedding*."""
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["embedding"] = embedding
        return CollectionDocument(**values)


def lon_ranges(minx: float, maxx: float) -> List[Tuple[float, float]]:
    """Split a longitude span into ranges, two when it crosses the antimeridian."""
    if minx <= maxx:
        return [(minx, maxx)]
    return [(minx, 180.0), (-180.0, maxx)]


def extent_intersects(extent: Optional[Bbox], query: Optional[Bbox]) -> bool:
    """Return True when *extent* (None means global) intersects *query*."""
    if extent is None or query is None:
        return True
    if extent[1] > query[3] or extent[3] < query[1]:
        return False
    return any(
        lo <= q_hi and hi >= q_lo
        for lo, hi in lon_ranges(extent[0], extent[2])
        for q_lo, q_hi in lon_ranges(query[0], query[2])
    )


def normalize_bbox(values: Optional[Sequence[float]]) -> Optional[Bbox]:
    """Return a 4-number bbox, or None when *values* is missing or global."""
    if not values or len(values) != 4:
        return None
    bbox = tuple(float(value) for value in values)
    if bbox == WORLD_BBOX:
        return None
    return bbox  # type: ignore[return-value]


def mongo_extent(bbox: Optional[Bbox]) -> Bbox:
    """Return the MongoDB extent fields: an empty extent is the whole world."""
    return bbox if bbox is not None else WORLD_BBOX


def mongo_dates(start: Optional[datetime], end: Optional[datetime]) -> Interval:
    """Return MongoDB dates: open ends become fixed sentinel dates."""
    return (start or OPEN_START, end or OPEN_END)


def time_overlaps(document: Interval, query: Interval) -> bool:
    """Return True when two intervals overlap; None ends are unbounded."""
    doc_start, doc_end = document
    query_start, query_end = query
    if query_end is not None and doc_start is not None and doc_start > query_end:
        return False
    if query_start is not None and doc_end is not None and doc_end < query_start:
        return False
    return True


def parse_datetime_filter(value: str) -> Interval:
    """Parse an RFC 3339 instant or interval (with `..` open ends).

    Raises ValueError when the value is malformed or the interval is reversed.
    """
    text = value.strip()
    if not text:
        raise ValueError("datetime is empty")
    if "/" not in text:
        instant = parse_instant(text, end_of_day=False)
        if len(text) == 10:
            return instant, parse_instant(text, end_of_day=True)
        return instant, instant
    raw_start, _, raw_end = text.partition("/")
    start = _parse_bound(raw_start, end_of_day=False)
    end = _parse_bound(raw_end, end_of_day=True)
    if start is None and end is None:
        raise ValueError("datetime interval needs at least one closed end")
    if start is not None and end is not None and start > end:
        raise ValueError("datetime interval start is after its end")
    return start, end


def _parse_bound(text: str, end_of_day: bool) -> Optional[datetime]:
    """Parse one interval bound; `..` or an empty string means open."""
    stripped = text.strip()
    if stripped in ("", OPEN_END_MARKER):
        return None
    return parse_instant(stripped, end_of_day)


def parse_instant(text: str, end_of_day: bool = False) -> datetime:
    """Parse an RFC 3339 date-time or full-date into an aware UTC datetime."""
    if len(text) == 10:
        day = date.fromisoformat(text)
        clock = time(23, 59, 59) if end_of_day else time(0, 0)
        return datetime.combine(day, clock, tzinfo=timezone.utc)
    if "T" not in text.upper():
        raise ValueError(f"'{text}' is not an RFC 3339 date-time")
    parsed = datetime.fromisoformat(text.replace("z", "Z"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
