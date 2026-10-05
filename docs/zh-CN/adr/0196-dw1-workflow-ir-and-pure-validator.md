# ADR 0196：DW1 Workflow IR 与纯验证器基础

**简体中文** · [English](../../en/adr/0196-dw1-workflow-ir-and-pure-validator.md)

- 状态：已接受 DW1 基础层
- 日期：2026-10-06
- 范围：仅不可变定义；**尚不存在任何 Workflow 执行路径**

## 背景与边界

DW0 确认需要在不可变 Task DAG 批次上表达有界分支、重复和集合展开。
现有 DAG、Leader、Writable Worker、Profile、Permission、Workspace、Sandbox、
verification 与 recovery 保留原有所有权。DW1 只建立数据和验证契约，不增加
Interpreter、Run、scheduler、repository、migration、bootstrap wiring、模型调用、
工具、prompt、CLI/TUI 入口或 runtime event，不能发布 DAG 或执行 Activity。

`neuro_code.domain.workflows.compile_workflow` 接收 strict JSON，返回不可变
`WorkflowDefinition` 或 `WorkflowValidationError.diagnostics`。没有 I/O。
直接构造 typed definition 也经过相同结构和数据流验证。嵌套值使用 frozen dataclass
与 tuple；可变 source/projection 字典不会进入 IR。

## Source 与 identity

Strict JSON 便于审查、确定性处理并分离声明数据与执行。未知字段、重复 JSON key、
类型强制转换、浮点数、NaN/Infinity、未知 kind 与未支持版本均拒绝。不支持 YAML、
Python/JavaScript expression、`exec`、regex condition、plugin 或文件自动发现。
Prompt 是有界、不透明 template 文本，DW1 不解释或替换；inputs 使用显式 typed
binding，不是表达式字符串。

必须声明 `version: 1`、`definition_id`、`input_schema`、`budget`、`steps` 和
`completion_requirements`。Source 与 canonical 输出均限制为 128 KiB。
Input schema 只支持 required-field 闭合树：string、integer、boolean、object、
有界 array 或 artifact；不是通用 JSON Schema。不支持 optional/nullable、任意 union、
浮点数、plugin schema 或验证执行。Object 必须有 `properties`；array 必须有 `items`
和 `max_items`。容器深度上限为八。

Source 是声明，IR 是已验证、归一化的不可变含义。未来 Run identity 表示一次执行，
**不是** definition hash。Canonical UTF-8 JSON 使用排序 object key、不转义中文、
紧凑 separators；不包含 timestamp、UUID、repr、由主机推断的路径或 locale 信息。
Step/task 数组顺序有语义；capability 排序，dependency set 按 task 声明顺序归一化，
binding、path/schema 名称排序。Source 省略 task `route` 时归一化为 `writable_subagent`。
Canonical bytes 的 SHA-256 是 definition fingerprint；检测定义语义变化，不能充当
权限、artifact 内容检查、run ID、freshness proof、签名或执行成功证明。

## 五种节点

| Kind | 声明与 completion 契约 | DW1 上限 |
|---|---|---|
| TaskBatch | `tasks`、`max_parallel`；批次完成即 join，不创建 Join worker | 复用 Domain Task DAG 常量：8 nodes、16 edges、每 task 4 dependencies、parallel 1–4 |
| Activity | `activity`、typed `inputs`；仅未来 parent-owned intent | `parent.adopt`、`parent.verify`、`parent.repair`；没有 handler |
| Branch | `condition`、恰好两个命名 `paths`、覆盖两条路径的不同 `then_path`/`else_path` | 结构化向前路径，不支持 jump 或任意目标 ID |
| Repeat | `steps`、必须有 `max_iterations`、body 后判断 `until` | 1–3；包括经过 Branch 也禁止嵌套 Repeat |
| Map | 有界 `collection`、必须有 `max_items`、一个 TaskBatch `batch` template | 单次 flatten 为一个批次；`max_items × tasks <= 8`、template parallel <= 4；body 禁止 Map/Repeat/嵌套展开 |

有序 `steps` 表达 sequence，TaskBatch 表达 parallel，TaskBatch/Map completion 表达
join。不增加第六种节点。未来 runtime 应发布不同的不可变 DAG 批次，不能原地修改运行中
DAG，不能创建第二套 scheduler。Map ceiling 必须覆盖 collection schema maximum，
禁止静默截断超限集合。

