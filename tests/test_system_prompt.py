"""System prompt 教学段验证 —— evidence chain ref token 协议 + 禁止 [N] 脚注。

ref token 协议见 spec/requirements/evidence-chain.md；本测试盯死 prompt
包含契约关键词，避免后续 prompt 重构无意删除。

v5.1 重写（commit 1622450 后续）:prompt 改为中文陈述 + 删 id 字段
（v5.1 协议变更）。中文关键词匹配宽松（部分术语双语并列，避免被
表述微调击穿）。
"""
from core.config import Config
from run import DEFAULT_SYSTEM_TEMPLATE, render_default_system


def _prompt() -> str:
    """渲染 system prompt;用 cfg 默认值,避免拉真实路径。"""
    return render_default_system(Config())


# ---------------------------------------------------------------------------
# 段存在性：v5.1 中文标题 + 协议关键词
# ---------------------------------------------------------------------------


def test_section_heading_present():
    """v5.1: prompt 用中文段标题 '## 证据链'."""
    assert "## 证据链" in _prompt()


def test_section_heading_in_template_constant():
    """模板常量本身必须含段（避免运行时动态注入带来的差异）."""
    assert "## 证据链" in DEFAULT_SYSTEM_TEMPLATE


def test_token_syntax_documented():
    """v5.1: token 协议删了 id=<positive int> 必填要求."""
    p = _prompt()
    assert "[[ref type=" in p, "missing [[ref type= syntax anchor"
    # v5.1 协议删 id= 必填字段 —— 不应该再有 `id=<positive int>` 这种带等号的强制约束
    assert "id=<positive int>" not in p, (
        "prompt should not require id=<positive int> (v5.1 removed id field)"
    )


def test_four_common_types_listed():
    """link / memory / snippet / tool 四种 type 必须在常用 type 表里."""
    for t in ("link", "memory", "snippet", "tool"):
        assert f"type={t}" in _prompt(), f"type {t!r} missing from prompt"


def test_required_fields_documented():
    """v5.1: 字段名跟 parser 对齐:
    link: url, title, desc
    memory: title, key, snippet
    snippet: title, from, content
    tool: title, tool_name, call_id, result_summary
    """
    p = _prompt()
    # link
    assert 'url="' in p and 'title="' in p and 'desc="' in p
    # memory
    assert 'key="' in p and 'snippet="' in p
    # snippet
    assert 'from="' in p and 'content="' in p
    # tool
    assert 'tool_name="' in p and 'call_id="' in p and 'result_summary="' in p


def test_correct_example_present():
    """正确示例存在 —— worked example 用抽象通用例子,2-3 行够."""
    p = _prompt()
    # 例子用 `某来源 A` / `某观点的描述` 这种通用占位
    assert "[[ref type=link" in p


def test_bad_examples_present():
    """错误示例段必须存在 —— 缺字段 + 堆叠 + 表格内 ref."""
    p = _prompt()
    # 中文版用「不要」/「禁止」/「缺字段」之类措辞
    forbid_words = ["不要", "禁止", "不能"]
    assert any(w in p for w in forbid_words), (
        "missing anti-pattern wording; expected 中文禁止/不要/不能 任一"
    )


def test_when_to_emit_documented():
    """什么时候 emit 段必须存在 —— 关键结论 vs 常识."""
    p = _prompt()
    assert "关键结论" in p, "missing '关键结论' guidance"


def test_scope_boundary_documented():
    """ref 只能在 final answer 文本流,不能嵌在 table / code block 内."""
    p = _prompt()
    assert "table" in p.lower() or "表格" in p, "missing table prohibition"
    assert "code" in p.lower() or "代码" in p, "missing code block prohibition"


def test_hard_rule_emphasis():
    """'关键结论必须给出 ref' 是核心规则."""
    assert "关键结论" in _prompt() and "ref" in _prompt().lower()


def test_no_bracket_footnote_reminder():
    """禁止 LLM 输出 [1] [2] [3] 数字脚注 (Image 57 / Image 59 反馈)."""
    p = _prompt()
    # 显式提到 [1] [2] [3] + 禁止语义
    assert "[1]" in p and "[2]" in p and "[3]" in p
    assert any(w in p for w in ["不要", "禁止", "Do NOT"]), (
        "missing prohibition wording"
    )


