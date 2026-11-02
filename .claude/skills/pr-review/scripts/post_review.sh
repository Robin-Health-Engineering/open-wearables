#!/usr/bin/env bash
# Post a PR review (summary + inline comments) atomically via the GitHub API.
# Policy: APPROVE only if clean (no 🔴/🟡 comments), COMMENT otherwise, REQUEST_CHANGES never.
# Usage: post_review.sh <owner/repo> <pr-number> <review.json>
set -euo pipefail

REPO="${1:?usage: post_review.sh <owner/repo> <pr-number> <review.json>}"
PR="${2:?missing PR number}"
PAYLOAD="${3:?missing review.json path}"

python3 - "$PAYLOAD" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))
event = r.get("event")
assert event in ("COMMENT", "APPROVE"), f"event must be COMMENT or APPROVE, got: {event!r} (REQUEST_CHANGES is forbidden)"
assert r.get("body", "").strip(), "review body is empty"
has_blocking = False
for c in r.get("comments", []):
    assert c.get("path") and c.get("line") and c.get("side") in ("LEFT", "RIGHT"), f"malformed comment: {c}"
    if c.get("body", "").startswith(("\U0001F534", "\U0001F7E1")):  # 🔴 🟡
        has_blocking = True
if event == "APPROVE":
    assert not has_blocking, "cannot APPROVE: payload contains 🔴/🟡 findings — use COMMENT"
print(f"payload OK: event={event}, {len(r.get('comments', []))} inline comments")
PYEOF

gh api \
  --method POST \
  -H "Accept: application/vnd.github+json" \
  "repos/${REPO}/pulls/${PR}/reviews" \
  --input "$PAYLOAD"
