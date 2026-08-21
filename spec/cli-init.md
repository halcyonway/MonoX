# CLI 工具跨机器部署指南

本文档说明 MonoX 依赖的外部 CLI 工具如何在不同机器上初始化，解决"换机器/重装后 CLI 不可用"的问题。

---

## 环境要求

- Node.js ≥ 18（建议 20+）
- Python ≥ 3.9
- uv（项目包管理器）

## npm 全局路径配置

所有 npm 全局包安装在 `~/.npm-global/`，需要将以下路径加入 shell 配置：

```bash
export PATH="$HOME/.npm-global/bin:$PATH"
```

### macOS（zsh）

```bash
# ~/.zshrc 或 ~/.zprofile
export PATH="$HOME/.npm-global/bin:$PATH"
```

### Linux（zsh/bash）

```bash
# ~/.zshrc 或 ~/.bashrc
export PATH="$HOME/.npm-global/bin:$PATH"
```

### Windows（Git Bash / WSL）

```bash
# ~/.bashrc 或 ~/.profile
export PATH="$HOME/.npm-global/bin:$PATH"
```

### 验证

```bash
source ~/.zshrc   # 或对应的 shell 配置文件
echo $PATH | tr ':' '\n' | grep npm
```

---

## 1. lark-cli（飞书官方 CLI）

官方工具链，支持消息、日历、文档、审批等 11 个业务域，200+ 命令，内置 19 个 AI Agent Skills。

### 安装

```bash
# 标准安装（需要 npm 官方源可访问）
npm install -g @larksuite/cli

# 国内镜像（推荐）
npm install -g @larksuite/cli --registry=https://registry.npmmirror.com
```

### 验证

```bash
lark-cli --version   # 应输出 1.0.x
lark-cli --help
```

### 初始化授权

```bash
# 初始化配置（生成授权二维码）
lark-cli config init --new

# 登录授权
lark-cli auth login --recommend

# 检查授权状态
lark-cli auth status

# 健康检查
lark-cli doctor
```

### 常用命令

```bash
lark-cli im message send_text --token <token> --content '{"text":"hello"}'
lark-cli calendar +agenda
lark-cli docs document --help
```

### 获取帮助

```bash
lark-cli <domain> --help          # 查看某个域的所有命令
lark-cli schema <service>.<resource>.<method>   # 查看 API 参数
lark-cli api GET /open-apis/...   # 原始 HTTP 请求
```

---

## 2. feishu-mcp-server（飞书 MCP Server）

MCP 协议服务器，通过 MCP 协议访问飞书文档（知识库/云文档）。

### 安装

```bash
npm install -g feishu-mcp-server --registry=https://registry.npmmirror.com
```

### CLI 命令

```bash
feishu-mcp --help           # 查看帮助
feishu-mcp start-server --help   # 查看启动参数
```

### 启动方式

```bash
# SSE 模式（本地 HTTP 暴露）
feishu-mcp start-server --app_id <app_id> --app_secret <app_secret> --sse --port 8080

# WebSocket 模式（默认）
feishu-mcp start-server --app_id <app_id> --app_secret <app_secret>
```

### 卸载

```bash
npm rm -g feishu-mcp-server
```

---

## 3. uv（Python 包管理器）

MonoX 用 uv 管理 Python 依赖。

### 安装

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

或者通过 pip：

```bash
pip install uv
```

### 验证

```bash
uv --version
```

---

## 4. Node.js（前端运行时）

如果系统没有 Node.js，或版本 < 18，推荐用 nvm 管理。

### 安装 nvm

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
```

### 安装 Node.js

```bash
nvm install 20     # 安装 Node 20（LTS）
nvm use 20        # 使用 Node 20
nvm alias default 20
```

### 验证

```bash
node --version    # v20.x.x
npm --version     # 10.x.x
```

---

## 新机器一键初始化脚本

```bash
#!/bin/bash
set -e

# 1. Node.js（通过 nvm）
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
nvm install 20 && nvm use 20 && nvm alias default 20

# 2. npm 全局路径
mkdir -p "$HOME/.npm-global"
npm config set prefix '~/.npm-global'

# 3. PATH 写入 shell 配置
SHELL_RC="$HOME/.zshrc"
if [ -n "$(grep -s 'npm-global' "$SHELL_RC")" ]; then
    echo "npm-global already in $SHELL_RC"
else
    echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$SHELL_RC"
fi
export PATH="$HOME/.npm-global/bin:$PATH"

# 4. 安装 CLI 工具
npm install -g @larksuite/cli --registry=https://registry.npmmirror.com
npm install -g feishu-mcp-server --registry=https://registry.npmmirror.com

# 5. uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 6. MonoX 依赖
cd /path/to/MonoX
uv sync

echo "=== 初始化完成 ==="
echo "Node: $(node --version)"
echo "npm: $(npm --version)"
echo "lark-cli: $(lark-cli --version)"
echo "uv: $(uv --version)"
```

---

## 故障排查

### `lark-cli: command not found`

```bash
# 确认 PATH
echo $PATH | tr ':' '\n' | grep npm
# 手动 export 后重试
export PATH="$HOME/.npm-global/bin:$PATH"
lark-cli --version
```

### `feishu-mcp: command not found`

同上，feishu-mcp 的 CLI 名称是 `feishu-mcp`（不是 `feishu-mcp-server`）。

### npm 安装报 EACCES 权限错误

不要用 `sudo npm install -g`，改为配置用户级全局目录：

```bash
mkdir -p "$HOME/.npm-global"
npm config set prefix '~/.npm-global'
export PATH="$HOME/.npm-global/bin:$PATH"
```

### 授权失败

```bash
lark-cli auth status   # 查看当前身份
lark-cli auth login --recommend   # 重新授权
lark-cli doctor        # 健康检查
```

---

## 相关文档

- 飞书开放平台：https://open.feishu.cn/
- lark-cli GitHub：https://github.com/larksuite/cli
- feishu-mcp-server：https://github.com/hankeyyh/feishu-mcp-server
