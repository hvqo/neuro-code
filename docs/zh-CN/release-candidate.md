# 预览版候选构建

[English](../en/release-candidate.md) · **简体中文**

R1 为 `0.1.0a1` 定义可复现的本地预览版候选构建。它不会发布包、创建 Git tag
或创建 GitHub Release。

## 源码开发

贡献者从 checkout 工作时使用：

```bash
uv sync --extra dev --locked
uv run neuro
```

源码开发环境不能证明发布候选产物可以安装。不要把 editable 环境作为候选产物安装证据。

## 构建并验证候选产物

从干净的 tracked commit 开始执行：

```bash
uv sync --extra dev --locked
uv run python scripts/release_candidate.py
```

该命令使用 `uv build` 构建一个 wheel 和一个 sdist，检查实际归档成员，分别将两者安装到
checkout 之外的临时虚拟环境，执行 `python -m neuro_code`、`neuro` 和 `neuro-code`，并写入
`dist/release-manifest.json`。manifest 记录精确的源码 commit、文件名、SHA-256、Python 要求
和版本。`dist/` 已被忽略，不得提交。

干净安装检查会移除 checkout 的 `PYTHONPATH`/环境泄漏，并证明导入的模块属于临时环境。
它不会读取 `~/.neuro-code` 或任何用户凭据目录。

## 安装本地候选产物

命令成功后，可以在 checkout 外使用新环境测试候选产物：

```bash
uv venv /tmp/neuro-code-preview
uv pip install --python /tmp/neuro-code-preview/bin/python dist/neuro_code-0.1.0a1-py3-none-any.whl
cd /tmp
/tmp/neuro-code-preview/bin/neuro-code --version
```

Windows 使用 `Scripts/python.exe` 和 `Scripts/neuro-code.exe` 路径。需要验证源码构建时，
可以用同样方法安装 sdist。

## 未来的公开安装

目前尚未发布公开预览版。`pip install neuro-code`、`uv tool install neuro-code` 以及 PyPI/
TestPyPI 发布都属于未来流程，不是当前 R1 的声明。

## 未来的 tag 契约

创建发布 tag 后，tag 必须是 `v0.1.0a1`，并且必须等于规范包版本。分支和 pull request
候选构建不要求 tag。R1 只验证这个契约，不创建 tag。
