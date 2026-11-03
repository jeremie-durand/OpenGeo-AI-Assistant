#!/usr/bin/env bash
# Review stamp for the /review-opengaa commit gate.
#
# Usage:
#   review-opengaa-stamp.sh write   record approval of the current staged diff
#   review-opengaa-stamp.sh check   exit 0 only if the staged diff was approved
#
# The stamp is a git object hash of HEAD plus the staged diff, stored inside the
# git directory so it is never committed. Merge commits and commits with nothing
# staged (message-only amends, empty commits) are exempt from the check.
set -euo pipefail

stamp_path() {
    git rev-parse --git-path review-opengaa.stamp
}

current_hash() {
    {
        git rev-parse -q --verify HEAD || true
        git diff --cached --binary --full-index --no-color \
            --no-ext-diff --no-textconv
    } | git hash-object --stdin
}

write_stamp() {
    if git diff --cached --quiet; then
        echo "review-opengaa: nothing staged, no stamp written." >&2
        exit 1
    fi
    current_hash > "$(stamp_path)"
    echo "review-opengaa: approval recorded for the staged changes."
}

check_stamp() {
    if git rev-parse -q --verify MERGE_HEAD > /dev/null; then
        exit 0
    fi
    if git diff --cached --quiet; then
        exit 0
    fi
    local stamp
    stamp="$(stamp_path)"
    if [[ -f "$stamp" && "$(cat "$stamp")" == "$(current_hash)" ]]; then
        exit 0
    fi
    cat >&2 << 'EOF'
review-opengaa: no approved review matches the staged changes.
Run /review-opengaa in Claude Code, then commit again.
(Deliberate bypass: git commit --no-verify)
EOF
    exit 1
}

case "${1:-}" in
    write) write_stamp ;;
    check) check_stamp ;;
    *)
        echo "usage: $0 {write|check}" >&2
        exit 2
        ;;
esac
