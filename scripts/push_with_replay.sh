#!/usr/bin/env bash
# Push HEAD to origin/main, replaying this run's outputs if another workflow got
# there first. Extracted from inline YAML because the snipe poller can commit
# several times in one job and needed the same logic each time.
#
# Not a rebase: only history.jsonl has a merge driver, so rebasing conflicts on
# nfl.json and sniped.json. reset-plus-replay cannot conflict at all.
# 3 pushes, 2 replays — no replay after the final attempt.
set -uo pipefail

OUT="${RUNNER_TEMP:-/tmp}/out"
MSG="${1:-data: update}"

for attempt in 1 2 3; do
  if git push; then
    exit 0
  fi
  echo "push $attempt rejected"
  [ "$attempt" = 3 ] && break
  git fetch origin
  # The push may have LANDED and only reported failure.
  if git merge-base --is-ancestor HEAD origin/main; then
    echo "our commit is already on origin — the push did land"
    exit 0
  fi
  git reset --hard origin/main
  ARGS=()
  [ -f "$OUT/new_rows.jsonl"  ] && ARGS+=(--rows  "$OUT/new_rows.jsonl")
  [ -f "$OUT/sniped_ids.json" ] && ARGS+=(--ids   "$OUT/sniped_ids.json")
  [ -f "$OUT/nfl.json"        ] && ARGS+=(--nfl   "$OUT/nfl.json")
  [ -f "$OUT/usage.json"      ] && ARGS+=(--usage "$OUT/usage.json")
  if [ ${#ARGS[@]} -gt 0 ]; then
    python3 scripts/replay_outputs.py "${ARGS[@]}"
  fi
  git add data/nfl.json data/usage.json data/history.jsonl data/sniped.json 2>/dev/null || true
  if git diff --cached --quiet; then
    echo "nothing left to commit after replay"
    exit 0
  fi
  git commit -q -m "$MSG (replay $attempt)"
done
echo "::error::could not push after 3 attempts; this run's outputs are in the uploaded artifact"
exit 1
