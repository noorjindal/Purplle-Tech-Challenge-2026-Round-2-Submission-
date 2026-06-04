#!/usr/bin/env bash
# run.sh — process all clips for a store through the detection pipeline into the API.
# Usage: ./run.sh <CLIPS_DIR> <STORE_ID> [API_URL]
#   ./run.sh ./clips/store_1008 ST1008 http://localhost:8000
set -euo pipefail

CLIPS_DIR="${1:?usage: run.sh <clips_dir> <store_id> [api_url]}"
STORE="${2:?store id required, e.g. ST1008}"
API="${3:-http://localhost:8000}"
LAYOUT="$(dirname "$0")/../data/store_layout.json"

declare -A CAM_MAP=( ["entry"]="CAM1" ["floor"]="CAM2" ["billing"]="CAM3" )

for role in entry floor billing; do
  clip=$(ls "$CLIPS_DIR"/*"$role"* 2>/dev/null | head -1 || true)
  if [[ -n "${clip:-}" ]]; then
    echo ">> ${CAM_MAP[$role]} <- $clip"
    python "$(dirname "$0")/detect.py" \
      --clip "$clip" --store "$STORE" --camera "${CAM_MAP[$role]}" \
      --layout "$LAYOUT" --api "$API" --out "events_${STORE}_${role}.jsonl"
  else
    echo ">> no clip matched role=$role in $CLIPS_DIR (skipping)"
  fi
done
echo "All clips processed for $STORE."
