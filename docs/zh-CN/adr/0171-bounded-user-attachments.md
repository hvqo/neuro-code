# ADR 0171：有界的用户附件

[English](../../en/adr/0171-bounded-user-attachments.md) · **简体中文**

- 状态：已接受
- 日期：2026-09-22
- 范围：回合输入上用户提供的文件/图片附件（TUI 与 CLI）

## 背景
运行时一直接受用户回合上的 `ContentPart` 负载,Provider 适配器也已按各自的媒体与
尺寸规则序列化内联图片.ACP 入口暴露了这条管道.交互式 TUI 与无头 CLI 此前无法附加
任何内容,而且请求估算器把消息的 base64 图片负载当作文本计数,一张数 MB 的图片会让
估算虚增几十万 token,并可能在 `ContextPreflight` 中错误地阻断回合.

## 决策
**估算投影.** `domain/conversation/request.py` 现在把媒体部件投影为有界的计量事实——
媒体类型、解码字节数与短摘要——而不是内嵌编码负载;`estimate_model_request_tokens`
为每个图片部件加固定的 `IMAGE_PART_TOKEN_ESTIMATE`(1,536).指纹保持确定性与内容
敏感性,因为摘要覆盖解码后的字节.

**附件管道.** `application/sessions/attachments.py` 拥有从用户路径到内容部件的有界
转换:图片(按后缀判定媒体类型,单张 ≤5 MiB,总量 ≤10 MiB,每回合 ≤8 张)变为 data URI
的 IMAGE 部件;可解码的文本文件(单个 ≤64 KiB,总量 ≤128 KiB,每回合 ≤8 个)在
`[Attached file: …]` 头之后内联进合成提示词;其余一律拒绝并给出可读原因.路径基于
当前工作区解析并展开 `~`.附件是用户输入:绝不经过沙箱或审批流程.

**入口.** TUI 新增 `/attach <PATH>...` 与输入框上方的附件托盘(点击 ✕ 移除),并通过新的
`ClipboardImageReader` 端口(xclip/wl-paste、osascript PNGf、PowerShell)支持 `Ctrl+V`
粘贴系统剪贴板图片;剪贴板没有图片时 `Ctrl+V` 回落到 TextArea 的文本粘贴.存在待发送
附件时允许空提示词提交.CLI 新增可重复的 `--attach <PATH>`.ACP 保持不变,并与本契约
共享同一套上限词汇.

**消息形状.** 携带媒体部件的回合,把合成提示词作为唯一的 TEXT 部件放在媒体部件旁,
满足既有 `Message` 不变量(“`content` 等于 TEXT 部件投影”).没有媒体部件的回合与
普通回合逐字节一致.

## 后果
- 附加数 MB 的图片不再阻断回合;无论图片大小,估算都稳定且很小.
- 附件内容是用户自有数据:随会话持久化,不进入会话检索索引,不会被脱敏,也不会被
  视为代理发起的读取.
- 剪贴板捕获依赖的平台工具可能缺失;此时快捷键会报告剪贴板中没有图片,并回落到
  文本粘贴.
- 各 Provider 的媒体白名单仍然决定内容是否真正送达 Provider;不支持的媒体照旧在
  Provider 边界失败.