def test_desc_not_url_advice():
    """Image 60: link type 的 desc 字段不要填 URL."""
    p = _prompt()
    seg_start = p.find("## 证据链")
    assert seg_start >= 0, "missing ## 证据链 section"
    seg = p[seg_start:]
    # 找 desc + url + 不要/NOT 上下文
    idx = 0
    while True:
        idx = seg.find("desc", idx)
        if idx < 0:
            break
        near = seg[idx : idx + 600]
        if "url" in near.lower() and any(
            w in near for w in ["不要", "不能", "not", "already", "已在", "已是"]
        ):
            return
        idx += 1
    raise AssertionError(
        "no guidance about 'desc should not be the URL' in 证据链 section"
    )


def test_no_ref_stacking_advice():
    """Image 63: 禁止 ref token 堆叠 [[ref]][[ref]][[ref]]."""
    p = _prompt()
    assert any(kw in p for kw in ["堆叠", "紧挨", "连续", "stack", "concatenat"]), (
        "missing stacking prohibition wording"
    )


def test_placement_restrictions_documented():
    """Image 63: ref 不能在表格/标题/代码块内."""
    p = _prompt()
    forbidden = ["表格", "标题", "代码块", "blockquote", "table", "head", "code"]
    missing = [kw for kw in forbidden if kw not in p]
    assert not missing, f"missing forbidden-position keywords: {missing}"


def test_credibility_emphasis():
    """Image 63: ref 可信度价值 —— 多用 / 引用来源 / 信任 / 溯源."""
    p = _prompt()
    cred_keywords = ["credib", "trust", "traceable", "可信", "信任", "溯源", "慷慨"]
    assert any(kw.lower() in p.lower() for kw in cred_keywords), (
        "missing ref credibility value emphasis"
    )


# ---------------------------------------------------------------------------
# 段位置（顺序）：Evidence Chain 在 Image preview 之后、Reading documents 之前
# ---------------------------------------------------------------------------


def test_after_image_preview():
    out = _prompt()
    # v5.1 中文标题:「## 证据链」
    assert out.index("## Image preview") < out.index("## 证据链"), (
        "证据链段必须在 Image preview 之后（两者都是输出格式约束）"
    )


def test_before_reading_documents():
    out = _prompt()
    assert out.index("## 证据链") < out.index(
        "## Reading documents"
    ), "证据链段必须在 Reading documents 之前"


def test_after_async_tasks():
    out = _prompt()
    assert out.index("## Async tasks") < out.index("## 证据链")


# ---------------------------------------------------------------------------
# 现有段不破：加 evidence chain 段不能破坏现有 prompt 内容
# ---------------------------------------------------------------------------


def test_image_preview_section_intact():
    assert "## Image preview" in _prompt()
    assert "![油画](<https://dashscope-a717.oss-accelerate" in _prompt()


def test_async_tasks_section_intact():
    assert "## Async tasks (fork / poll / cancel)" in _prompt()
    assert "fork_task" in _prompt()


def test_bash_target_section_intact():
    assert "## bash tool `target` field (MonoDesk display)" in _prompt()
    assert "target" in _prompt()


def test_reading_documents_section_intact():
    assert "## Reading documents" in _prompt()
    assert "read_doc" in _prompt()


def test_image_preview_show_dont_describe_intact():
    assert "## Image preview — show, don't just describe" in _prompt()


# ---------------------------------------------------------------------------
# 模板渲染不变量
# ---------------------------------------------------------------------------


def test_evidence_chain_survives_render():
    """占位符替换后段还在."""
    out = render_default_system(Config())
    assert "## 证据链" in out
    assert "[[ref type=" in out


def test_no_unreplaced_placeholders():
    """render 后所有 {MONOX_*} 占位符都被替换成真实路径."""
    out = render_default_system(Config())
    assert "{MONOX_" not in out, "render_default_system 应替换所有 MONOX_* 占位符"


def test_default_template_is_string():
    """模板必须是 string,方便 format()."""
    assert isinstance(DEFAULT_SYSTEM_TEMPLATE, str)