"""End-to-end collection selection accuracy: fixed list versus retrieval.

A question is answered correctly when the first collection the LLM selects is
one of its acceptable ids. Used by ``cli evaluate`` with a real LLM (D12).
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .index import VectorIndex

DEFAULT_QUESTIONS = Path(__file__).with_name("collection_selection_eval.json")
FIXED = "fixed"
RETRIEVAL = "retrieval"
MAX_DEFICIT = 1


@dataclass
class Score:
    """Correct answers out of the questions asked, for one language and path."""

    correct: int = 0
    total: int = 0

    def add(self, ok: bool) -> None:
        """Count one question."""
        self.total += 1
        self.correct += int(ok)

    def __str__(self) -> str:
        rate = self.correct / self.total if self.total else 0.0
        return f"{self.correct}/{self.total} ({rate:.2f})"


@dataclass
class Report:
    """Scores per language and path, and the misses for each path."""

    scores: Dict[str, Dict[str, Score]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Score))
    )
    misses: List[str] = field(default_factory=list)

    def passes(self) -> bool:
        """True when retrieval trails the fixed list by at most MAX_DEFICIT.

        Checked per language; a stricter rule fails on LLM variance alone with
        about 15 questions per language.
        """
        return all(
            paths[FIXED].correct - paths[RETRIEVAL].correct <= MAX_DEFICIT
            for paths in self.scores.values()
        )

    def lines(self) -> List[str]:
        """Return the printable report."""
        out = [f"{'lang':<6}{FIXED:<16}{RETRIEVAL:<16}"]
        for lang, paths in sorted(self.scores.items()):
            out.append(f"{lang:<6}{str(paths[FIXED]):<16}{str(paths[RETRIEVAL]):<16}")
        out += self.misses
        margin = f"by more than {MAX_DEFICIT} question(s) in a language"
        out.append(
            "PASS" if self.passes() else f"FAIL: retrieval trails fixed {margin}"
        )
        return out


def load_questions(path: Path) -> List[Dict[str, Any]]:
    """Read the evaluation questions from *path*."""
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


async def _select_both(
    translator: Any, index: VectorIndex, question: str
) -> Dict[str, List[str]]:
    """Return the ids each path selects for *question*."""
    from generic_query_translator import _retrieval_hits

    hits = await _retrieval_hits(question, index=index)
    if hits is None:
        raise RuntimeError(f"Retrieval failed for: {question}")
    return {
        FIXED: await translator._fixed_list_selection(question),
        RETRIEVAL: await translator._candidate_selection(question, hits),
    }


async def evaluate(
    translator: Any, index: VectorIndex, questions: List[Dict[str, Any]]
) -> Report:
    """Score both selection paths on *questions* with *translator*'s LLM."""
    report = Report()
    for item in questions:
        selected = await _select_both(translator, index, item["question"])
        acceptable = set(item["acceptable"])
        for path, ids in selected.items():
            ok = bool(ids) and ids[0] in acceptable
            report.scores[item["lang"]][path].add(ok)
            if not ok:
                report.misses.append(f"MISS {path:<10}{item['question']} -> {ids}")
    return report
