# ADR 0001 — 脚本生成由 verify 事件门禁（单 op 单 step + act 必验证）

## Status

Accepted（2026-10-10）

## Context

explorer.py 重构为原子/复合两层工具后，"不成功不生成"失去机械保障：

- 原 SKILL.md 要求 act 后 `waitForStateChange` 验证，但代码中**从未实现**——act 记了 `--step` 标记不代表操作生效（点歪时 stateId 不变）；
- JSONL 日志 append-only，agent 点错后重试会产生**多条带同一 step 标记的 act 事件**，gen 无法区分哪条成功；
- "验证靠 agent 自觉"与本项目核心原则（机械的交给确定性工具、原语自我防御）矛盾。

## Decision

1. **单 op 单 step 不变量**：一个 step 标签恰好绑定一个 act 操作；多操作意图（点输入框 + 输文本）拆成多个 step。step 为不透明标签，允许插叙（`2b`），`--steps` 参数顺序即脚本顺序。
2. **act 必 verify**：每个带 step 的 act 之后，agent 必须紧跟 `dump.py --verify-step N --expect-state <上一状态>`。`changed` 由工具机械计算（stateId 对比），`changed=false` 是数据不是错误（exit 0）。
3. **agent 显式放行通道**：预期无状态变化的操作（如 toggle 后 UI 树不变），verify 可省略 `--expect-state`，`changed=null`，gen 视为已确认。这是有意保留的窄逃生口，滥用属 agent 责任。
4. **gen 配对采信**：对每个请求 step，取该 step 最后一次 `changed∈{true,null}` 的 verify 作为关闭事件，采信其之前**最近的一条**同标签 act；更早的同标签 act（重试）自然去重。任一 step 无关闭 verify → exit 1 拒绝生成。检测到关闭窗口内多条同标签 act（不变量违反）→ stderr 告警不阻断。
5. **wait_activity / assertion 片段从 verify 链机械派生**：launch 步后必插；后续步仅当 activity 相对前一步变化时插；尾部 assertion 用最后一个关闭 verify 的 activity。

## Consequences

- 每个带 step 的操作多一次 dump（约 1.5–2s/步），5 步路径约多 10s——换取生成脚本的完整性可机械保证。
- launch 步骤的 verify 须等渲染稳定后再做，否则 activity 片段可能抓到 splash（写入 SKILL.md 纪律）。
- gen 的采信逻辑完全确定，可用 fixture 日志做 golden 测试，无需真机。
- 重试去重零特判；"点歪"从静默污染脚本变为 changed=false 的显式排障信号。
- 未来若出现"一个 step 必须多 op"的真实需求，需先推翻本 ADR 的不变量，而不是在 gen 里加窗口规则打补丁。
