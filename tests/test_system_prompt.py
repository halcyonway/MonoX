"""System prompt 教学段验证 —— evidence chain ref token 协议 + 禁止 [N] 脚注。

ref token 协议见 spec/requirements/evidence-chain.md；本测试盯死 prompt
包含契约关键词，避免后续 prompt 重构无意删除。

v5.1 重写：prompt 改为中文陈述 + 删 id 字段（v5.1 协议变更）。
中文关键词匹配宽松（部分术语双语并列，避免被表述微调击穿）。
"""
from core.config import Config
from run import DEFAULT_SYSTEM_TEMPLATE, render_default_system


def _prompt() -> str:
    # 用最小 Config 跑 render_default_system，验证替换后内容完整
    cfg = Config.from_dict({
        "sandbox": {
            "workspace_root": "/tmp/monoxx_ws",
            "memory_root": "/tmp/monoxx_mem",
            "skills_root": "/tmp/monoxx_skills",
            "tmp_root": "/tmp/monoxx_tmp",
            "state_root": "/tmp/monoxx_state",
            "traces_root": "/tmp/monoxx_traces",
        },
    })
    return render_default_system(cfg)


def test_prompt_has_evidence_chain_section():
    """prompt 含「证据链」段（v5.1 中文标题）:教 LLM 输出 ref token."""
    p = _prompt()
    assert "## 证据链" in p, "missing ## 证据链 section header"


def test_prompt_teaches_ref_token_syntax():
    """prompt 包含 4 种 type 的 token 形式（link/memory/snippet/tool）.

    v5.1: 协议删了 id= 字段（LLM 不需要递增计数），但 type=... 仍然必填。
    这里只断言 4 种 type 关键词 + 关键字段名都在 prompt 里。
    """
    p = _prompt()
    # 4 种 type 必填
    for kw in [
        "type=link",
        "type=memory",
        "type=snippet",
        "type=tool",
    ]:
        assert kw in p, f"missing type keyword: {kw!r}"
    # 关键字段名（chip 展示 + hover 内容）
    for kw in [
        'url="',
        'title="',
        'key="',
        'snippet="',
        'from="',
        'content="',
        'tool_name="',
        'call_id="',
    ]:
        assert kw in p, f"missing field keyword: {kw!r}"


def test_prompt_prohibits_id_field():
    """v5.1 新增: 协议删 id= 字段,LLM 不需要手填。prompt 必须明确说明
    token 不含 id（避免 LLM 习惯性加 id=N 破坏协议）。"""
    p = _prompt()
    # 不应再要求 id 字段
    # 在「证据链」段内不该有 `id=<positive int>` 这种带等号的强制约束
    seg_start = p.find("## 证据链")
    assert seg_start >= 0
    seg = p[seg_start:]
    assert "id=<positive int>" not in seg, (
        "prompt should not require id=<positive int> (v5.1 protocol removed id field)"
    )


def test_prompt_prohibits_bracket_footnote():
    """prompt 明确禁止 LLM 输出 [1] [2] [3] 数字脚注（Image 57 / Image 59 反馈）."""
    p = _prompt()
    # 中文版用「不要用」/「不要」/「禁止」之类措辞 + 显式提到 [1] [2] [3]
    assert "[1]" in p and "[2]" in p and "[3]" in p, (
        "missing explicit [N] bracket footnote examples"
    )
    # 必须有禁止语义（中文双语都接受）
    forbid_words = ["不要用", "不要", "禁止", "Do NOT use"]
    assert any(w in p for w in forbid_words), (
        f"missing prohibition wording; expected one of {forbid_words}"
    )


def test_prompt_advises_against_desc_url():
    """prompt 教 LLM: link type 的 desc 字段不要填 URL（Image 60 反馈）.

    v5.1 中文: 「不要把 url 自己填进 content,url 已在 chip 的跳转链接里」。
    测试检测「证据链」段里有 desc + url + 不要 这三关键词的近邻上下文,
    以及 rationale 关键词(解释为什么不能填 url)。
    """
    p = _prompt()
    seg_start = p.find("## 证据链")
    assert seg_start >= 0, "missing ## 证据链 section"
    seg = p[seg_start:]

    # 关键词双语都接受
    seg_has_desc = "desc" in seg
    seg_has_url = "url" in seg.lower()
    assert seg_has_desc and seg_has_url, (
        "Evidence Chain section must mention desc and url"
    )

    # 找「desc + url + 不要/NOT」近邻上下文
    idx = 0
    while True:
        idx = seg.find("desc", idx)
        if idx < 0:
            break
        near = seg[idx : idx + 600]
        # 关键词双语: 「不要」「不能」「not」「已是」「already」「chip」
        if "url" in near.lower() and any(
            w in near for w in ["不要", "不能", "not", "already", "已在", "已是", "already shown"]
        ):
            return  # 找到相关教学段
        idx += 1
    raise AssertionError(
        "no guidance about 'desc should not be the URL' in Evidence Chain section"
    )


def test_prompt_prohibits_ref_stacking():
    """Image 63: LLM 错误堆叠 [[ref]][[ref]][[ref]] —— prompt 必须明确禁止."""
    p = _prompt()
    # 关键禁止短语: 中文「堆叠」/「紧挨」/「连续」或英文 stack/concatenat
    keywords_any = ["堆叠", "紧挨", "连续", "stack", "concatenat", "back-to-back"]
    assert any(kw in p for kw in keywords_any), (
        f"missing prohibition of ref token stacking; expected one of {keywords_any}"
    )


def test_prompt_restricts_ref_placement():
    """Image 63: ref 只能在普通段落末尾 —— prompt 必须明确禁止表格/标题/代码块内 ref."""
    p = _prompt()
    # 关键禁止位点关键词（中文双语: 表格/标题/代码块/blockquote 都在 spec 里允许英文混排）
    forbidden = ["表格", "标题", "代码块", "blockquote", "table", "head"]
    missing = [kw for kw in forbidden if kw not in p]
    assert not missing, f"missing forbidden-position keywords: {missing}"


def test_prompt_emphasizes_ref_for_credibility():
    """Image 63: prompt 必须强强调 ref 的可信度价值（多用 / 引用来源 / 信任 / 溯源）."""
    p = _prompt()
    # 找到「证据链」段开头，向后搜 2000 字符
    header_idx = p.find("## 证据链")
    assert header_idx >= 0, "missing ## 证据链 header"
    seg = p[header_idx : header_idx + 2000]
    # 必须有可信度 / 信任 / 溯源相关关键词
    cred_keywords = ["credib", "trust", "traceable", "可信", "信任", "溯源", "慷慨"]
    assert any(kw.lower() in seg.lower() for kw in cred_keywords), (
        "Evidence Chain section must emphasize ref's credibility value"
    )


def test_default_template_is_string():
    """模板必须是 string，方便 format()."""
    assert isinstance(DEFAULT_SYSTEM_TEMPLATE, str)