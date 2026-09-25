# ADR 0176：供应商搜索路由与外部兜底

**简体中文** · [English](../../en/adr/0176-independent-search-api-backend.md)

- 状态：已接受
- 日期：2026-09-24
- 范围：在原生搜索、供应商适配器和外部后端之间解析可执行的 Web Search 路由

## 背景

各供应商的搜索协议并不统一。OpenAI Responses、Anthropic Messages、Gemini Interactions、
DeepSeek 的 Anthropic 兼容 Search，以及 Brave 独立 API 都有各自的请求与响应契约。
DeepSeek 的 OpenAI 兼容 Responses 和 Chat 接口不会执行内置 web_search 工具。仅有
OpenAI 兼容协议或模型 API 密钥，不能证明 Search 路由确实可执行。

## 决策

### 统一契约与可信能力注册表

application.ports.web_search 继续拥有供应商无关的请求、结果、错误、模式、路由类型和执行端口。
可信 SearchCapabilityRegistry 仅根据供应商 service identity、protocol、model capability
事实与路由配置解析已知且具体的适配器。它不会根据 OpenAI 兼容性或供应商名称推断 Search 支持。
现有 OpenAI Responses、Anthropic Messages 和 Gemini Interactions 适配器继续负责各自的原生协议。

有效 route kind 会报告为 native、provider adapter、external fallback 或 unavailable。只有存在
具体客户端路由链且当前工具策略允许时，组合根才注册本地 web_search 工具。只有可信供应商能力和
工具组合检查均通过时，MAIN 原生搜索才可用。

### 有序路由

auto 和 sidecar 按以下次序解析可执行路由：

1. 可信的 MAIN 原生 Search，且能与当前客户端工具共同使用。
2. 已注册的供应商专用适配器或已配置的托管 Search 路由。
3. 用户配置的 Brave Search API 密钥。
4. 明确报告 unavailable，且不向模型暴露 web_search 工具。

DeepSeek 适配器使用用户已有的 DeepSeek API 凭据，通过 Anthropic 兼容的 /messages
Search 契约执行搜索。它只会将凭据发送到固定的 api.deepseek.com HTTPS 端点。解析时必须满足
service_id = "deepseek"、HTTPS、精确官方主机、官方 API 基路径以及非 proxy-managed 凭据。
自定义 base URL、兼容网关或未知主机均不符合条件；对应密钥绝不会发送到官方端点。此时继续尝试
其他已配置路由或 Brave。

该适配器以 Anthropic text block 发送明确且有界的搜索指令，再将有界文本、来源和引用转换成规范
WebSearchResult。域名限制会传达给供应商，并在返回来源主机上再次强制过滤。Agent Runtime 不会接触
供应商私有响应格式。除非以后增加可信适配器或明确的原生能力，Qwen 及其他兼容供应商仍视为不支持；
enable_search 或类似 Responses 的 API 本身不能证明 web_search 可用。

Brave 是用户配置的最后兜底。密钥可通过“设置 → 网页能力”保存在 Neuro 用户 state 的
credentials.json 中，也可通过 BRAVE_SEARCH_API_KEY 提供；环境变量优先。该文件依靠文件权限
保护（POSIX 下为 0600），但不加密，也不使用系统密钥链。搜索密钥不会进入工作区或持久对话。

disabled 继续关闭 Search。显式 inline 仍要求 MAIN 原生 Search，不可用时失败关闭。在路由模式下，
unsupported、endpoint unavailable、超时或 did-not-search 结果可以进入下一路由。鉴权和限流错误立即
呈现；无效请求和格式损坏的响应不会被其他供应商掩盖。did-not-search 只描述当前请求：本次可以进入
兜底，但不会因此把该 route 标记为不可用。每个新请求都会按配置优先级重新检查候选，因此一次兜底成功
不会永久压过更高优先级的 route。确认属于 route 级别的 unsupported 或 endpoint unavailable 后，仍会在
当前 binding 生命周期内抑制该 route。

### 有界、不可信证据与诊断

供应商结果会投影到已有的有界规范结果。返回来源仍会按域名过滤再次检查。适用的 HTTP 客户端使用
固定端点、不跟随搜索 API 的重定向，并限制响应大小与耗时；不会泄漏凭据或原样呈现不可信供应商错误体。
搜索证据始终是非权威数据，不能成为指令来源。

应用层拥有的运行时检查会在现有设置页和 /status 中报告可用性、执行路径、route kind、安全的供应商/
模型标签和已配置的兜底；不包含 endpoint 或密钥。

## 验证

Mock transport 测试覆盖 DeepSeek 官方凭据路由、自定义 base 隔离、明确搜索请求映射与域名过滤、规范结果
转换、原生/适配器/Brave 优先级、请求级 no-search 兜底及下一请求优先级重试、unsupported 与 unavailable
兜底、鉴权/限流处理、格式损坏响应、无路由时的工具门控，以及既有供应商和 Runtime 集成。默认测试门禁
不调用可能计费的在线供应商。

## 参考

- [DeepSeek Anthropic API 兼容说明](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api/)
- [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)
- [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search)
- [Brave API 鉴权](https://api-dashboard.search.brave.com/documentation/guides/authentication)
