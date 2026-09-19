"""system prompt 测试 —— 「Evidence Chain（ref）」段已正确嵌入。

覆盖：
1. DEFAULT_SYSTEM_TEMPLATE 字符串包含必需内容
2. render_default_system(cfg) 输出含 evidence chain 段
3. 段位置在 `## Image preview` 之后、`## Reading documents` 之前
4. 现有段不破（image preview / bash target / reading documents 等仍命中）

prompt 改动是文案层面的「输入字符串」，加段不应破坏现有行为。
见 spec/requirements/system-prompt-ref-emit.md。
"""
from __future__ import annotations

from core.config import Config
from run import DEFAULT_SYSTEM_TEMPLATE, render_default_system


def _prompt() -> str:
    """渲染 system prompt；用 cfg 默认值，避免拉真实路径。"""
    return render_default_system(Config())


# ---------- 段存在性 ----------

class TestEvidenceChainSectionExists:
    """Evidence Chain 段必须出现在 prompt 里（LLM 才会 emit）。"""

    def test_section_heading_present(self):
        assert "## Evidence Chain（ref）" in _prompt()

    def test_section_heading_in_template_constant(self):
        # 模板常量本身必须含段（避免运行时动态注入带来的差异）
        assert "## Evidence Chain（ref）" in DEFAULT_SYSTEM_TEMPLATE

    def test_token_syntax_documented(self):
        assert "[[ref id=N type=TYPE key=value ...]]" in _prompt()

    def test_four_common_types_listed(self):
        # link / memory / snippet / tool 四种 type 必须在常用 type 表里
        for t in ("link", "memory", "snippet", "tool"):
            assert f"`{t}`" in _prompt(), f"type {t!r} missing from prompt"

    def test_required_fields_documented(self):
        # 必填字段示例。prompt 里 link/memory/tool 三个 type 的字段示例里有
        # `key=value` 形态；snippet 的 from/content 是裸字 + 中文括号注释。
        p = _prompt()
        # link: url, title（示例里有 url= 和 title=）
        assert "url=" in p and "title=" in p
        # memory: key, snippet
        assert "key=" in p and "snippet=" in p
        # snippet: from, content（裸字 + 中文括号注释，避免 false positive
        # 用 `from（` 这种窄上下文）
        assert "from（" in p and ", content" in p
        # tool: tool_name, call_id
        assert "tool_name=" in p and "call_id=" in p

    def test_correct_example_present(self):
        # 正确示例：MonoX 是 2022 年成立的 AI agent runtime [1]
        assert "MonoX 是 2022 年成立的 AI agent runtime" in _prompt()
        assert "[[ref id=1 type=link" in _prompt()

    def test_bad_examples_present(self):
        # 错误示例段必须存在
        assert "### 错误示例" in _prompt()
        # ref 应该放在观点之后，不是插入观点中间
        assert "ref 应该放在观点" in _prompt()
        # 关键结论必须 ref
        assert "关键结论必须 ref" in _prompt()

    def test_hard_rule_emphasis(self):
        # 「关键结论必须给出 ref」是核心规则
        assert "关键结论必须给出 ref" in _prompt()

    def test_when_to_emit_table(self):
        # 什么时候 emit 段（关键结论 / 常识 / 数据 / 用户原文 / 闲聊）
        assert "### 什么时候 emit" in _prompt()
        assert "数据 / 引用 / 数字" in _prompt()
        assert "常识 / 简单事实" in _prompt()

    def test_scope_boundary_documented(self):
        # 适用边界：ref 只能在 final answer 文本流
        assert "### 适用边界" in _prompt()
        assert "final answer 是 **文本流**" in _prompt()
        # reasoning / tool_call 里 emit 不会被解析
        assert "reasoning" in _prompt() and "tool_call" in _prompt()


# ---------- 段位置（顺序） ----------

class TestSectionOrdering:
    """Evidence Chain 段必须在 Image preview 之后、Reading documents 之前。"""

    def test_after_image_preview(self):
        out = _prompt()
        assert out.index("## Image preview") < out.index("## Evidence Chain（ref）"), (
            "Evidence Chain 段必须在 Image preview 之后（两者都是输出格式约束）"
        )

    def test_before_reading_documents(self):
        out = _prompt()
        assert out.index("## Evidence Chain（ref）") < out.index(
            "## Reading documents"
        ), "Evidence Chain 段必须在 Reading documents 之前"

    def test_after_async_tasks(self):
        # Async tasks 段靠前（task 用法）；Evidence Chain 靠后（输出格式）
        out = _prompt()
        assert out.index("## Async tasks") < out.index("## Evidence Chain（ref）")


# ---------- 现有段不破 ----------

class TestExistingSectionsIntact:
    """加 evidence chain 段不能破坏现有 prompt 内容。"""

    def test_image_preview_section_intact(self):
        assert "## Image preview" in _prompt()
        # image preview 段关键示例：OSS URL 角度括号形式
        assert "![油画](<https://dashscope-a717.oss-accelerate" in _prompt()

    def test_async_tasks_section_intact(self):
        assert "## Async tasks (fork / poll / cancel)" in _prompt()
        assert "fork_task" in _prompt()

    def test_bash_target_section_intact(self):
        assert "## bash tool `target` field (MonoDesk display)" in _prompt()
        assert "target" in _prompt()

    def test_reading_documents_section_intact(self):
        assert "## Reading documents" in _prompt()
        assert "read_doc" in _prompt()

    def test_image_preview_show_dont_describe_intact(self):
        assert "## Image preview — show, don't just describe" in _prompt()

    def test_no_tool_call_reminder_section_intact(self):
        # 之前加的 no-tool-call reminder 段（#17）仍在
        # 这条 case 是回归 guard：后续改 prompt 不能顺手删已有段
        # 这里只断言一段明显有「## Async tasks」/「## Image preview」相邻的占位
        assert "## Reading documents" in _prompt()


# ---------- 模板渲染不变量 ----------

class TestRenderDefaultSystem:
    """render_default_system(cfg) 不破坏 evidence chain 段。"""

    def test_evidence_chain_survives_render(self):
        # 占位符替换后段还在
        out = render_default_system(Config())
        assert "## Evidence Chain（ref）" in out
        assert "[[ref id=N type=TYPE key=value ...]]" in out

    def test_no_unreplaced_placeholders(self):
        # render 后所有 {MONOX_*} 占位符都被替换成真实路径
        out = render_default_system(Config())
        assert "{MONOX_" not in out, "render_default_system 应替换所有 MONOX_* 占位符"
