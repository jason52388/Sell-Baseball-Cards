#!/usr/bin/env bash
#
# Bulk-identify a folder of card photos with your Claude Code subscription
# (no metered API key) and push the results into the running web app.
#
# For each image it asks `claude -p` (headless Claude Code) to read the photo and
# emit the detection JSON, then POSTs the image + JSON to /api/ingest. The server
# crops and prices each card, keeps its own copy of the photo in
# data/inbox/processed (so it archives later), and lands the cards as `preview`
# to review/add in the UI.
#
# Usage:
#   tools/ingest_folder.sh [FOLDER] [SERVER_URL]
#
# With no FOLDER it processes the website's inbox (data/inbox) — i.e. the photos
# you dropped via the "Queue for Claude" button.
#
# What happens to each photo:
#   ingested          inbox: removed (the server kept its copy in data/inbox/processed)
#                     any other folder: moved to FOLDER/processed
#   already uploaded  moved to FOLDER/duplicates (the server answered 409)
#   no cards found    moved to FOLDER/failed, with a .txt note saying why
#   other failure     left in place, so the next run tries it again
# Moves never overwrite: a name already taken gets a -2, -3 ... suffix.
#
# HEIC photos (iPhone) are converted to JPEG with macOS `sips` first. Files that
# are not photos are listed as skipped.
#
# Examples:
#   tools/ingest_folder.sh                       # process the website inbox
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

FOLDER="${1:-data/inbox}"   # default: the website's queue inbox
URL="${2:-http://127.0.0.1:8000}"

if [[ ! -d "$FOLDER" ]]; then
  echo "Folder not found: $FOLDER" >&2
  echo "Usage: tools/ingest_folder.sh [FOLDER] [SERVER_URL]" >&2
  exit 1
fi
PROCESSED="$FOLDER/processed"
FAILED="$FOLDER/failed"
DUPLICATES="$FOLDER/duplicates"
command -v claude >/dev/null 2>&1 || { echo "ERROR: 'claude' (Claude Code) not found on PATH." >&2; exit 1; }
command -v curl   >/dev/null 2>&1 || { echo "ERROR: 'curl' not found on PATH." >&2; exit 1; }

# The website inbox: the server's own copy replaces the original after ingest.
IN_INBOX=0
mkdir -p data/inbox
if [[ "$(cd "$FOLDER" && pwd -P)" == "$(cd data/inbox && pwd -P)" ]]; then
  IN_INBOX=1
fi

# Single source of truth for the detection schema: the app's own prompt.
PY="${PYTHON:-.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"
PROMPT="$("$PY" -c 'from app.prompts.card_detection import DETECTION_SYSTEM; print(DETECTION_SYSTEM)')"

# move_unique SRC DIR: move SRC into DIR without overwriting anything there.
move_unique() {
  local src="$1" dir="$2" base stem ext target n=1
  mkdir -p "$dir"
  base="$(basename "$src")"
  if [[ "$base" == *.* ]]; then stem="${base%.*}"; ext=".${base##*.}"; else stem="$base"; ext=""; fi
  target="$dir/$base"
  while [[ -e "$target" ]]; do
    n=$((n+1)); target="$dir/${stem}-${n}${ext}"
  done
  mv "$src" "$target"
  printf '%s' "$target"
}