TaskSpec 必须有 `task_id`、`prompt_template`、`profile_ref`、
`required_capabilities`、`inputs`、`depends_on`；Source 可省略 `route`。
只接受当前 `TaskDagNodeKind.WRITABLE_SUBAGENT` route。Task ID 在批次内唯一，
dependency 在批次内引用且无环。所有 step ID 全局唯一，包括 Map template、互斥分支。
Identifier/profile 使用有界 ASCII 标识；中文 prompt/literal 原样保留，不做 Unicode
normalization 或换行转换。

## 类型、引用与支配关系

Binding 是 tagged `literal`、`input`、`item`、`result` 或 `artifact` object。
Literal 仅有界 string/integer/boolean。InputRef 使用声明的 input-schema path；
ItemRef 仅在 Map TaskSpec binding 中可见。ResultRef 包含 `step_id`、显式
`field_path` 分量，以及可选 `iteration`、`item_key`。Path 只选 required object property，
不是 expression 或未验证 array index。ArtifactRef 包含 `artifact_id`、小写 SHA-256
`integrity_fingerprint` 和可选 opaque `workspace_identity`。现有 worktree/adoption
identity 绑定执行所有者，不强行复用为通用 artifact type。DW1 不读取 artifact。

固定 output schema 使 path 能静态验证：

- TaskBatch：`tasks.<task_id>.status`、`.response`。
- parent.adopt：`status`、`parent_workspace_changed`。
- parent.verify：`status`、`workspace_generation`。
- parent.repair：`status`、`response`。
- Repeat：`iterations`、`last.<必然产生的 body step>.<field>`。
- Map：`count`、`items`（有界 batch result array）。
- Branch：不提升结果，branch-local result 保持局部。

这些是**未来 projection 契约**，不宣称现有 runtime 已产生这些输出。
后续 adapter 必须验证真实返回值。

消费者只能读取已经产生、支配当前位置的结果。Branch 输出不能被无条件读取，
`exists` 也不能掩盖无效数据流。Repeat body 结果可供 body 后的 `until` 使用，只有
Repeat 的 `last` projection 能离开循环。Repeat 采用 post-test：成功继续之前至少执行
一次 body，所以 explicit selector 只允许**已完成 Repeat 的 iteration 1**，后续轮次
无法保证。DW1 无法证明 Map item key 集合，因此拒绝 item-key selector。Optional
output/key-selection proof 留到后续，不能用 runtime `None` 掩盖缺失。

Condition 只有 `eq`、`exists`。Equality 要求 exact scalar type，boolean 不等于 integer，
不提供 boolean scripting language。DW1 只验证，**不求值**。Verification PASS/freshness、
output value、reference/artifact resolution 均属于未来 runtime。

## Profile、预算与诊断

Profile/capability 只表达 intent。内置非 worker role 名称与唯一 writable route 冲突。
Compiler 可以显式接收 Domain AgentProfile 的不可变 `known_profiles` catalog，检查
已知 capability/role contradiction；不导入 application catalog，不发现 custom profile。
合法但未知的 profile 只是未确认 intent。编译不保证 provider/tool availability，不授予
Permission。未来授权仍由实际 parent/global/Profile/runtime capability 交集以及
Permission、Workspace、Sandbox 边界决定。

Budget 必须声明 positive strict integer：`max_generated_tasks`、`max_model_calls`、
`max_tool_calls`、`max_input_tokens`、`max_output_tokens`、`max_wall_seconds`。
Generated task ceiling 为 32，其他声明最大 2^31−1；这是表示上限，不是分配额度。
总声明最多 64，控制嵌套最多八。Worst-case generated work：sequence 求和、branch
取最大路径、Repeat 乘 max iterations、Map 乘 max items，并满足声明的 task ceiling。
DW1 没有 ledger、reservation、spend、authority intersection 或时间测量。

Diagnostic 包含稳定 `code`、JSON `path`、`expected`、有界 `actual`。
先收集独立 structural error，再做 semantic validation；不做危险 compiler recovery。
标准 JSON decoder hook 没有外层 path，因此重复 JSON object key 的 path 固定为根 `$`。
Traceback 不是 API。

## 证据与后续工作

Deterministic fixture 覆盖五种节点、fan-out/fan-in、mixed control flow、CJK、key ordering、
roundtrip 和固定 SHA-256。纯契约测试置于 `tests/architecture/`，现有 Fast CI 会在
Linux、Windows、macOS / Python 3.12、3.14 检查相同 golden fingerprint，无 Runtime mock。

DW2+ 仍需 durable Run/step identity、atomic publication/CAS、validated output projection、
profile/runtime grants、ledger、verification freshness、recovery、reference selection 和
parent activity adapter。本决策没有实现或启用任何 DW2+ 能力。
