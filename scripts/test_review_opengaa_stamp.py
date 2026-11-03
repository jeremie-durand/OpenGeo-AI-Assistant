"""Tests for the /review-opengaa commit-gate stamp script.

Usage:
    python -m pytest scripts/test_review_opengaa_stamp.py
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "review-opengaa-stamp.sh"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given repository."""
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def stamp(repo: Path, mode: str) -> subprocess.CompletedProcess[str]:
    """Run the stamp script in the given mode inside the repository."""
    return subprocess.run(
        ["bash", str(SCRIPT), mode], cwd=repo, capture_output=True, text=True
    )


def stage(repo: Path, name: str, content: str) -> None:
    """Write a file and stage it."""
    (repo / name).write_text(content)
    git(repo, "add", name)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create an empty git repository with a committer identity."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


@pytest.fixture
def committed_repo(repo: Path) -> Path:
    """Create a repository with one existing commit."""
    stage(repo, "base.txt", "base\n")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def test_matching_stamp_passes(committed_repo: Path) -> None:
    """An approved staged diff passes the check."""
    stage(committed_repo, "a.txt", "a\n")
    assert stamp(committed_repo, "write").returncode == 0
    assert stamp(committed_repo, "check").returncode == 0


def test_changed_stage_fails(committed_repo: Path) -> None:
    """Staging more changes after approval invalidates the stamp."""
    stage(committed_repo, "a.txt", "a\n")
    stamp(committed_repo, "write")
    stage(committed_repo, "a.txt", "a\nb\n")
    result = stamp(committed_repo, "check")
    assert result.returncode == 1
    assert "/review-opengaa" in result.stderr


def test_missing_stamp_fails(committed_repo: Path) -> None:
    """A commit without any review is rejected."""
    stage(committed_repo, "a.txt", "a\n")
    assert stamp(committed_repo, "check").returncode == 1


def test_new_head_fails(committed_repo: Path) -> None:
    """The same diff on top of a different HEAD needs a new review."""
    stage(committed_repo, "a.txt", "a\n")
    stamp(committed_repo, "write")
    git(committed_repo, "stash", "-q")
    git(committed_repo, "commit", "-q", "--allow-empty", "-m", "other")
    git(committed_repo, "stash", "pop", "-q", "--index")
    assert stamp(committed_repo, "check").returncode == 1


def test_merge_commit_exempt(committed_repo: Path) -> None:
    """A merge in progress passes without a stamp."""
    git(committed_repo, "switch", "-q", "-c", "feature")
    stage(committed_repo, "feature.txt", "f\n")
    git(committed_repo, "commit", "-q", "-m", "feature")
    git(committed_repo, "switch", "-q", "main")
    stage(committed_repo, "main.txt", "m\n")
    git(committed_repo, "commit", "-q", "-m", "main")
    git(committed_repo, "merge", "-q", "--no-commit", "--no-ff", "feature")
    assert stamp(committed_repo, "check").returncode == 0


def test_empty_stage_exempt(committed_repo: Path) -> None:
    """Message-only amends and empty commits have nothing to review."""
    assert stamp(committed_repo, "check").returncode == 0


def test_initial_commit(repo: Path) -> None:
    """The gate works before the first commit exists."""
    stage(repo, "a.txt", "a\n")
    assert stamp(repo, "check").returncode == 1
    stamp(repo, "write")
    assert stamp(repo, "check").returncode == 0


def test_write_refuses_empty_stage(committed_repo: Path) -> None:
    """No stamp is written when nothing is staged."""
    assert stamp(committed_repo, "write").returncode == 1
    path = git(committed_repo, "rev-parse", "--git-path", "review-opengaa.stamp")
    assert not (committed_repo / path.stdout.strip()).exists()


def test_stamp_is_not_tracked(committed_repo: Path) -> None:
    """The stamp lives in the git directory, outside the work tree."""
    stage(committed_repo, "a.txt", "a\n")
    stamp(committed_repo, "write")
    status = git(committed_repo, "status", "--porcelain").stdout
    assert "review-opengaa.stamp" not in status
