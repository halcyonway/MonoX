#!/usr/bin/env bash
# install.sh — 一次性本地准备。
# - 建 $MONOX_HOME/{workspace,memory,state,traces,skills,tmp}（默认 ./.monox）
# - 把 extensions/skills/* 拷到 skills/（仅当 skills 为空时）
# - 把 extensions/cli/inner/exec_cli.py 拷到 ~/.local/bin/exec_cli
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

# Install exec_cli shell client
EXEC_CLI_SRC="$(cd "$(dirname "$0")/.." && pwd)/extensions/cli/inner/exec_cli.py"
INSTALL_BIN_DIR="${HOME}/.local/bin"
INSTALL_BIN="${INSTALL_BIN_DIR}/exec_cli"
if [ -f "$EXEC_CLI_SRC" ]; then
    mkdir -p "$INSTALL_BIN_DIR"
    # 拷贝（不软链）：server source 位置变了也不会断
    cp "$EXEC_CLI_SRC" "$INSTALL_BIN"
    chmod +x "$INSTALL_BIN"
    echo "Installed exec_cli → $INSTALL_BIN"

    # PATH 警告（不自动 export——install.sh 不假设 shell 类型）
    case ":$PATH:" in
        *":${INSTALL_BIN_DIR}:"*) ;;
        *)
            echo "WARNING: ${INSTALL_BIN_DIR} not in PATH; add it or use full path \`${INSTALL_BIN}\`"
            ;;
    esac
fi

cat <<EOF

Done.

Next:
  export MINIMAX_API_KEY=<your-key>     # or OPENAI_API_KEY / DEEPSEEK_API_KEY etc.
  uv run python run.py

\`run.py\` 自动起三个子服务（health :8767 / debug :8768 / CLI server :8769），
跟 Runtime 同生命周期——\`run.py --stop\` 一起收。CLI server 让 LLM 能用
\`exec_cli mono_search\` / \`mono_i2i\` / \`mono_asr\` 等原子能力，install.sh
不负责启动任何 server。

(Edit config.toml if api_base / api_key / model don't match your provider.)

注意目录布局：
  workspace/<session_key>/   ← LLM shell cwd（per-session）
  memory/                    ← 长期记忆（跨会话全局：Memory.md + notes/）
  state/<session_key>/       ← Runtime 内部 state（LLM 不可见）
  traces/<session_key>/      ← 可观测 trace
EOF