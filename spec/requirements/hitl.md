# hitl: 敏感操作人工确认

## 问题

bash tool 拿到 LLM 的 cmd 直接交给 `BashRunner.run()` 执行，无任何审查。LLM 误判 / prompt injection / 用户自己的「危险模式」全部无法拦截：

- `rm -rf /` / `rm -rf ~` / `chmod 777 /` 一类破坏性
- `curl ... | bash` / `wget ... | sh` 一类远程代码执行
- `git push --force` / `git reset --hard` 一类 git 破坏性
- `:(){:|:&};:` 一类 fork bomb
- `.env` / `~/.ssh/` / `id_rsa` 一类敏感文件读写

## 现状（v0.9）

- `core/loop/tools/bash.py` 直接 `runner.run(cmd, cwd, timeout)`，无 dangerous 检查
- 无 HITL hook

## 设计（模式匹配 + channel 拦截）

### 两层防护

**Layer 1：模式匹配（必加）**

core 内置 dangerous_patterns 列表（regex），命中即拒绝 tool_result.status="rejected"，不真执行：

```python
DANGEROUS_PATTERNS = [
    # 破坏性 rm
    (r"\brm\s+(-[rfRF]+\s+)*[/~]", "recursive delete on root/home"),
    # 远程代码执行
    (r"(curl|wget).*\|\s*(bash|sh)\b", "pipe remote to shell"),
    # git 破坏性
    (r"\bgit\s+push\s+(-f|--force(?!-with-lease))", "force push"),
    (r"\bgit\s+reset\s+--hard", "hard reset"),
    # fork bomb
    (r":\s*\(\s*\)\s*\{", "fork bomb pattern"),
    # 权限放大
    (r"\bchmod\s+(-R\s+)?777\b", "world-writable"),
    # 敏感路径
    (r"(~/?\.ssh|id_rsa|\.env\b)", "sensitive path"),
]

def check_dangerous(cmd: str) -> str | None:
    """返回第一个命中的描述；None = 安全。"""
```

- 命中后 `ToolResult(status="rejected", stderr="rejected: <pattern>: <cmd>")`，LLM 收到错误自己改
- 列表可由 `config.toml [bash] extra_dangerous_patterns = [...]` 追加
- 列表**不**走 LLM：纯 regex，避免二次失败

**Layer 2：HITL（可选 / 危险等级更高时）**

对「用户没明确说要做的破坏性操作」（如 rm 子目录）走 HITL：

- bash tool execute 时若 `cmd` 命中 high_risk 列表，**不**直接执行，先 `ToolResult(status="hitl_pending", ...)` 返回 LLM
- 同时 engine 透过 `output_queue` 发一个 `HitlRequest(tool_call_id, cmd, reason)` 事件
- channel（terminal / textual / feishu）收到事件 → 弹 prompt（terminal: y/n；textual: confirm dialog；feishu: reply「确认」/「取消」）
- 用户回复后 channel 调 `hitl.respond(call_id, approved: bool)`
- engine 收到 respond 后重发原 tool_call，approved → 执行；rejected → 返回 rejected

### 接口

新增 protocol：

```python
class HitlGate(Protocol):
    async def request(self, call_id: str, cmd: str, reason: str) -> bool: ...
```

默认实现 `LocalHitlGate`：通过 `output_queue` 发 `HitlRequest`，等 `HitlResponse` 事件。

```python
@dataclass(frozen=True)
class HitlRequest(StreamEvent):
    call_id: str
    cmd: str
    reason: str

@dataclass(frozen=True)
class HitlResponse:
    call_id: str
    approved: bool
```

### 配置文件

```toml
[bash]
dangerous_patterns_extra = [
    "\\bdd\\s+if=/dev/(zero|urandom).*of=/dev/sd",  # 覆盖硬盘
]
high_risk_patterns = [
    "\\brm\\s+-rf\\b",     # HITL 二次确认
]
disabled = false          # 完全关掉 bash tool（最严模式）
```

### 工具自带 HITL bypass

`core` 不感知，但 `run.py` 装配时若检测到「dev/safe mode」，可注入 `PermissiveHitlGate`（auto-approve）。避免 dev 体验被破坏。

## 验证

- 单测：每个 dangerous_pattern 命中 → tool_result rejected
- 单测：high_risk_pattern + LocalHitlGate mock approve → 真执行；reject → rejected
- e2e：mock channel 收到 HitlRequest；approve 后 tool 真跑
- 回归：原有 e2e 4/4 不破

## 风险

- regex 误杀（合法 `rm` 子目录被拦）→ high_risk 走 HITL 而不是直接拒绝，让用户决断
- regex 漏杀（新攻击 pattern 没入库）→ 列表要扩展；社区贡献
- HITL 在 IM channel（feishu）的 UX 差，可能让用户忽略 → 用 emoji / @ 提醒

## 进度

- 设计：本文档
- 实现：未开始（中期目标）