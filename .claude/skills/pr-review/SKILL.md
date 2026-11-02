---
name: pr-review
description: Reviews a GitHub pull request and posts the review (summary + inline comments) directly on GitHub using the gh CLI. Use this whenever the user asks to review a PR, check a pull request, "dai un'occhiata alla PR", leave review comments, or mentions a PR number/URL in the context of reviewing code — even if they don't explicitly say "review". Also use it when the user asks for feedback on a branch that has an open PR. Applies to all the organization's repos (they are all named robin-*).
---

# PR Review

Review a pull request against the organization's context and post the review on GitHub. Outcome policy: **APPROVE** if the PR is clean (zero 🔴 and zero 🟡 findings — nits alone don't block approval), **COMMENT** if there are findings. **Never REQUEST_CHANGES** — blocking findings are still posted as COMMENT and argued on their merits.

## Workflow

### 1. Identify the PR and gather context

```bash
gh pr view <number-or-url> --json number,title,body,author,baseRefName,headRefName,files,url
gh pr diff <number-or-url>
```

If the user didn't specify a PR, list open PRs with `gh pr list` and ask which one.

Sanity check: the org's repos are all named `robin-*`. If the PR belongs to a repo outside that pattern, tell the user the org context may not apply and ask whether to proceed with a generic review.

Read the PR description carefully — it tells you the *intent*. A change that looks wrong in isolation may be deliberate; flag it as a question, not a bug.

Then read `references/org-context.md` for the organization's conventions, stack, review culture, and accepted debt. Also read the matching deep-dive file in `references/repos/<repo-name>.md` — it has repo-specific patterns (forbidden patterns, error-handling contracts, test frameworks) that the org file only summarizes.

**Check existing review activity before analyzing.** The org runs an automated first-pass reviewer (`ross-robin-assistant[bot]`), and humans may have already commented:

```bash
gh api repos/{owner}/{repo}/pulls/<n>/reviews
gh api repos/{owner}/{repo}/pulls/<n>/comments
```

Never re-raise a finding that already exists in a thread (whether from ross, a human, or a previous run of this skill) — duplicating open threads is noise. If you disagree with an existing finding or its resolution, that's a reply in the existing thread, not a new comment.

For non-trivial PRs, don't review the diff in a vacuum: open the surrounding code of the files touched (`gh pr checkout` if working locally, or fetch the files at the head ref) so you understand how the changed lines fit into their context. Reviewing only the diff is the #1 source of false positives.

### 2. Analyze — in this priority order

1. **Bugs & correctness** — logic errors, unhandled edge cases, races/idempotency (conflict-retry paths, fire-and-forget promises racing navigation), broken error handling, off-by-one, incorrect async/await usage.
2. **Security & PHI** — authz completeness (verify the *whole* allow-path, and parity across parallel paths like REST + GraphQL fallback), writes to access-granting fields guarded like reads, sentinel states (`PENDING`, `RBN-0001`) enforced in all paths, PII/tokens in logs, secrets in code.
3. **Org conventions** — only what's codified in `references/org-context.md` and the repo file. Do NOT invent style preferences.
4. **Architecture & design** — raise only for significant issues, framed as questions or discussion points, not directives.

Within these, apply the org's **distilled reviewer checklist** in §3 of `org-context.md` — it's ranked by what the org actually flags. The dominant theme is **untested new branches**: for any test-coverage finding, paste the exact missing test (a concrete `it(...)` block in the repo's test framework — check the repo file, frameworks differ: Jest / Vitest / `node:test`). Also verify every comment and JSDoc touched by the diff is still accurate — the org treats an inaccurate comment as a real defect.

What to skip entirely:
- Anything a linter/guard script/CI already enforces — but **never suggest removing a weird-looking guard script**; they're incident-driven.
- Formatting/style if the code is functional and consistent with existing patterns (there is no enforced formatting org-wide; this is deliberate).
- Anything listed under "accepted debt" in `org-context.md` §4 or the repo file — flagging known debt is noise.
- Pure taste with no org-context backing.
- Nitpicks beyond the cap (see below).

### 3. Draft the review

Every finding gets a severity prefix, matching the org's existing convention:

- 🔴 **blocking** — will or can break at runtime, security/PHI issue, or violated data-model canon (`_version`, soft deletes, error-contract leakage)
- 🟡 **important** — likely problem, untested branch, edge case, or codified-convention violation
- 🟢 **nit** — minor; take it or leave it

Rules of proportion:
- Max **3 nits** per review. If there are more, pick the 3 most useful and drop the rest.
- Every 🔴 and 🟡 comment must include *why* it's a problem and, where feasible, a concrete suggestion (GitHub `suggestion` blocks for small fixes; a pasted `it(...)` block for missing tests).
- If the PR is clean, say so — a short positive summary is a valid review. Don't manufacture findings to justify the review's existence.

The summary body must end with a **"Not audited"** line stating what the review did not cover (scope honesty is an org convention — e.g. "Not audited: the EAS build config changes, the migration SQL").

Comment tone: direct, technical, no fluff. Write all posted comments in English (org convention: code-facing text is English; Italian is for human replies in threads), regardless of the conversation language with the user.

### 4. Post the review

Post everything in **one atomic call** to the reviews endpoint — summary + inline comments together. Choose the event:

- `APPROVE` — only when there are zero 🔴 and zero 🟡 findings. Nits may still ride along inline. The summary should say what was checked, not just "LGTM".
- `COMMENT` — whenever there is at least one 🔴 or 🟡 finding.
- `REQUEST_CHANGES` — never. The bundled script rejects it.

Use the bundled script, which handles payload assembly:

```bash
scripts/post_review.sh <owner/repo> <pr-number> <review.json>
```

Where `review.json` is (`event` per the policy above):

```json
{
  "body": "## Review summary\n\n...overall assessment...",
  "event": "COMMENT",
  "comments": [
    {
      "path": "src/handlers/user.ts",
      "line": 42,
      "side": "RIGHT",
      "body": "🔴 **bug** — `userId` can be undefined here because ..."
    }
  ]
}
```

#### Getting `line` and `side` right (this is where reviews fail)

- `line` is the line number **in the file at the head of the PR**, not a diff position.
- `side: "RIGHT"` for added/unchanged lines shown in the new version; `side: "LEFT"` only when commenting on a *deleted* line.
- You can only comment on lines that appear in the diff hunks. If your finding concerns a line outside the diff, put it in the summary body instead, referencing `path:line`.
- For multi-line comments add `start_line` and `start_side`.

Before posting, validate: every `path` must be in the PR's changed files, and every `line` must fall inside a hunk of that file's diff. Comments failing this make the entire API call return 422 — one bad comment kills the whole review.

If the post fails with 422, remove or relocate the offending comment (move it to the summary) and retry. Do not retry more than twice; on repeated failure, post the summary-only review and report the inline comments to the user in chat.

### 5. Report back

After posting, give the user a one-paragraph recap in chat: number of findings by severity, the single most important issue, and the review URL.
