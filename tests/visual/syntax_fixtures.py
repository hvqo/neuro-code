"""No-network source fixtures shared by syntax regression and visual review."""

SOURCES = {
    "python": """from dataclasses import dataclass
# 审查 / review
@dataclass
class Report:
    path: str = "src/app.py"
    def score(self, count: int = 42) -> float:
        return count / 2.0 if self.path else 0.0
""",
    "rust": """// Preserve source tokens
#[derive(Debug)]
struct Report { count: u32 }
impl Report {
    fn summary(&self) -> String {
        format!("count: {}", self.count + 42)
    }
}
""",
    "json": """{
  "name": "Neuro Code",
  "enabled": true,
  "count": 42,
  "ratio": 2.5,
  "items": ["审查", null]
}
""",
    "shell": """#!/usr/bin/env bash
# Read-only verification
TARGET="src/app.py"
if [ -f "$TARGET" ]; then
    printf '%s\\n' "$TARGET"
    git diff --stat -- "$TARGET"
fi
""",
    "diff": """diff --git a/review.py b/review.py
--- a/review.py
+++ b/review.py
@@ -1,3 +1,3 @@
 class Report:
-    count = 1
+    count = 42
     path = "src/app.py"
""",
    "unknown": 'alpha <beta> "字符串" = 42\nnot_a_known_language ${value}\n',
}

SYNTAX_FIXTURES = {
    f"syntax-{name}": f"```{name if name != 'unknown' else 'neuro-unknown-language'}\n{source}```"
    for name, source in SOURCES.items()
}
SYNTAX_FIXTURES["syntax-long"] = (
    "```python\n"
    + "\n".join(
        f'item_{number} = {{"path": "src/very_long_name_{number}.py", "count": {number}}}'
        for number in range(1, 31)
    )
    + "\n```"
)
SYNTAX_FIXTURES["syntax-mixed-markdown"] = (
    "## Review / 审查\n\n"
    "Ordinary prose stays neutral. `AGENTS.md` remains foreground-only.\n\n"
    + SYNTAX_FIXTURES["syntax-python"]
    + "\n\n**Evidence:** the code palette is independent of the UI palette."
)
