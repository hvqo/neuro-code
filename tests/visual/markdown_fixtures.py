# ruff: noqa: RUF001
"""Deterministic Markdown reading fixtures; no model/provider or variant logic."""

READING_FIXTURES = {
    "chinese-paragraphs": "\n\n".join(
        [
            "这次仓库审查从用户可观察的行为开始。我们先检查配置如何进入应用，再沿着请求链路确认权限、工作区与持久化之间的边界。结论需要来自当前源码与测试，而不是仅凭文件名猜测职责。",
            "第一组证据说明，界面层负责把输入转换成应用请求，核心服务负责维护状态一致性。即使终端宽度改变，段内自动换行也应保持紧凑；它并不表示用户开始了一个新的语义段落。",
            "第二组证据关注恢复路径。进程重新启动后，已有会话应恢复同一项目归属，同时重新检查当前工作区的真实状态。旧记忆只是上下文证据，不能替代文件检查与权限判断。",
            "第三组证据来自失败场景。供应商超时、用户取消和无法执行的验证应留下明确结果。审查报告应区分已确认事实、实现推断和仍未解决的问题，避免把部分成功写成完整验收。",
            "最后，我们将独立问题整理成一组可验证的后续行动。正文中的强调只用于重要结论，路径和命令只是定位证据。段落间距应该帮助读者停顿，而不是把每个自动换行都拆成一块空白。",
        ]
    ),
    "english-paragraphs": "\n\n".join(
        [
            "The review begins with observable behavior. Configuration crosses the interface boundary before a request reaches the application service. Each claim should be connected to current source evidence, including the cancellation and recovery paths, rather than inferred from a convenient module name.",
            "Independent evidence can be gathered together. Dependent operations still require ordering, and permission remains authoritative. A narrow terminal should wrap this paragraph naturally without inserting artificial space between the wrapped lines. The next paragraph is a separate Markdown block.",
            "The persistence layer keeps durable conversation intact while presentation remains a projection. A rendering change must not alter the original response, provider requests, or the user's selected reasoning effort. This distinction is especially useful when comparing screenshots from several themes.",
            "The final recommendation records the verified outcome and the remaining gap. A failed verification is reported explicitly instead of hidden behind a success label. Readability should improve scanning without making a compact terminal spend most of its space on empty rows.",
        ]
    ),
    "mixed-paragraphs": "\n\n".join(
        [
            "本轮检查 `AGENTS.md`、application ports 与恢复测试。The reading axis stays stable across themes，普通正文保持 regular，**关键结论**与 *尚待核实的推断* 仍使用原有 emphasis contract。",
            "Snapshot 是 presentation evidence，不是 Runtime authority。我们关注 paragraph boundary 与 CJK 自动换行的区别：中英文混排可能改变一行能够容纳的字符数，但不应该产生随机的额外空行。",
            "验证命令为 `uv run python scripts/check_all.py`。The command is a reference, not a decorative badge；行内代码仍然只有前景色，不恢复 background chip。",
            "最后说明：System 的 ANSI snapshot 不代表所有真实 terminal palette。真实终端仍需人工验收，尤其要确认标题可扫描、正文可连续阅读，以及窄屏没有被空白占满。",
        ]
    ),
    "headings": "\n\n".join(
        f"{'#' * level} H{level} · {'审查结论' if level < 4 else '补充说明'}\n\n"
        "正文保持原有颜色，标题颜色来自 UI semantic theme。**Emphasis**、*italic* 与 `inline code` 不改变。"
        for level in range(1, 7)
    ),
    "paragraph-list": "开头正文说明独立证据需求，不改变列表原有间距。\n\n"
    "- 检查接口与应用服务的调用边界。\n- 检查恢复、取消与持久化测试。\n"
    "- [x] 已确认正文不由 Syntax Theme 控制。\n- [ ] 等待人工视觉选择。\n\n"
    "列表之后的正文保持现有 block transition，不额外扩张。",
    "nested-list": "嵌套列表保持紧凑。\n\n"
    "- 接口证据\n  - 输入归一化\n  - 多行草稿恢复\n"
    "- 应用证据\n\n  同一 list item 中的独立说明段落。\n\n"
    "  第二个说明段落不能套用顶层 prose gap。\n\n"
    "  - 取消\n  - 持久化\n\n结束段落保持列表后的正常间距。",
    "paragraph-code": "代码之前的正文只用于解释，不改变 fenced code 的 token colors。\n\n"
    "```python\ndef validate_request(name: str, retries: int = 3) -> bool:\n"
    "    # Existing Syntax Theme remains authoritative for code.\n"
    '    return name != "" and retries > 0\n```\n\n'
    "代码之后的正文保持现有间距，行内 `validate_request` 没有背景块。",
    "blockquote": "引用之前的普通正文。\n\n"
    "> 引用内部第一段，不套用顶层 prose spacing。\n>\n"
    "> 引用内部第二段。Keep the quote contiguous.\n>\n"
    "> - 引用中的列表\n> - 第二项\n\n引用之后的正文保持现有间距。",
    "table": "表格之前的正文。\n\n"
    "| Evidence | 状态 | 说明 |\n| --- | --- | --- |\n"
    "| Source | 已检查 | Interface boundary |\n| Tests | 已检查 | Cancellation and recovery |\n"
    "| Visual | 待审查 | Three deterministic viewports |\n\n"
    "表格之后的正文保持现有间距。",
}
READING_FIXTURES["long-answer"] = (
    "# 仓库审查 / Repository review\n\n"
    + READING_FIXTURES["mixed-paragraphs"]
    + "\n\n## 证据与边界\n\n"
    + READING_FIXTURES["paragraph-list"]
    + "\n\n### 实现参考\n\n"
    + READING_FIXTURES["paragraph-code"]
    + "\n\n#### 风险说明\n\n"
    + READING_FIXTURES["blockquote"]
    + "\n\n##### 验证结果\n\n"
    + READING_FIXTURES["table"]
    + "\n\n###### 后续验收\n\n"
    + READING_FIXTURES["english-paragraphs"]
)
READING_FIXTURES["streaming-final-state"] = (
    "## Streaming final state / 流式完成态\n\n"
    "第一段已经完整输出，布局在后续 token 到达时不应反复改写。A completed block stays stable.\n\n"
    "第二段逐步到达，只有建立新的独立 paragraph 时才引入一次额外间距，段内软换行仍然连续。\n\n"
    "- 第一项完整证据\n- 第二项完整证据\n\n"
    "最后的 synthesis 不改写历史文本，也不增加任何 Runtime 状态。"
)


READING_FIXTURES = {f"markdown-{name}": source for name, source in READING_FIXTURES.items()}