shopt -s nullglob nocaseglob
images=("$FOLDER"/*.jpg "$FOLDER"/*.jpeg "$FOLDER"/*.png "$FOLDER"/*.webp "$FOLDER"/*.heic "$FOLDER"/*.heif)
shopt -u nocaseglob
# Anything else in the folder is reported, never silently ignored.
skipped=()
for f in "$FOLDER"/*; do
  [[ -f "$f" ]] || continue
  case "$(printf '%s' "${f##*.}" | tr '[:upper:]' '[:lower:]')" in
    jpg|jpeg|png|webp|heic|heif|tag) ;;
    *) [[ "$(basename "$f")" == .* ]] || skipped+=("$(basename "$f")") ;;
  esac
done
shopt -u nullglob
if (( ${#skipped[@]} > 0 )); then
  echo "Skipping ${#skipped[@]} file(s) that are not photos (jpg/jpeg/png/webp/heic):"
  printf '  %s\n' "${skipped[@]}"
fi

total=${#images[@]}
if (( total == 0 )); then
  echo "No images (*.jpg/.jpeg/.png/.webp/.heic) to process in $FOLDER"
  exit 0
fi
echo "Ingesting $total image(s) from $FOLDER -> $URL/api/ingest"

# Inside the project, so headless Claude may read it like the inbox photos.
mkdir -p data
WORK="$(mktemp -d data/.ingest_view.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

ok=0; fail=0; dup=0; nocards=0; i=0
for img in "${images[@]}"; do
  i=$((i+1))
  printf "[%d/%d] %s ... " "$i" "$total" "$(basename "$img")"

  # HEIC: convert to a JPEG for both Claude and the upload (the server would
  # convert too, but only where pillow-heif or sips is available).
  send="$img"
  case "$(printf '%s' "${img##*.}" | tr '[:upper:]' '[:lower:]')" in
    heic|heif)
      if ! command -v sips >/dev/null 2>&1; then
        echo "SKIP (HEIC needs macOS sips; export it as JPG)"; fail=$((fail+1)); continue
      fi
      base="$(basename "$img")"
      send="$WORK/${base%.*}.jpg"
      if ! sips -s format jpeg "$img" --out "$send" >/dev/null 2>&1; then
        echo "FAIL (could not convert HEIC)"; fail=$((fail+1)); continue
      fi
      ;;
  esac

  # Claude reads an upright copy: the cropper turns the photo upright before
  # cutting, so the boxes must be read off the upright view, not the sideways
  # pixels a phone stores. The original is still what gets uploaded below.
  if ! view="$("$PY" -m tools.upright_copy "$send" "$WORK")"; then
    echo "FAIL (unreadable image)"; fail=$((fail+1)); continue
  fi

  # Ask headless Claude Code to read this photo and return ONLY the JSON.
  # Write to a temp file (NOT a shell var): the detection JSON often contains
  # apostrophes/quotes (e.g. "Cubs' Sammy Sosa") that corrupt a -F "field=$var"
  # POST. curl's "field=<file" form sends the raw file contents verbatim.
  json_file="$(mktemp -t ingest_det.XXXXXX)"
  resp_file="$(mktemp -t ingest_resp.XXXXXX)"
  # The detection rules go in as the system prompt (as the app's own
  # claude_cli provider does) and the model is pinned, so a folder ingest
  # reads cards exactly like an in-app upload.
  if ! claude -p "Detect every sports card in this image (up to 9). Read the image at the path '${view}' and return ONLY the JSON object for every card in it." \
        --system-prompt "${PROMPT}" \
        --model "${CLAUDE_CLI_MODEL:-claude-opus-5-5}" \
        --allowedTools Read --output-format text >"$json_file" 2>/dev/null; then
    rm -f "$json_file" "$resp_file" "$view"; echo "FAIL (claude)"; fail=$((fail+1)); continue
  fi
  if [[ ! -s "$json_file" ]]; then
    rm -f "$json_file" "$resp_file" "$view"; echo "FAIL (empty response)"; fail=$((fail+1)); continue
  fi

  # Forward the batch tag if the "Queue for Claude" button left a sidecar.
  tag_args=()
  if [[ -f "${img}.tag" ]]; then
    tag_args=(-F "batch_tag=$(cat "${img}.tag")")
  fi

  # POST image + detections. The app tolerates fenced/messy JSON on its side.
  code="$(curl -s -o "$resp_file" -w '%{http_code}' \
        -F "image=@${send}" \
        -F "detections=<${json_file}" \
        ${tag_args[@]+"${tag_args[@]}"} \
        "${URL}/api/ingest" || echo 000)"
  case "$code" in
    200)
      if (( IN_INBOX )); then
        rm -f "$img"   # the server saved its own copy in data/inbox/processed
      else
        move_unique "$img" "$PROCESSED" >/dev/null
      fi
      rm -f "${img}.tag" 2>/dev/null || true
      echo "OK"; ok=$((ok+1))
      ;;
    409)
      move_unique "$img" "$DUPLICATES" >/dev/null
      [[ -f "${img}.tag" ]] && move_unique "${img}.tag" "$DUPLICATES" >/dev/null
      echo "SKIP (already uploaded; moved to $DUPLICATES)"; dup=$((dup+1))
      ;;
    422)
      dest="$(move_unique "$img" "$FAILED")"
      { echo "No cards were ingested from this photo."; echo; cat "$resp_file"; echo; } >"${dest}.txt"
      [[ -f "${img}.tag" ]] && move_unique "${img}.tag" "$FAILED" >/dev/null
      echo "NO CARDS (moved to $FAILED; see ${dest}.txt)"; nocards=$((nocards+1))
      ;;
    *)
      echo "FAIL (ingest HTTP $code)"; fail=$((fail+1))
      ;;
  esac
  rm -f "$json_file" "$resp_file" "$view"
done

echo "Done. $ok ingested, $dup already uploaded, $nocards with no cards, $fail failed (left in place to retry)."
echo "Review and add them at ${URL}/"
