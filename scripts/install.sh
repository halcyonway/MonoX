#!/usr/bin/env bash
# install.sh — 一次性本地准备。
# - 建 $MONOX_HOME/{workspace,memory,skills,tmp}（默认 ./.monox）
# - 把 extensions/skills/* 拷到 skills/（仅当 skills 为空时）
set -euo pipefail

ROOT="${MONOX_HOME:-$PWD/.monox}"
echo "MonoX home: $ROOT"

mkdir -p "$ROOT"/workspace "$ROOT"/memory "$ROOT"/skills "$ROOT"/tmp

if [ -z "$(ls -A "$ROOT/skills" 2>/dev/null)" ]; then
    SKILLS_SRC="$(cd "$(dirname "$0")/.." && pwd)/extensions/skills"
    if [ -d "$SKILLS_SRC" ]; then
        cp -r "$SKILLS_SRC/"* "$ROOT/skills/"
        echo "Installed default skills → $ROOT/skills/"
    fi
fi

cat <<EOF

Done.

Next:
  export MINIMAX_API_KEY=<your-key>     # or OPENAI_API_KEY / DEEPSEEK_API_KEY etc.
  uv run python run.py

(Edit config.toml if api_base / api_key / model don't match your provider.)
EOF