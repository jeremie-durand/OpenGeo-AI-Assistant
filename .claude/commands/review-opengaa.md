---
description: Senior-dev review of the staged changes; records an approval stamp so the commit gate lets the commit through
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(SKIP=review-opengaa-gate pre-commit run:*), Bash(cd opengeo-ai-assistant/web-ui && npx lint-staged), Bash(scripts/review-opengaa-stamp.sh write)
---

Run the OpenGeo AI Assistant pre-commit review on the **staged** changes.

1. **Check there is something to review.** Run `git diff --cached --quiet`. If
   it exits 0 (nothing staged), tell the user to stage their changes first and
   stop.

2. **Run the formatters first**, the same ones the commit hook runs, so the
   reviewed diff is the one that gets committed. From the repo root, record
   `git status --porcelain`, then:
   - Run `SKIP=review-opengaa-gate pre-commit run`. If `pre-commit` is not
     installed, say so and continue.
   - Run `cd opengeo-ai-assistant/web-ui && npx lint-staged`. It re-stages its
     own prettier fixes.

   Record `git status --porcelain` again. If pre-commit failed, or there are
   unstaged modifications that weren't there before, the formatters changed
   staged code. Show which files changed, ask the user to review and `git add`
   them, and stop. Reviewing now would approve a diff that is about to change.

3. **Review.** Invoke the `opengaa-reviewer` agent with the Agent tool:
   "Review the staged changes in this repository." Show its full report to the
   user unchanged.

4. **Record the verdict.** Read the last non-empty line of the report.
   - If it is exactly `VERDICT: APPROVE`, run
     `scripts/review-opengaa-stamp.sh write` from the repo root and tell the
     user they can now commit these exact staged changes. Restaging anything
     needs a new review.
   - If it is `VERDICT: REQUEST CHANGES`, or anything else, write no stamp.
     Summarize the Blockers to fix, and remind the user that the commit will
     be rejected until a review approves.

Never write the stamp yourself in any other situation, and never edit files as
part of this command. If the user asks you to fix the findings, do that as a
separate step, then run `/review-opengaa` again.
