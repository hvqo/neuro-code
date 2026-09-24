# ADR 0176：独立搜索 API 后端

**简体中文** · [English](../../en/adr/0176-independent-search-api-backend.md)

- 状态：已接受
- 日期：2026-09-24
- 范围：通过独立配置的搜索 API 密钥提供与模型无关的网页搜索

## 背景

DeepSeek 的 Responses API 支持客户端函数调用，但会忽略内置 `web_search` 请求类型。
其他 MAIN 配置也可能缺少可信且可执行的托管搜索路由。因此，仅配置模型 API 密钥并不能
使 Neuro 的搜索路由自动可执行。受管理模型配置也不会仅凭兼容的协议格式推断托管工具。设置页需要区分模型供应商
与真正可执行的搜索后端。

## 决策

Neuro 使用 Brave Web Search API，作为现有规范 `web_search` 客户端工具的独立后端。
用户通过进程环境或密钥管理器提供 `BRAVE_SEARCH_API_KEY`，然后重启 Neuro。密钥不写入
工作区、模型供应商配置或持久对话。配置将该环境变量列为受保护凭据，并把其值纳入工具
结果脱敏。

`auto` 的稳定优先级仍是 MAIN 托管搜索、显式且可执行的 `WEB_SEARCH` 路由、自动发现的
可信托管搜索路由。如果这些都不存在，且 Brave 密钥有效，则选择 `SEARCH_API`。
`sidecar` 在未配置托管路由时也可以使用独立 API。`disabled` 不暴露搜索；`inline` 仍
要求 MAIN 具有托管搜索能力。显式路由不可用时，不会暗中切到其他服务。只有后端已解析且
工具策略允许时，binding 才注册本地工具。密钥缺失或格式异常时继续显示搜索不可用。

Brave 适配器只向固定 HTTPS 端点发起一次有界请求，不跟随重定向，并限制查询长度、响应
字节数、来源数量和耗时。它只把有界的标题、URL 和片段投影成规范 `WebSearchResult`。
域名限制既用于 API 查询，也对返回的来源主机名再次检查。HTTP 故障映射为不含凭据的
类型化搜索错误；取消会终止请求。搜索结果仍是不可信证据；该后端不生成模型摘要，也不
提升网页指令的权威性。

只要 MAIN 供应商能调用 Neuro 的客户端工具，独立 API 即可与 DeepSeek、Qwen、
Anthropic、Gemini 等模型配合。各供应商自身的托管搜索能力仍独立判定并保持失败关闭。
设置页和 `/status` 将该路径标为搜索 API，而非模型供应商。使用时仍需要单独的 API 密钥
和网络连接；仅存在密钥不代表远端服务一定会接受。

## 验证

聚焦测试覆盖真实模型适配器的组合与模型可见工具执行、有界 HTTP 投影、域名过滤、脱敏、
类型化故障、取消以及设置页状态。默认完成门禁不执行可能计费的在线服务调用。

## 参考

- [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search)
- [Brave API 鉴权](https://api-dashboard.search.brave.com/documentation/guides/authentication)
- [DeepSeek Responses API 工具兼容性](https://api-docs.deepseek.com/guides/responses_api/)
