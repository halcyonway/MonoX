#!/usr/bin/env bash
# save_note.sh <name> — 把 stdin 追加到 notes/<name>
set -euo pipefail

NAME="${1:?usage: save_note.sh <name>}"

MEM_ROOT="${MONOX_MEMORY_ROOT:-/var/agent/memory}"
SESSION="${MONOX_SESSION_KEY:-default}"

NOTES_DIR="$MEM_ROOT/$SESSION/notes"
mkdir -p "$NOTES_DIR"

cat >> "$NOTES_DIR/$NAME"
echo "[memory-write] appended to $NAME"