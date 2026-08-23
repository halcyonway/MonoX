"""Context 组装 + L1 工具结果压缩。

注意：tool 消息的 content 现在用 XML event 格式（见 core.loop.event_format），
不是 JSON 字符串。XML schema 文档会注入 system prompt，让 LLM 知道怎么读。
"""
from __future__ import annotations

import uuid
from typing import Any

from core.loop.tools.read_tr_budget import ReadToolResultBudgetTool
from core.protocol import ToolResult
from core.skill_service import SkillService


L1_TRUNCATE_LEN = 4000


def memory_section(path_vars: dict[str, str]) -> str:
    """System prompt 的 `## Memory` section。

    路径用 `{MONOX_*}` 占位符，调用方负责 `.format(**path_vars)` 一次替换。
    替换值来自 `cfg.sandbox`——同一份 config 控制所有路径，prompt 不硬编码。
    """
    return """\

## Memory

`{MONOX_MEMORY_DIR}/Memory.md` (injected above, under this section) is your long-term
**cross-session** memory. Its body is a sparse index: each line `- topic: notes/x.md`
points to a detail file in `{MONOX_MEMORY_DIR}/notes/`.

**Read**: `{MONOX_MEMORY_DIR}/Memory.md` is already injected. To fetch a specific topic's
detail file, use Bash (`cat {MONOX_MEMORY_DIR}/notes/<topic>.md`).

**Write** — low-frequency, explicit only:
- The user says 记住 / remember / save this / 别忘了, **or**
- You learn a durable preference, project convention, or gotcha worth keeping across sessions.

To remember: write the detail file under `{MONOX_MEMORY_DIR}/notes/`, then append one line
to the index file at `{MONOX_MEMORY_DIR}/Memory.md`.

Do **not** auto-summarize the conversation, do not write ephemeral task state, do not write raw data.
""".format(**path_vars)


def assemble_messages(
    system: str,
    memory_index: str,
    skill_service: SkillService | None,
    messages: list[dict[str, Any]],
    path_vars: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """拼装最终发给 LLM 的 messages。

    `path_vars` 来源于 `cfg.sandbox`，作为模板 `{MONOX_*}` 的替换表——同一份 config
    既注入 prompt（通过占位符替换）又让 bash 子进程可见（通过路径直接落在 cwd 下的子目录）。
    state/traces **不**出现在这里——它们是 Runtime 内部 state，不让 LLM 看见。

    Skills：把 `SkillService` 实例传进来，每 turn `service.format_l1_prompt_section()`
    重新扫文件系统（agent 在 session 内创建 skill 后下一个 turn 立即生效）。
    `skill_service=None` 时不注入 Skills section（向后兼容测试）。
    """
    if path_vars is None:
        path_vars = {}
    parts: list[str] = [system, memory_section(path_vars)]
    if memory_index:
        parts.append(f"\n# Memory index\n{memory_index}")
    if skill_service is not None:
        skills_section = skill_service.format_l1_prompt_section_resolved(
            path_vars.get("MONOX_SKILLS_DIR", "")
        )
        if skills_section:
            parts.append(skills_section)
    return [{"role": "system", "content": "".join(parts)}] + list(messages)


def compress_tool_result(
    result: ToolResult,
    budget: ReadToolResultBudgetTool,
    limit: int = L1_TRUNCATE_LEN,
) -> ToolResult:
    if len(result.stdout) <= limit and len(result.stderr) <= limit:
        return result

    budget_id = uuid.uuid4().hex[:12]
    budget.put(budget_id, result)
    half = limit // 2

    def _truncate(text: str) -> str:
        return (
            f"[L1 compressed, full version requires read_tool_result_budget(budget_id='{budget_id}')]\n"
            f"{text[:half]}\n...\n{text[-half:]}"
        )

    return ToolResult(
        call_id=result.call_id,
        status=result.status,
        stdout=_truncate(result.stdout) if len(result.stdout) > limit else result.stdout,
        stderr=_truncate(result.stderr) if len(result.stderr) > limit else result.stderr,
        exit_code=result.exit_code,
        artifacts=result.artifacts,
        truncated=True,
        budget_id=budget_id,
    )


def format_tool_message(result: ToolResult, *, tool: str | None = None) -> str:
    """DEPRECATED：tool 消息 content 现在由 core.loop.event_format.tool_result_event_xml 生成。

    保留仅为向后兼容测试。新代码请用 tool_result_event_xml(call_id, result, tool=tool_name)。
    """
    from core.loop.event_format import tool_result_event_xml
    return tool_result_event_xml("", result, tool=tool)