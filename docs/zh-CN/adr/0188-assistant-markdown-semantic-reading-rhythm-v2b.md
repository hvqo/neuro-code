# ADR 0188: Assistant Markdown 语义层级与阅读节奏 V2B

**简体中文** · [English](../../en/adr/0188-assistant-markdown-semantic-reading-rhythm-v2b.md)

- 状态：已接受（Variant C 已选择；正式终端验收待完成）
- 日期：2026-10-01

## 背景

V1B 的标题使用中性色，长回答不易快速扫描，连续正文段落也显得密集。
Rich 已在独立 paragraph 之间保留一行空白；所有尺寸统一加一行会明显增加 80×24 的
纵向成本。A/B/C 视觉探索最终选择响应式方案：标题语义 accent，普通视口额外增加
段间空白，compact 保留原有密度。最终代码移除实验 variant 实现。

## 决策

UI semantic Markdown theme 拥有 H1 accent + bold、H2 accent、H3 primary emphasis、
H4 primary、H5 emphasis、H6 secondary。除模型显式 inline emphasis 外仅 H1 使用 bold。
标题没有背景或链接下划线。所有 UI palette 解析相同角色，Syntax Theme 选择不影响标题。

`AssistantMarkdown` 注册 paragraph element，仅当当前和前一个顶层 block 都是 paragraph
时增加一个 `Segment.line()`。token depth 排除 list item、嵌套列表与 blockquote。
heading、list、围栏/缩进代码、quote、table 保留 Rich 原有布局；softbreak 和自动换行仍
紧凑。不修改 response string，也不改变 conversation margin。

transcript controller 通过小型 callback 提供主 Shell 已有的 `compact-chrome` 状态，
打开 modal 时也读取主 Shell。不判断终端名称，不新增第二套 responsive threshold。
现有 Shell 在 width <80 或 height <28 时进入 compact。因此 120×40、100×32 的
paragraph 间为两行空白；80×24 保留 Rich 原有一行空白。独立 renderer 默认普通间距。

每次解析源码时确定 eligible boundary，render 时由当前 Shell policy 决定是否输出间距。
不保存可累加的 spacer 列表或 padding。Textual resize 对同一 widget 重排；streaming
通过已有 controller 重新解析累积原文。恢复视口后恢复同一 segment projection。
模型原文、复制选择、Session History、Syntax Theme、代码 surface/token、Composer/输入
与 Runtime 均不改变。

## 验证与限制

正式测试覆盖所有 UI 标题角色与 Syntax 独立性、已知 RGB accent 对比、CJK/English
换行、准确的一/两行 paragraph gap、非正文 segment 不变、streaming token append，
以及真实 message/pending widget resize。resize 包括 120×40 →80×24 →120×40 和仅
改变高度的 compact boundary；测试不替换实验 renderable，不手动 refresh 掩盖残留空行。

正式 visual harness 提升十一组纯源码 reading fixture，覆盖中文、英文、中英混排、
H1～H6、列表/嵌套、代码、引用、表格、长回答与 streaming 完成态，使用三个主题和三个
视口。显式更新 baseline，复用已有 gallery 展示 main 与正式实现对照。

契约保证 paragraph boundary policy，不承诺所有 streaming Markdown 都不重排：未闭合
fence、setext heading、未完成 table 仍可能触发 Rich 原生 reparse。System snapshot 是
确定性 ANSI/RGB fixture，不代表所有真实 terminal palette；合并前仍需 Konsole 视觉验收。
Empty State artwork、motion、新 Markdown parser 与全局 line-height 不在本决策范围内。
