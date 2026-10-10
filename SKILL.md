# Skill: android-adb-explorer-gen

## Name

`android-adb-explorer-gen`

## Description

接收一条自然语言的 Android 操作路径（如"打开某 App，点击某按钮，输入账号再点击登录"），用两层工具**边走边探测** App 界面，**日志驱动**（单一 JSONL 记录全部决策数据，无截图无目录树），**只有整条路径全部探索成功且逐步验证通过后**，才机械采信日志生成**纯 ADB 命令**脚本（`.sh`）。

核心原则：
- **探索成功再生成**：任一 step 缺少成功验证 → gen 拒绝生成（机械门禁，非 agent 自觉）。
- **日志驱动**：所有工具追加写同一 JSONL；排障 = 检索日志。
- **找/动分离**：seek/find_h5 只返回坐标；act 是唯一执行操作的工具。
- **宁可失败也不点错**：`unique=false` 硬约束禁止动手。
- **单 op 单 step + act 必 verify**：一个 step 标签绑定一个操作，操作后紧跟 stateId 对比验证（ADR 0001）。
- **纯 ADB 输出**：生成脚本只依赖 adb + bash。
- **确定性探索**：打分/唯一性判定内置在工具中，agent 只消费字段（unique/gap/webviewStale/coords）。

## When to Use

当用户需要：
- 根据自然语言操作路径自动探索 Android App 并生成可复现的纯 ADB 脚本
- 不想依赖视觉模型，只基于 UI 层级做确定性操作
- 生成的脚本可直接在 CI/任意有 adb 的环境运行

**不适用**：图像对比/OCR 类复杂断言；含**中文文本输入**的路径（`input text` 不支持 Unicode，遇到应明确告知用户该路径暂不支持）；无法用关键词描述的极模糊跨 App 跳转。

## Tools

- `bash` - 调用 scripts/ 下工具、执行生成的脚本
- `write` / `read` - 写读日志与生成的脚本
- `grep` - 检索 JSONL 日志

## Bundled Assets（随包资产，直接使用，禁止重新实现）

| 文件 | 用途 |
|---|---|
| `scripts/adb_tools.py` | 共享原语库（打分/唯一性/状态指纹/慢滑/日志），禁止绕过或复制其逻辑 |
| `scripts/dump.py` | 看/验证：`dump.py [--out PATH]`；验证模式 `dump.py --verify-step N --expect-state <stateId>`（省略 expect-state → changed=null 显式放行） |
| `scripts/find.py` | 找：`find.py <dump.xml> <desc>` → unique/gap/webviewStale（纯函数，不碰设备） |
| `scripts/act.py` | 动：`act.py tap --x --y / text --text [--ime IME_ID] / back / key --keycode / launch --pkg [--activity] / swipe ... / scroll [--step N --log F]`（唯一执行操作的工具） |
| `scripts/seek.py` | 翻找：`seek.py <desc> [--timeout 15] [--max-stuck 6]` → found/failed 富输出 |
| `scripts/find_h5.py` | H5 定位：`find_h5.py <desc> [--target-id ID]`（find 报 webviewStale=true 时使用；需 Node ≥21） |
| `scripts/cdp.mjs` | CDP 执行器（find_h5 内部调用，端口参数已支持 tcp:0 临时端口） |
| `scripts/gen.py` | 生成：`gen.py --log <run.jsonl> --steps <有序标签> --out <path.sh> [--goal "..."]` |
| `templates/run-script-template.sh` | 唯一模板（wait_activity/assertion 三坑已修），gen.py 自动套用，禁止手写替换 |

所有工具统一约定：stdout 恰好一个 JSON；`--log` 指定 JSONL（缺省 explorer.jsonl）；数据类判定（unique=false、changed=false）exit 0。

## Inputs

通过 `user_message` 接收自然语言操作路径。先规划 `StepGoal[]`：**单 op 粒度**（tap/input/back/launch/swipe 各占一步；"点输入框再输文本"拆两步）；step 标签为**不透明标记**（1,2,3…；计划外插入用 `2b` 这类插叙标签），仅在探索中遇到对应操作时落到 `--step` 上。

## Workflow

### 1. 环境检查与日志初始化

1. `adb --version`、`adb devices`（确认 ≥1 台 device）；**多设备时列出并让用户指定，随后全程设 `ANDROID_SERIAL`**。
2. `adb shell cmd statusbar collapse` 收起系统遮挡。
3. 创建空日志 `logs/<runId>.jsonl`（runId=时间戳），**不写首行元数据**；后续所有工具传 `--log logs/<runId>.jsonl`。

### 2. 逐 step 探索循环

对每个 StepGoal（launch 类型直接 act，无需定位）：

