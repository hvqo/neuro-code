"""Deterministic whole-delta Markdown render regression tapes; no provider calls."""

CJK = (
    "当前分析必须保留真实模型内容。稳定历史与正在增长的正文不同;缓存不能破坏中文、链接或代码语义。"
)

CODE = "".join(
    f'def inspect_{i}(value: int) -> str:\n    # 中文 annotation {i}\n    return f"value={{value + {i}}}"\n'
    for i in range(180)
)

FIXTURES = {
    "long-cjk": "\n\n".join(CJK * 6 for _ in range(24)),
    "long-markdown": "".join(
        f"## 第 {i} 组证据\n\n{CJK * 2}\n\n- **重点** `file_{i}.py`\n- [引用][doc]\n\n"
        for i in range(30)
    )
    + "[doc]: https://example.com/reference\n\n",
    "code-540": "```python\n" + CODE + "```\n\n",
    "multi-fence": "".join(
        f"```{lang}\n{source}\n```\n\n"
        for lang, source in [
            ("python", CODE[: len(CODE) // 3]),
            ("rust", 'fn main() { let x: u32 = 42; println!("{}", x); }\n' * 120),
            ("json", '{"value": 42, "active": true}\n' * 120),
            ("sh", 'printf "%s\\n" "$HOME"\n' * 120),
        ]
    ),
    "list-code": "- source evidence\n\n  ```python\n"
    + "".join("  " + line for line in CODE[: len(CODE) // 3].splitlines(keepends=True))
    + "  ```\n\n- verification\n  - nested detail\n\n",
    "table": "| 名称 | Evidence | Result |\n|:---|---:|---|\n"
    + "".join(f"| 文件 {i} | {i} | **正确** `value` |\n" for i in range(180))
    + "\n",
    "diff": "```diff\n"
    + "".join(f"@@ -{i},2 +{i},2 @@\n-old = 1\n+new = 42\n context\n" for i in range(160))
    + "```\n\n",
    "mixed": "\n\n".join((CJK + " Evidence must remain correct. ") * 5 for _ in range(25)),
    "long-scroll": "\n\n".join(f"第 {i} 段\n" + CJK * 3 for i in range(60)),
    "open-code": "```python\n" + CODE[: CODE.index("def inspect_110")],
}


def recording(case: str) -> list[dict]:
    items = [{"at_ms": 0, "text": FIXTURES[case]}]
    if case == "open-code":
        tail = CODE[CODE.index("def inspect_110") :]
        chunks = tail.splitlines(keepends=True)
        chunks = ["".join(chunks[i : i + 7]) for i in range(0, len(chunks), 7)]
        chunks += ["```\n\n"] + [CJK + "\n\n"] * 12
    else:
        chunks = [CJK if i % 3 else "\n\n" + CJK for i in range(40)]
    items.extend({"at_ms": (i + 1) * 27, "text": text} for i, text in enumerate(chunks))
    return items


FIXTURES["repeated-code"] = ("```python\n" + CODE + "```\n\n") * 2
FIXTURES["cjk-code"] = CJK * 15 + "\n\n```python\n" + CODE + "```\n\n" + CJK * 10 + "\n\n"
