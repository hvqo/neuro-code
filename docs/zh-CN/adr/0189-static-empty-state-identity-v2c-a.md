# ADR 0189：静态空状态品牌视觉 V2C-A

**简体中文** · [English](../../en/adr/0189-static-empty-state-identity-v2c-a.md)

- 状态：已接受
- 日期：2026-10-01

## 背景

空会话缺少中央视觉锚点。此前选定的 Braille 图形来自错误原图。正确源为 212×212 RGBA，
SHA256 为 `8334fe13506ca923f09b16933c6c7e5713346d349ada6eb0675ef97021398a6e`，包含
六边形徽章和中央六片旋转结构。旧行数据 / mask 已删除，旧 Logo 正式快照撤回 main
基线，生命周期 / 布局工作保留。用户明确去掉六边形外框，选择 B：仅中央六片结构。
A/C 仅保留为探索记录。

## 决策

`EmptyStateIdentity` 拥有空绑定的呈现门控和几何，图形 seam 只消费固定终端行数据，
从不读取桌面 PNG。Production 只消费 `empty_state_logo.LOGO_ROWS` 的正确源中央结构，
不含六边形外框。探索记录提供 A 完整徽章、B 中央标志、C 中央标志加稀疏弱外框。
离线颜色分割仅提取中央结构与边框，排除实心背景，保持原图比例。CI 无需图像依赖；
Pillow 仅用于可选离线转换脚本。

Resting 使用现有 `TEXT_DIM` / `text-dim` 并叠加 dim，不新增 Logo palette、偏好、
Provider 调用、transcript item 或持久化状态。Activated peak 仅为 gallery 静态预览：
使用现有 secondary 前景并取消 dim；Hybrid 外框仍稀疏且 dim。返回 Resting 恢复完全
相同的截图字节。不发布主动触发输入、动画、计时器或 reveal。

主屏幕覆盖层在 `TranscriptScroll.content_region` 内居中，排除 Header、Composer
与 Footer；不参与布局、不产生滚动高度。Transcript 发出呈现层 resize 通知，草稿
增长或面板改变可用区域时重新定位，无轮询、Runtime 耦合或新增计时器。

终端 >=120×40 选择 32×16，>=100×32 选择 24×12，其他选择 16×8。低于 70 列 / 22 行，
或者实际 conversation 区域无法容纳图形加 8 列 / 4 行余量时隐藏。不得挤压 Composer
或裁剪图形来适配。Resize 始终从固定行数据重算。

新空绑定显示；用户 / Assistant 正文立即关闭并锁定，包括流式内容和历史恢复。
Tool / error 活动同样隐藏，避免成为水印；普通 system 提示不关闭。替换 transcript
时先重置，再根据恢复条目重建锁定状态，因此已有历史 / resume 不误显示。
覆盖层属于主屏幕，modal 生命周期不会重置该绑定的状态。

本轮不实现动画或 reveal。静态图形避免增加动效源，保持确定性与已批准范围。
Syntax Theme、Markdown、键盘归一化、Composer 几何和 Runtime 行为保持冻结。

## 验证与限制

测试覆盖三主题 / 三视口、外壳几何与滚动不变、首次内容消失、实际启动 resume、
绑定重置、modal 所有权、草稿增长居中、空间不足隐藏，以及 large→compact→large
截图字节稳定性。正式空状态 / 草稿快照更新为选定的无边框中央结构；正确源 A/B/C
截图仅作为独立探索记录；首条消息 / 恢复历史 fixture 保留。Gallery 覆盖三主题 / 三视口、Resting / peak / returned-resting、focus、首条消息、
恢复历史与 resize；无真实 Provider 或终端探测。

Braille 依赖单列 Unicode 字体。浏览器 SVG 与确定性 ANSI 快照不能证明真实 Konsole
字形笔画、字宽和 palette。合并前需人工检查 Graphite / Porcelain / System 三尺寸，
焦点、中文多行 / 粘贴、首次发送、历史恢复与 resize。
