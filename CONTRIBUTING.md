# 贡献指南 / Contributing

- [中文贡献指南](docs/zh-CN/CONTRIBUTING.md)
- [English contributing guide](docs/en/CONTRIBUTING.md)

所有实现改动都必须通过以下检查。All implementation changes must pass:

```bash
uv run python scripts/check_all.py
```

`scripts/check_all.py` 集中执行锁文件校验、文档 parity 与一致性、Ruff、format、mypy、
pytest 覆盖率与构建；`--quick` 跳过测试与构建。新增门禁只需修改该脚本。
`scripts/check_all.py` runs the lock check, documentation parity and consistency,
Ruff, format, mypy, pytest with coverage, and the build; `--quick` skips the slow
steps. Adding a gate means editing that script.

新增或修改英文 Markdown 文档时，必须在 `docs/zh-CN/` 的相同相对路径提供中文版本；
中文文档也必须在 `docs/en/` 提供英文对应版本。

Every English Markdown document added or changed under `docs/en/` must have a
Chinese counterpart at the same relative path under `docs/zh-CN/`, and vice
versa.
