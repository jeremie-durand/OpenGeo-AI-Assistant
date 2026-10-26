"""Render the dashboard's focused-collection stats as a prompt hint."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.dashboard_catalog import renderable_collection_ids
from services.dashboard_stats import CLOUD_BUCKET_LABELS, ITEM_LIMIT

CAPPED_MONTHLY_TEXT = (
    f"the search hit its {ITEM_LIMIT}-scene cap, so the monthly counts are "
    "incomplete and must not be compared; months with few or no counted "
    "scenes may have many"
)

ACTION_PREFIX = re.compile(
    r"^(show|display|load|go to|fly to|navigate|zoom|take me|"
    r"affiche[rz]?|montre[rz]?|charge[rz]?|va |allez |aller )"
)
QUESTION_START = re.compile(
    r"^(why|how|what|when|which|is|are|does|do|"
    r"pourquoi|comment|combien|quand|quel|quelle|quels|quelles|est-ce)\b"
)
DASHBOARD_TERMS = re.compile(
    r"\b(scenes?|acquisitions?|clouds?|cloudy|cloud cover|"
    r"latest|newest|most recent|how many|dashboard|chart|months?|monthly|"
    r"january|february|march|april|june|july|august|september|october|"
    r"november|december|sc[eè]nes?|nuages?|nuageux|nuageuse|derni[eè]re?|"
    r"r[eé]cente?|combien|tableau de bord|graphique|mois|mensuel|janvier|"
    r"f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|"
    r"novembre|d[eé]cembre)\b"
)


def _monthly_text(monthly: List[Dict[str, Any]]) -> str:
    return ", ".join(f"{m['month']}: {m['count']}" for m in monthly)


def _cloud_text(cloud_cover: List[int]) -> str:
    return ", ".join(
        f"{label}: {count}" for label, count in zip(CLOUD_BUCKET_LABELS, cloud_cover)
    )


def _latest_text(latest: Any) -> str:
    if isinstance(latest, datetime):
        return latest.date().isoformat()
    return str(latest)[:10]


def _comparison_text(monthly: List[Dict[str, Any]]) -> str:
    """State the average and lowest complete months; the last month is current.

    Small models misread a raw month list, so the comparison is precomputed.
    """
    *complete, current = monthly
    parts = []
    if complete:
        average = sum(m["count"] for m in complete) / len(complete)
        lowest = sorted(complete, key=lambda m: m["count"])[:2]
        parts.append(f"average {average:.1f} scenes per complete month")
        parts.append(
            "lowest complete months: "
            + ", ".join(f"{m['month']} ({m['count']})" for m in lowest)
        )
    parts.append(f"{current['month']} is the current month and still incomplete")
    return "; ".join(parts)


def format_dashboard_hint(req_body: Dict[str, Any]) -> Optional[str]:
    """Return a one-line summary of the dashboard context, or None.

    None when the request carries no context or names a collection the map
    cannot render.
    """
    context = req_body.get("dashboard_context")
    if not context or context.get("collection") not in renderable_collection_ids():
        return None
    count = context["scene_count"]
    count_text = f"{count}+" if context.get("capped") else str(count)
    period = "in the last 12 months" if context.get("monthly") else "in total"
    parts = [
        f"{context['collection']} over bbox {context['bbox']}: "
        f"{count_text} scenes {period}"
    ]
    if context.get("monthly") and context.get("capped"):
        parts.append(CAPPED_MONTHLY_TEXT)
    elif context.get("monthly"):
        parts.append(f"scenes per month: {_monthly_text(context['monthly'])}")
        parts.append(_comparison_text(context["monthly"]))
    if context.get("latest_datetime"):
        parts.append(f"latest acquisition {_latest_text(context['latest_datetime'])}")
    if context.get("cloud_cover"):
        parts.append(f"scenes by cloud cover: {_cloud_text(context['cloud_cover'])}")
    return f"[Dashboard shown to the user: {'; '.join(parts)}]"


def is_dashboard_question(req_body: Dict[str, Any]) -> bool:
    """True for a question about the dashboard's figures while it shows stats.

    Needs dashboard context, a question (not a show or go-to request) and a
    term naming a dashboard figure: scenes, cloud cover, a month, the latest
    date. "May" is left out of the months, as in "may I".
    """
    if format_dashboard_hint(req_body) is None:
        return False
    query = (req_body.get("query") or "").strip().lower()
    if ACTION_PREFIX.match(query):
        return False
    is_question = query.endswith("?") or bool(QUESTION_START.match(query))
    return is_question and bool(DASHBOARD_TERMS.search(query))
