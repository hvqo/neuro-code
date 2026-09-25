# Neuro Code

[English](../../README.md) · **简体中文**

[![CI](https://github.com/hvqo/neuro-code/actions/workflows/ci.yml/badge.svg)](https://github.com/hvqo/neuro-code/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](../../pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-2F5D50.svg)](../../LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#项目状态)

Neuro Code 是一个 Python 原生的终端 Coding Agent。它通过模型驱动的工作流帮助开发者理解、修改和测试代码库，同时将本地操作置于明确的工作区、权限和沙箱边界之内。

命名的 provider profile 和持久会话，让工作流可以在受支持的模型服务之间灵活切换，而不必将运行时绑定到单一托管 provider。

> **本文与英文版的关系**
>
> 本页是**产品概览**；英文版 [`README.md`](../../README.md) 同时还是**完整使用手册**，
> 以下专题目前**只在英文版**中提供，本页不重复其内容：
>
> - 安装与启动细节：`Install and launch`
> - 开发环境与本地验证：`Development`
> - 完整的 TUI 使用说明（快捷键、设置页、会话管理、工具卡片）：`Interactive TUI`
> - 受管后台命令与唤醒策略：`Managed background commands`
> - OS 沙箱 profile 的逐平台边界：`Operating-system sandbox profiles`
> - provider profile、能力矩阵、故障转移与代理策略：`Model providers`
> - 只读 LSP 语义导航：`Read-only LSP semantic navigation`
> - ACP v1 适配器的能力与限制：`Partial ACP v1 stdio`
>
> 若要了解某项能力**支持到什么程度、明确不支持什么**，请直接查
> [兼容性矩阵](compatibility-matrix.md)（中英内容一致度约 98%）。

<p align="center">
  <img src="../NeuroCode.png" alt="Neuro Code 终端界面" width="90%">
</p>

## 为什么选择 Neuro Code？

- **Agentic coding workflow** — 从一次提示开始，在同一个 agent loop 中完成仓库检查、受边界约束的文件修改、命令执行和迭代反馈。
- **Provider flexibility** — 通过命名 profile 连接受支持的模型服务，并可按次运行或在 TUI 中切换活动 profile。
- **Explicit control** — 副作用工具经过权限策略和工作区检查；可选的 OS 级子进程 sandbox 会约束已获批准的本地操作。
- **Durable sessions** — 基于 SQLite 的会话支持继续、搜索、重命名、分叉、导出和导入；恢复时会校验工作区、provider 和 sandbox 关联。

## 快速开始

Neuro Code 目前处于 pre-alpha 阶段，当前应从源码 checkout 运行。需要 Python 3.12 或更高版本；本流程使用 [`uv`](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/hvqo/neuro-code.git
cd neuro-code
uv sync --extra dev
uv run neuro
```

首次交互式启动时，如果没有已就绪的 profile，会打开 provider 设置。当前目录会作为工作区。配置好 provider profile 后，可以使用以下命令运行无头 prompt：

```bash
uv run neuro-code -p "Explain this repository"
```

### 构建的预览候选产物

R1 可以在不发布包的情况下构建并验证本地 `0.1.0a1` wheel 和 sdist。请参阅[预览版候选构建流程](release-candidate.md)，
然后在 checkout 外的新环境中安装生成的 wheel。

### 未来的公开安装

目前尚未发布公开包。`uv tool install neuro-code`、`pipx install neuro-code` 以及
PyPI/TestPyPI 安装属于未来发布流程，不是当前支持声明。

### 显式执行验证

可以通过显式启动参数，为普通用户回合指定一个有界的验证命令：

```bash
uv run neuro-code -p "更新解析器" --verify-command "uv run pytest -q"
uv run neuro-code agent -p "更新解析器" --verify-command "uv run pytest -q"
uv run neuro-code code --verify-command "uv run ruff check ."
```

工作区发生修改且当前验证报告仍需要证据时，该命令最多执行一次。执行会复用普通 Bash
工具的权限、工作区、sandbox、取消和事件流水线。只接受现有已识别的 `pytest` 与静态检查
命令族；未知、格式错误或超出大小限制的命令会在执行前拒绝。本选项必须由用户显式提供，
不会发现测试、推断要求、检测框架，也不提供专用测试运行器。通用成功结果只表示当前工作区
中的已识别命令成功，不表示所有相关测试都已执行，也不表示请求的行为已得到完整验证。普通
TUI 启动也通过共享的应用设置边界使用该选项；本切片不向 ACP 暴露该选项。

## 核心能力

- **Coding workflow** — 无头 prompt 和 Textual TUI 共享同一个事件驱动运行时。内置工具覆盖受边界约束的文件查看与搜索、精确替换编辑、Bash、后台任务和计划。
- **Model providers** — 命名 profile 使用 OpenAI Responses、OpenAI-compatible Chat、Anthropic Messages 或 Gemini 适配器，并执行 provider 和 model 级别的能力检查。
- **Tools** — 内置仓库和 shell 工具；在受支持配置下，还可使用可选的 web search、公开网页获取和只读 LSP 集成。
- **Sessions** — 基于 SQLite 的会话支持恢复、工作区范围内的搜索、标题、分叉、导出/导入以及持久化的计划/任务元数据。
- **TUI** — Textual 界面提供流式对话、provider 和 session 选择器、审批提示、斜杠命令、Markdown 渲染以及持久化的 UI 偏好设置。
- **Bounded orchestration** — 有界、持久化的 Task DAG、Leader、Agent Swarm 和自动 Ultracode 委派可以协调受约束的并行工作；结果采纳和可写 worker 仍受显式能力、工作区和 sandbox 边界控制。

Ultracode 路由还会识别“项目/仓库范围”与明确的优化意图同时出现的请求（例如“请你对这个项目的代码进行分析，告诉我哪里还有需要优化的地方？”），此组合可以选择 `BOUNDED_SWARM`；窄范围的函数/文件排错或优化仍选择 `MAIN_MAX`，现有 Swarm objective 字节上限继续有效。普通执行默认使用 `normal`（48 次模型调用）；省略档位时，Ultracode 的 `MAIN_MAX` 请求使用更深的 96 次预算，而显式 `--execution-profile normal`、`deep` 或 `--max-steps N` 保持显式选择。该预算按请求传递并持久化快照用于恢复；缺少或不兼容预算的 legacy 非终态记录会 fail closed，已完成记录的 replay 语义不变。TUI 的 typed `BUDGET_LIMITED` 提示会在可用时报告原因和用量，`STUCK` 提示保持不变。

## 安全与控制

- **Workspace boundary** — 结构化文件系统操作会将目标解析到启动工作区和显式配置的根目录内；会拒绝通过链接类路径逃逸。
- **Permission boundary** — Deny/ask/allow 规则会控制副作用工具。显式 deny 始终有效；无头模式下未解决的审批请求会被拒绝。
- **Sandbox boundary** — `off`、`workspace`、`read-only` 和 `strict` profile 可用。默认 `off` profile 明确不提供 OS 隔离；在可实施时，启用的 profile 使用平台特定的子进程边界，无法支持的显式请求会失败关闭。

权限、工作区身份和 sandbox 策略是彼此独立的决策。有关当前边界，请参阅[架构](architecture.md)和[兼容性矩阵](compatibility-matrix.md)。

## 集成

- **Provider integrations** — 当前 service catalog 包含 OpenAI、xAI、Anthropic、Gemini、DeepSeek、Kimi、GLM、MiniMax、Volcengine Ark、Baidu Qianfan、Alibaba Model Studio、Tencent TokenHub，以及通用 OpenAI-compatible endpoint。实际能力取决于 provider 和 model；请参阅[兼容性矩阵](compatibility-matrix.md)。
- **MCP** — 由 session 持有的 MCP server 连接支持 stdio、Streamable HTTP 和 legacy SSE transport，并提供有边界的工具发现与执行。
- **ACP** — 提供 partial ACP v1 适配器，支持换行分隔的 stdio，并提供有边界的 WebSocket bridge，以及部分工作区绑定的 session、permission、filesystem 和 terminal 能力。ACP 兼容性明确是 partial；ACP-transport MCP server declaration、二进制多媒体历史回放、客户端交互式终端输入/resize/PTY 方法以及任意自定义扩展仍不支持；请参阅[兼容性矩阵](compatibility-matrix.md)。

### 为 DeepSeek 等模型配置独立网页搜索

DeepSeek 的 OpenAI 兼容 Responses 和 Chat 接口不会执行内置 `web_search`。对于官方 DeepSeek
供应商配置，Neuro 会使用 DeepSeek 的 Anthropic 兼容 Search 适配器，并且只有在配置指向 DeepSeek
官方 HTTPS 端点时才复用已有的 DeepSeek 密钥；不会把自定义地址或兼容网关的密钥发送到官方端点。
Brave Search 是最后的独立兜底。获取 Brave Search API 密钥后，可打开
**设置 → 网页能力 → Brave 网页搜索 API 密钥**进行配置。Neuro 会将密钥保存在用户 state
目录的 `credentials.json` 中，与模型配置分开且不写入仓库。该文件当前不加密，也不使用系统密钥链。
也可以设置 `BRAVE_SEARCH_API_KEY`；环境变量会覆盖设置中保存的值。在 Bash 中可避免把密钥写入 Shell 历史：

```bash
read -rsp 'Brave Search API key: ' BRAVE_SEARCH_API_KEY
export BRAVE_SEARCH_API_KEY
neuro
```

网页搜索模式保持为 `auto`。没有可执行的原生或供应商适配器路由时，Neuro 会将 Brave Search API
作为最后兜底，设置页和 `/status` 会显示这一实际路径。搜索密钥与模型供应商密钥分开，不写入项目或对话。通过设置页保存后，
运行时会重新加载并恢复当前会话。Brave 可能要求开通套餐，并可能对 API 调用计费。详见
[ADR 0176](adr/0176-independent-search-api-backend.md)。

## 项目状态

Neuro Code 处于 **pre-alpha** 阶段。当前源码树已包含 CLI 和无头运行时、Textual TUI、命名 provider profile、本地工具、SQLite session、权限与 sandbox 控制、MCP 连接以及 partial ACP 的已实现切片。

Provider/model 兼容性、平台 sandbox 覆盖范围和协议能力仍在演进。当前支持边界请查看[兼容性矩阵](compatibility-matrix.md)，路线图请查看[开发计划](rewrite-plan.md)。

## 文档

| 内容 | 简体中文 | English |
|---|---|---|
| 架构 | [架构](architecture.md) | [Architecture](../en/architecture.md) |
| 兼容性 | [兼容性矩阵](compatibility-matrix.md) | [Compatibility matrix](../en/compatibility-matrix.md) |
| 预览候选 | [预览候选](release-candidate.md) | [Release candidate](../en/release-candidate.md) |
| 路线图 | [开发计划](rewrite-plan.md) | [Development plan](../en/rewrite-plan.md) |
| 贡献 | [贡献指南](CONTRIBUTING.md) | [Contributing](../en/CONTRIBUTING.md) |
| 架构决策 | [ADR](adr/) | [ADRs](../en/adr/) |

## 参与贡献

开发流程、必要检查和文档规则请参阅[贡献指南](CONTRIBUTING.md)。

## 许可证

根据 [Apache-2.0 License](../../LICENSE) 授权。
