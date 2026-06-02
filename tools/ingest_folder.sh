#!/usr/bin/env bash
#
# Bulk-identify a folder of card photos with your Claude Code subscription
# (no metered API key) and push the results into the running web app.
#
# For each image it asks `claude -p` (headless Claude Code) to read the photo and
# emit the detection JSON, then POSTs the image + JSON to /api/ingest. The server
# crops and prices each card and lands it as a `preview` to review/add in the UI.
#
# Usage:
#   tools/ingest_folder.sh <FOLDER> [SERVER_URL]
#
# Examples:
#   tools/ingest_folder.sh ~/card-photos
#   tools/ingest_folder.sh ~/card-photos http://127.0.0.1:8000
#
# Prerequisites:
#   - The app is running (./run.sh) and reachable at SERVER_URL.
#   - Claude Code is installed and logged in to your Claude subscription
#     (`claude` on PATH). No ANTHROPIC_API_KEY is needed by the app.
#   - Run from the project root so the venv + prompt module are importable.
set -euo pipefail
cd "$(dirname "$0")/.."

FOLDER="${1:-}"
URL="${2:-http://127.0.0.1:8000}"

if [[ -z "$FOLDER" || ! -d "$FOLDER" ]]; then
  echo "Usage: tools/ingest_folder.sh <FOLDER> [SERVER_URL]" >&2
  exit 1
fi
command -v claude >/dev/null 2>&1 || { echo "ERROR: 'claude' (Claude Code) not found on PATH." >&2; exit 1; }
command -v curl   >/dev/null 2>&1 || { echo "ERROR: 'curl' not found on PATH." >&2; exit 1; }

# Single source of truth for the detection schema: the app's own prompt.
PY="${PYTHON:-.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"
PROMPT="$("$PY" -c 'from app.prompts.card_detection import DETECTION_SYSTEM; print(DETECTION_SYSTEM)')"

shopt -s nullglob nocaseglob
images=("$FOLDER"/*.jpg "$FOLDER"/*.jpeg "$FOLDER"/*.png "$FOLDER"/*.webp)
shopt -u nullglob nocaseglob

total=${#images[@]}
if (( total == 0 )); then
  echo "No images (*.jpg/.jpeg/.png/.webp) found in $FOLDER" >&2
  exit 1
fi
echo "Ingesting $total image(s) from $FOLDER -> $URL/api/ingest"

ok=0; fail=0; i=0
for img in "${images[@]}"; do
  i=$((i+1))
  printf "[%d/%d] %s ... " "$i" "$total" "$(basename "$img")"

  # Ask headless Claude Code to read this photo and return ONLY the JSON.
  if ! json="$(claude -p "${PROMPT}"$'\n\n'"Read the image at the path '${img}' and return ONLY the JSON object for every card in it." \
        --allowedTools Read --output-format text 2>/dev/null)"; then
    echo "FAIL (claude)"; fail=$((fail+1)); continue
  fi
  if [[ -z "${json// }" ]]; then
    echo "FAIL (empty response)"; fail=$((fail+1)); continue
  fi

  # POST image + detections. The app tolerates fenced/messy JSON on its side.
  if curl -sf -o /dev/null \
        -F "image=@${img}" \
        -F "detections=${json}" \
        "${URL}/api/ingest"; then
    echo "OK"; ok=$((ok+1))
  else
    echo "FAIL (ingest)"; fail=$((fail+1))
  fi
done

echo "Done. $ok ingested, $fail failed. Review and add them at ${URL}/"