1. **定位**：
   - 常规：`seek.py <desc> --log …`。found → 取 `coords` 进 act。
   - failed → 读富输出决策：stateId 重复=到底/卡住，换路径或换关键词重试；lastTop2 分数接近=换关键词；无候选 → **原子层兜底**：`dump.py --out …` + `find.py <dump> <desc>` 自己判读 top 列表。
   - find 报 **`webviewStale=true`**（uiautomator 对该页失效）→ 转 `find_h5.py <desc>`。
2. **硬规则：`unique=false`（或无 coords）禁止执行任何 tap**。可换 desc 关键词重试或转原子层分析。
3. **执行**：`act.py <op> … --step <标签>`。临时应对（关弹窗、退出死胡同、清理状态）**不带 step**，只留日志。
4. **验证（必须紧跟 act，不可跳过）**：先记下上一状态 `stateId`（来自上一次 dump/verify 的 stdout）：
   - `dump.py --verify-step <标签> --expect-state <上一stateId>` → `changed=true` 采信，进入下一 step；
   - `changed=false` → 排障重试（重试的 act 仍带同 step 标签，gen 自动只取最近一条）；
   - **省略 `--expect-state` → `changed=null`**：仅用于预期无状态变化的操作（agent 显式放行，gen 视为已确认）。
   - **launch 后等渲染稳定再 verify**（首屏可能是 splash，activity 片段会取错；可 `sleep 2` 后再 verify）。
5. seek/兜底/find_h5 全部失败 → 该 step 失败 → 不生成脚本，汇报失败详情。

### 3. 生成脚本

1. 全部 step 关闭后：`python3 scripts/gen.py --log logs/<runId>.jsonl --steps <有序标签逗号分隔> --out <脚本路径> --goal "<原始目标>"`。
   - gen 机械采信：每个 step 取"最后一次成功 verify 之前最近的同标签 act"；wait_activity/assertion 片段自动从 verify 链派生。
   - 任一 step 未验证 → gen exit 1，回到探索补验证。
2. gen 已 `chmod +x`。**禁止手工改写脚本坐标**——坐标错了回到探索重走该 step。

### 4. 冷启动复验（必须）

`adb shell am force-stop <pkg>` → `bash <脚本>.sh` → **期望 exit 0**。失败 = 脚本在干净状态下不可复现，回探索日志定位失效坐标，重走该 step 后重新生成。

### 5. 报告

runId、目标、设备、状态、日志路径 `logs/<runId>.jsonl`、脚本路径（仅成功）、各 step 摘要（desc/op/坐标/验证结论）、失败详情（仅失败）。

## Implementation Notes

- **dump 唯一入口**：一律经 `dump.py`（内置重试退避），禁止循环内裸调 `uiautomator dump`。
- **滑动语义同源**：探索翻找用 `scroll`（1px/ms 慢滑，按 wm size 比例派生）；生成脚本回放探索期 swipe 原参数。惯性滑动（<800ms）只可探底，不可定位。
- **text 仅 ASCII**：act.py 对非 ASCII 直接拒绝（`non_ascii_unsupported`）；中文输入需求应提示用户暂不支持。
- **中文 IME 设备吞字**：中文输入法（搜狗/讯飞/微信输入法等）会拦截 adb 注入的字符并唤起自身吞掉输入（`input text` 与字符 keyevent 均失效，且 IME 被重新唤起）。用 `act.py text --ime <空输入法ID>`（如已装的 `com.android.adbkeyboard/.AdbIME`）：切换→注入→立即还原默认输入法，gen 按同语义回放（运行时捕获并还原）。
- **stateId 只用于变化检测**，不用于元素定位；H5 页面的 stuck 判定由 find_h5 用 `href|scrollY` 指纹完成。
- **CDP 探测**：socket 直探（无 debug 包判断）+ `tcp:0` 临时端口；非 debug 包无 devtools socket → `cdp_unavailable` 属预期，该 step 失败。
- **排障**：全部答案在 JSONL 日志里（每轮 stateId/指纹、top 分数与理由、verify 结论）。

## Error Handling

- 无设备 → 报错终止；多设备 → 要求指定 `ANDROID_SERIAL`。
- dump 反复失败 → 工具内部已重试退避，仍失败则该 step 失败。
- `cdp_unavailable` / `node_unavailable` / `webview_container_not_found` → find_h5 明确报因，agent 重新判定路由或终止。
- 卡住（连续 max-stuck 轮状态不变）→ 该 step 失败，换路径。

## Success Criteria

全部 step 存在成功 verify（changed=true 或 agent 放行的 null）→ gen 产出脚本 → **冷启动复验 exit 0** 才算完成。
