#!/usr/bin/env bash
# Continuous fuzzing: run the campaign, then upload any new panic corpus to the
# "incoming" release and trigger the automated triage workflow.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$script_dir"

set +e
"${PYTHON:-python3}" -m servo_fuzzing.campaign "$@"
campaign_status=$?
set -e

CORPUS="target/panic-corpus"
ASSET="target/new-panic-corpus.tar.gz"
ASSET_NAME="$(basename "$ASSET")"
REPO="tannal/servo-fuzzing"
TAG="incoming"

if [ "${SKIP_UPLOAD:-0}" = "1" ]; then
    echo "SKIP_UPLOAD=1; leaving the corpus in $CORPUS without uploading."
    exit "$campaign_status"
fi

if [ ! -d "$CORPUS" ] || ! find "$CORPUS" -name '*.html' -print -quit | grep -q .; then
    echo "No new panic corpus in $CORPUS; skipping upload."
    exit "$campaign_status"
fi

echo "Packaging new panic corpus into $ASSET ..."
tar -czf "$ASSET" -C "$CORPUS" .

test -s "$ASSET" || { echo "Archive not found: $ASSET"; exit 1; }
tar -tzf "$ASSET" >/dev/null || { echo "Invalid archive: $ASSET"; exit 1; }

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    gh release upload "$TAG" "$ASSET" --repo "$REPO" --clobber
else
    gh release create "$TAG" "$ASSET" --repo "$REPO" --prerelease \
      --title "Incoming Crash Bundle" \
      --notes "Temporary crash corpus for automated triage."
fi

gh workflow run import-crashes.yml --repo "$REPO" \
  -f release_tag="$TAG" \
  -f asset_name="$ASSET_NAME"

gh run list --repo "$REPO" --workflow import-crashes.yml --limit 3

exit "$campaign_status"
