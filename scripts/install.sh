#!/usr/bin/env bash
# install.sh — 一次性本地准备。
# - 建 $MONOX_HOME/{workspace,memory,state,traces,skills,tmp}（默认 ./.monox）
# - 把 extensions/skills/* 拷到 skills/（仅当 skills 为空时）
# - .monox/memory/Memory.md 不在这里预建——run.py 启动时写 starter，install 时不必管
set -euo pipefail

ROOT="${MONOX_HOME:-$PWD/.monox}"
echo "MonoX home: $ROOT"

mkdir -p "$ROOT"/workspace "$ROOT"/memory "$ROOT"/state "$ROOT"/traces "$ROOT"/skills "$ROOT"/tmp

if [ -z "$(ls -A "$ROOT/skills" 2>/dev/null)" ]; then
    SKILLS_SRC="$(cd "$(dirname "$0")/.." && pwd)/extensions/skills"
    if [ -d "$SKILLS_SRC" ]; then
        cp -r "$SKILLS_SRC/"* "$ROOT/skills/"
        echo "Installed default skills → $ROOT/skills/"
    fi
fi

# 安装 exec_cli（extension CLI HTTP 客户端）到 ~/.local/bin/
# 让 LLM（和人类）通过 `exec_cli mono_search "..."` 调用 extension 能力。
# 只 install，不启动 CLI server —— server 由用户手动或 supervisor 拉起。
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXEC_CLI_SRC="$ROOT_DIR/extensions/cli/inner/exec_cli.py"
INSTALL_BIN_DIR="${HOME}/.local/bin"
INSTALL_BIN="$INSTALL_BIN_DIR/exec_cli"
if [ -f "$EXEC_CLI_SRC" ]; then
    mkdir -p "$INSTALL_BIN_DIR"
    cp "$EXEC_CLI_SRC" "$INSTALL_BIN"
    chmod +x "$INSTALL_BIN"
    echo "Installed exec_cli → $INSTALL_BIN"
    case ":$PATH:" in
        *":$INSTALL_BIN_DIR:"*) ;;
        *) echo "  (note: $INSTALL_BIN_DIR is not on PATH; add it or use full path)" ;;
    esac
else
    echo "WARN: exec_cli source not found at $EXEC_CLI_SRC — skipping install"
fi

cat <<EOF

Done.

Next:
  # (1) LLM API key（runtime 用）
  export MINIMAX_API_KEY=<your-key>     # or OPENAI_API_KEY / DEEPSEEK_API_KEY etc.
  uv run python run.py

  # (2) Extension CLI（如 search）— 手动启动 server（一次性）
  uv run python -m extensions.cli.inner.server &
  export BOCHA_API_KEY=<your-bocha-key>   # 任何用 mono_search 的子命令需要这个

(Edit config.toml if api_base / api_key / model don't match your provider.)

注意目录布局：
  workspace/<session_key>/   ← LLM shell cwd（per-session）
  memory/                    ← 长期记忆（跨会话全局：Memory.md + notes/）
  state/<session_key>/       ← Runtime 内部 state（LLM 不可见）
  traces/<session_key>/      ← 可观测 trace
EOF
