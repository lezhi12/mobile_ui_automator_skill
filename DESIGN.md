# DESIGN.md — android-adb-explorer-gen 工具化重构设计

## 1. 背景与目标

现状：`scripts/explorer.py` 为 700 行单文件，四个 subcommand（activity/probe/score/seek）。核心问题：

- `seek` 是黑盒循环：dump→打分→滑动→重试全部熔在 15 秒的不透明过程里，agent 只能拿到最终结论，无法中途介入（关弹窗、换关键词、退出死胡同）；
- 证据体系过重：每步 before/attempt-XX/found/after 目录树 + 截图，维护成本高，主要服务人工排障；
- 循环逻辑无法单独测试；
- **验证机制名存实亡**：SKILL.md 的 `waitForStateChange` 在代码中从未实现，act 后无机械验证，"不成功不生成"在代码层无保障——act 记了 step 但点歪（stateId 未变）或重试（多次带 step 的 act）都会照单采进脚本。

目标：按"模拟人类写脚本"的动作拆分工具——**看（dump）、找（find）、动（act）、翻找（seek）**，agent 自己组织循环；机械的交给确定性工具，判断的留给 agent；验证成为一等公民，脚本生成的完整性由机械门禁保证。

## 2. 设计原则

1. **两层工具**：原子层（dump/find/act）供 agent 组合应对复杂情况；复合层（seek/gen/find_h5）覆盖常规机械路径。
2. **原语自我防御**：不变量（dump 重试退避、慢滑语义、U=150 唯一性、exact-match-first、text 非 ASCII 拒绝、设备度量运行探测）内置在工具内部，不依赖 agent 自觉遵守。
3. **日志驱动替代证据驱动**：单一 JSONL 追加日志承载全部决策数据，无截图、无目录树。
4. **找/动分离**：seek/find_h5 只返回坐标不执行点击；act 是唯一执行操作的工具。
5. **宁可失败也不点错**：`unique=false` 硬约束禁止动手。
6. **纯 ADB 产物**：生成的脚本只依赖 adb + bash（沿用模板）。
7. **单 op 单 step + act 必 verify**：一个 step 标签恰好绑定一个操作；带 step 的 act 之后必须紧跟 verify（stateId 对比）确认生效。gen 只采信"成功验证配对"的步骤——脚本完整性是机械检查，不是 agent 纪律（ADR 0001）。
8. **进程内复用**：工具间复用一律 `import` 共享原语库，6 个 CLI 入口只是薄壳，禁止工具 fork 彼此的子进程。

## 3. 总体架构

```
scripts/
  adb_tools.py   # 共享原语库 + 日志（唯一实现处，禁止各工具复制逻辑）
  dump.py        # 原子层·看：dump + activity + stateId；--verify-step 时兼任验证
  find.py        # 原子层·找：对指定 XML 打分 + unique 判定 + webviewStale 提示（纯函数，不碰设备）
  act.py         # 原子层·动：tap/text/back/key/launch/swipe/scroll（唯一执行操作的工具）
  seek.py        # 复合层·翻找：find 不中 → 安全慢滑 → 再 find，循环到找到/超时/到底
  find_h5.py     # 复合层·H5：WebView 页面经 CDP 定位（socket 直探，无 debug 包判断）
  gen.py         # 复合层·生成：日志中带 --step 标记且经验证的操作 → 套模板产出 .sh
templates/
  run-script-template.sh   # 不改动（wait_activity/assertion 三坑已修）
scripts/cdp.mjs             # 保留，小改：增加端口参数（配合 tcp:0 临时端口）
```

数据流：

```
agent 规划 StepGoal[]（单 op 粒度；step 为不透明标签，允许插叙如 2b）
  → 逐 step 定位：seek（常规）；seek failed 转原子层 dump+find 兜底；find 报 webviewStale → find_h5
  → act --step N（临时应对不带 step，只留日志）
  → dump --verify-step N --expect-state <上一状态> → changed=true 采信；false 则排障重试
  → 全部 step 关闭 → gen.py 从日志产出 .sh → agent 冷启动复验
全程：所有工具追加写 logs/<runId>.jsonl（无首行元数据，事件自描述）
```

## 4. 统一 CLI 约定

| 项 | 约定 |
|---|---|
| stdout | 永远恰好一个 JSON 对象（agent 解析用）；人类可读进度走 stderr |
| 退出码 | 0=成功；1=业务失败；2=用法错误（参数缺失等）。**数据类判定（unique=false、changed=false）exit 0**，只以字段表达 |
| 日志 | 所有工具统一 `--log <file>`（§6 各用法行省略），缺省 `explorer.jsonl`（cwd）；agent 在 run 开始创建空 `logs/<runId>.jsonl` 全程复用，**不写首行元数据**（runId 即文件名，goal 由 gen 显式传入，事件自描述） |
| 时间戳 | 事件 `ts` 用 ISO8601 本地时区（含日期，跨天 run 无歧义） |
| 多设备 | agent 设 `ANDROID_SERIAL` 环境变量，adb 原生识别，工具代码零改动 |
| 设备度量 | 共享原语库初始化时 `adb shell wm size` 探测屏幕尺寸并缓存（失败回落 1080×2400）；打分 onscreen 判定与 scroll 坐标按比例派生，不硬编码 |

## 5. 共享原语库 adb_tools.py

全部从 `explorer.py` 平移迁移，实现不变：

| 函数 | 来源 (explorer.py) | 说明 |
|---|---|---|
| `adb(*args, timeout)` | L22 | adb 封装 |
| `get_activity()` | L27 | 3 种 pattern 兜底 |
| `dump_primitive(path)` | L44 | 2 次重试 + `<hierarchy>` 校验 + 1.5s 退避 |
| `parse_nodes(xml_path)` | L124 | 含父指针/中心点/面积 |
| `visible(n)` / `resolve_view(n)` | L154/L176 | 零尺寸文本节点 → 可见祖先代理 |
| `keywords_of(desc)` / `rid_tail(rid)` | L200/L211 | 关键词提取（停用词过滤） |
| `score_node(n, kws)` / `rank(xml, desc)` | L215/L270 | 确定性打分 + 去重排序 |
| `slim(c)` | L294 | 候选瘦身输出 |
| `is_exact_match` / `is_contain_match` / `check_uniqueness` | L305-334 | U=150 + exact-match-first |
| `state_id(xml_path, activity)` | L183 | sha256 前 16 位状态指纹 |
| 常量 | L15-19 | UNIQUENESS=150、STOPWORDS、DYNAMIC_RE（SCREEN_W/H 改运行探测） |

新增：

- `log_event(log_path, tool, **fields)` — 追加一行 JSONL（UTF-8、`ensure_ascii=false`、ts=ISO8601）
- `now_ts()` — ISO8601 本地时区
- `screen_metrics()` — `wm size` 探测 W/H，进程内缓存；scroll 起止按比例派生（如 `0.5W,0.75H → 0.5W,0.33H`）
- `http_get_json(url)` — urllib 实现（**替代 curl**，供 CDP 探测用）

**不迁移**（本轮删除）：`screenshot`、`cmd_probe`。

**迁移并重构**（收编进 find_h5.py，见 §6.6）：`generate_cdp_query`、`get_webview_physical_top`、`get_device_pixel_ratio`、`convert_cdp_to_physical`、`cmd_seek_cdp`；其中 `probe_cdp_available` 简化——**删除 `dumpsys package` debuggable 检查**，改为 socket 直探（非 debug 包探测不到 devtools socket，一个检查覆盖 debug/非 debug），HTTP 请求走 `http_get_json`（删 curl），端口用 `adb forward tcp:0` 临时分配。

**复用纪律**：seek/find_h5 内部的 dump/打分/scroll 直接进程内调用上述函数；scroll 事实记入各自的 attempt 事件，不产生独立 act 事件。

## 6. 工具规格

### 6.1 dump.py — 看 / 验证

```bash
python3 scripts/dump.py [--out PATH]                                      # 普通模式
python3 scripts/dump.py --verify-step N --expect-state <stateId>          # 验证模式
# [--out PATH] 默认 /tmp/explorer/dump.xml（每轮覆盖）
```

- 内部：`dump_primitive` → `get_activity` → `state_id`。无截图。
- **验证模式**：stateId ≠ expect-state → `changed=true`；相等 → `changed=false`；**省略 `--expect-state` → `changed=null`**（agent 显式放行"预期无状态变化"的操作，gen 视为已确认）。`changed` 是数据不是错误，exit 0；dump 失败 exit 1。
- stdout 成功：`{"ok":true,"activity":"pkg/.Act","stateId":"a1b2c3d4e5f60718","dumpPath":"...","dumpBytes":48211,"ms":1820}`；验证模式附加 `"verifyStep":2,"expectState":"...","changed":true|false|null`
- stdout 失败：`{"ok":false,"error":"dump_failed","ms":...}`，exit 1
- 日志：一条 `dump` 事件；验证模式同事件附加 verifyStep/expectState/changed 字段（gen 据此筛 verify 事件）

### 6.2 find.py — 找（纯函数，不碰设备）

```bash
python3 scripts/find.py <dump.xml> <desc>
```

- 内部：`keywords_of` → `parse_nodes` → `rank`（自动应用 resolve_view）→ `check_uniqueness`
- stdout：
```json
{"ok":true,"keywords":["寄快递"],"unique":true,"gap":1330,"webviewStale":false,
 "best":{...slim},"runnerUp":{...}|null,"top":[...slim×5],
 "reason":"唯一命中|best-second>=150|exact-match-first|ambiguous|no-candidate"}
```
- `ok`=存在候选；`unique`=可安全操作；**`webviewStale`=H5 路由信号**——XML 中存在 class 含 WebView 的容器且其子节点无可见 text/resource-id（uiautomator 对该页失效）。**提示是数据，路由归 agent**（SKILL 规则：webviewStale=true → 转 find_h5）
- `unique=false`、`webviewStale=true` 均为数据，exit 0；XML 解析失败 exit 1
- **agent 硬规则：`unique=false` 禁止对 best 坐标执行 act**
- 日志：一条 `find` 事件（top 截断 5，含 webviewStale）

### 6.3 act.py — 动（唯一执行操作的工具）

```bash
python3 scripts/act.py tap    --x N --y N [--step N]
python3 scripts/act.py text   --text S [--step N]
python3 scripts/act.py back   [--step N]
python3 scripts/act.py key    --keycode KEYCODE_X [--step N]
python3 scripts/act.py launch --pkg P [--activity A] [--step N]
python3 scripts/act.py swipe  --x1 N --y1 N --x2 N --y2 N --duration MS [--step N]
python3 scripts/act.py scroll [--step N]
```

- `--step N`：**路径操作标记**（不透明标签，如 `2b`）。带 step 的 act 才可能被 gen 采信，且**每个 step 只允许绑定一个 act**（单 op 单 step 不变量；多操作意图拆成多个 step）。临时应对（关弹窗、退出死胡同）不带 step，只留日志。
- **text 非 ASCII 硬约束**：执行前检测，含非 ASCII（中文等）→ `{"ok":false,"error":"non_ascii_unsupported"}` exit 1 + stderr 说明（`input text` 不支持 Unicode，绕法需额外 app，明确不做；SKILL 注明中文输入路径暂不支持）。
- `text` 转义在执行前内置（空格→`%s`、过滤 shell 元字符），日志同时记 raw 与 escaped。
- `swipe`：duration<800ms 时 stderr 警告 + 日志记 `warn` 字段，不阻止。
- `scroll`：坐标由 `screen_metrics()` 按比例派生（`0.5W,0.75H → 0.5W,0.33H` @1000ms，1px/ms 慢速无惯性，幅度≤视口），只服务探索翻找，不暴露参数。
- `launch`：有 `--activity` 用 `am start -n`，否则 `monkey -p pkg 1`。
- 执行后不做验证（验证 = `dump.py --verify-step`，agent 职责是**每个带 step 的 act 之后必须紧跟一次**）。
- stdout：`{"ok":true,"op":"tap","step":2,"ms":320}`；日志：一条 `act` 事件。

### 6.4 seek.py — 翻找（复合层）

```bash
python3 scripts/seek.py <desc> [--timeout 15] [--max-stuck 6]
```

- 循环：dump（临时文件）→ find → `unique` → found；否则进程内调 scroll 原语翻一屏再来。
- 终止：found / timeout / stuck（连续 max-stuck 轮 stateId 不变 = 到底或卡死）。
- **找到只返回坐标，不执行点击**（找/动分离，found 证据直接喂 gen）。
- stdout found：`{"ok":true,"status":"found","coords":{"x":540,"y":2100},"best":{...},"attempts":3,"ms":8200}`
- stdout failed（富输出，agent 不用重新 dump 即可决策）：
```json
{"ok":false,"status":"failed","reason":"timeout|stuck","attempts":5,
 "stateIds":["a1b2","a1b2","c3d4","c3d4"],
 "lastTop2":[{...slim,"reasons":[...]},{...slim}],
 "ms":15000}
```
- 日志：每轮一条 `seek/attempt` 事件（含 `scrolled` 字段）+ 终态 `seek/found|failed` 事件。
- exit：found 0 / failed 1。

agent 拿到 failed 后的分支：stateId 重复 → 到底/卡住换路径；lastTop2 分数接近 → 换 desc 关键词重试；无候选 → 转原子层自己看 XML。

### 6.5 gen.py — 生成脚本（复合层）

```bash
python3 scripts/gen.py --log <run.jsonl> --steps <标签有序列表> --out <path.sh> [--goal "自然语言目标"]
# 例：--steps 1,2,2b,3   （顺序 = 脚本步骤顺序；标签不透明，允许插叙）
```

采信与生成规则（纯机械）：

1. **筛 verify 事件**：日志中带 `verifyStep` 字段的 `dump` 事件；
2. **关闭每个 step**：对 `--steps` 中每个标签 S，取 verifyStep==S 的**最后一次** `changed∈{true,null}` 事件作为关闭 verify；不存在 → exit 1 拒绝生成（"不成功不生成"的机械门禁）；
3. **配对取 op**：该 step 的 op = 关闭 verify 之前**最近的一条** step==S 的 act（更早的同标签 act 视为重试，自然去重）；若关闭 verify 与上一关闭 verify 之间存在**多条** step==S 的 act（违反单 op 单 step 不变量），stderr 警告 + 日志记 `warn`，仍取最近一条；
4. **wait_activity 派生**：op=launch 的步骤后必插（片段 = 该步关闭 verify 的 activity；SKILL 要求 launch 后等渲染稳定再 verify，避免抓到 splash）；非 launch 步仅当该步关闭 verify 的 activity **≠** 前一步关闭 verify 的 activity 时插（同 Activity 内开抽屉/弹层不插）；
5. op → 命令映射：launch → `am start`/`monkey`；tap → `input tap`；text → `input text`（同一转义函数）；back/key → `input keyevent`；swipe → `input swipe` 原样回放（<800ms 加注释）；
6. 注释自动关联该 op 之前最近一条 find/seek 事件的 desc/best：`# step-2 点击「寄快递」 | 定位: rid=:id/express_btn text=寄快递 | 中心(540,2100)`；
7. 步骤间 `sleep 2`（沿用模板节奏）；尾部 assertion 片段 = 最后一个关闭 verify 的 activity（模板实现）；
8. 头部注释：RUN_ID、目标、日志路径（替代原 Evidence root）。

生成后**冷启动复验**由 agent 执行（是"跑"不是"生成"）：force-stop → `bash out.sh` → 期望 exit 0；失败查日志定位失效坐标。

### 6.6 find_h5.py — H5/WebView 定位（复合层）

```bash
python3 scripts/find_h5.py <desc> [--targetId ID] [--timeout 15] [--max-stuck 6]
```

- **触发前提**：find.py 报 `webviewStale=true`（WebView 容器无可见子节点 → uiautomator 失效）；WebView 容器有可见子节点则正常走 find.py（rid 可能为空但 text 可用）
- **外部依赖检查（入口一次）**：`node --version` + `node -e "typeof WebSocket"` 探测原生 WebSocket（Node ≥21）；不可用 → `{"ok":false,"status":"failed","reason":"node_unavailable"}` + stderr 提示升级 Node。循环内不重复检查。
- **CDP 探测（socket 直探，无 debug 包判断、无 curl）**：未传 `--targetId` 时内部执行——
```
pid = adb shell pidof <pkg>
/proc/net/unix 查 webview_devtools_remote_<pid>，不存在 → unavailable
port = adb forward tcp:0 localabstract:webview_devtools_remote_<pid>   # 临时端口，adb 返回实际分配值
targets = http_get_json("http://127.0.0.1:<port>/json")                # urllib，非 curl
取 type=page 的 targetId；转发失败/解析失败 → reason: forward_failed
```
  unavailable → `{"ok":false,"status":"failed","reason":"cdp_unavailable"}`，无兜底路线，该 step 失败；App 重启后 PID 变化，每次探测重新 forward（tcp:0 天然无端口冲突，多设备/残留 forward 不再互踩）
- **容器 bounds（入口取一次）**：入口 dump 解析 WebView 容器 bounds（x1,y1），循环内复用，不再每轮 dump；找不到 WebView 容器 → `reason: webview_container_not_found`（页面已非 H5 或结构变化，agent 应重新判定路由）
- **定位循环**：`keywords_of` → 生成 JS（TreeWalker 按关键词遍历 DOM → `getBoundingClientRect` → 向上找 clickable 父层；返回值附带指纹 `location.href + '|' + window.scrollY`）→ cdp.mjs 执行 `Runtime.evaluate`（returnByValue）→ 命中且在视口 → found；未命中或离屏 → 进程内 scroll 原语慢滑一屏重试
- **stuck 判定**：**不用 native stateId**（WebView 无可见子节点时 dump 内容不变，stateId 恒定，会从第一轮就误判卡死）；用 JS 返回的 `href|scrollY` 指纹，连续 max-stuck 轮不变 → stuck
- **坐标换算（含 x 偏移）**：`screenX = webViewLeftPhysical + cssCx * dpr`；`screenY = webViewTopPhysical + cssCy * dpr`（webViewTop/Left = 入口 dump 中 WebView 容器 bounds 的 x1/y1；dpr 查 `window.devicePixelRatio`）
- 命中多个同名元素：clickable 最外层容器优先于其内部文本节点
- **只返回坐标不执行操作**（找/动分离）
- stdout found / failed 契约与 seek.py 相同，附加 `"mode":"cdp"`；日志：`find_h5/attempt|found|failed` 事件（attempt 记指纹，附加 mode、targetId、port）

## 7. 日志规范

单一 JSONL 文件，追加写，无首行元数据；事件 envelope：`{"ts":"<ISO8601>","tool":"...", ...}`。

| tool | 关键字段 |
|---|---|
| `dump` | ok, activity, stateId, dumpPath, dumpBytes, ms；验证变体附加 verifyStep, expectState, changed(true\|false\|null) |
| `find` | desc, keywords, unique, gap, webviewStale, topS, top[×5 slim 含 reasons] |
| `act` | op, step?, args(raw+escaped), ok, warn?, ms |
| `seek` | event=attempt(n, stateId, unique, topS, scrolled) / found(coords, attempts) / failed(reason) |
| `find_h5` | 同 seek 事件族（attempt 记 href\|scrollY 指纹），附加 mode、targetId、port |
| `gen` | steps, out, ops, warn? |

日志即证据：排障 = `grep` 日志（每轮 stateId/指纹变化、top 分数、选择理由、verify 结论都在），无文件树。

## 8. 相对现状的移除项与接受的取舍

| 移除 / 改动 | 影响 / 对策 |
|---|---|
| 截图全链路 | dump/seek 不再 screencap，每轮省 1~2s；排障靠日志 bounds+reasons 反推 |
| 证据目录树 | before/attempt-XX/found/after 全删，由 JSONL 日志等价承载决策数据 |
| CDP debug 包判断 | 删除 `probe_cdp_available` 的 `dumpsys package` debuggable 检查；socket 直探同时覆盖 debug/非 debug（非 debug 包无 devtools socket） |
| `curl` 依赖 | CDP HTTP 探测改 urllib（`http_get_json`），少一个外部二进制 |
| 固定端口 9222 | 改 `adb forward tcp:0` 临时端口；cdp.mjs 保留但增加端口参数 |
| dump 失败截图兜底 | 接受：重试 → 仍失败 → 该 step 失败（与"宁可失败也不点错"一致） |
| exact-match-first 截图旁证 | 接受：改为纯结构判定（祖先链 clickable 热区，resolve_view 已实现） |
| run 元数据首行 | 取消：无工具负责写、agent 手写破坏单一来源；runId 即文件名、goal 由 gen 显式传入 |
| 硬编码 SCREEN_W/H 与 scroll 坐标 | 运行探测 + 比例派生；换分辨率设备不再失效 |
| explorer.py | 迁移完成后删除 |

## 9. SKILL.md 同步修改清单

| 章节 | 修改 |
|---|---|
| Description 核心原则 | "证据驱动"→"日志驱动"，删"全程保留证据"表述 |
| Bundled Assets 表 | explorer.py 一行 → adb_tools.py + 6 工具各一行（含 CLI 用法；cdp.mjs 保留、加端口参数说明） |
| §2 环境检查 | 增加：多设备时设 `ANDROID_SERIAL`（与模板约定一致） |
| §3 证据根目录 | → 日志初始化：仅创建空 `logs/<runId>.jsonl`，**无首行元数据** |
| §4.1 dump 原语 | 标注"已内置 dump.py，禁止绕过"；增加 verify 模式说明 |
| §5.4 唯一性 | 删除截图旁证要求，旁证=祖先链 clickable 结构判定 |
| §5.7 能力探测 | 重写：删除截图分支与 debug 包检查；H5 判定 = find 的 `webviewStale` 提示；CDP socket 直探 + tcp:0 + node 入口检查 |
| §6 探索循环 | 重写：seek 优先 → failed 转原子层 → webviewStale 转 find_h5 → act 带 `--step` → **紧跟 `dump --verify-step`（changed=false 排障重试，changed=null 仅用于预期无状态变化的操作）**；`unique=false` 硬约束；**launch 后等渲染稳定再 verify（避免 splash）** |
| §6.1 目录结构 | 删除，替换为 §7 日志 schema |
| §7 生成脚本 | → `gen.py` 调用（`--steps` 有序标签）+ 冷启动复验流程 |
| §8 报告 | 证据路径 → 日志文件路径 |
| Implementation Notes | 增加：**单 op 单 step 不变量**（多操作拆多步）；**text 仅支持 ASCII**（中文输入路径暂不支持，提示用户） |

## 10. 测试策略

| 层 | 方式 | 需要设备 |
|---|---|---|
| find.py / score_node / keywords_of / state_id | fixture XML 纯函数测试：零尺寸 tab 代理、未读数降权、歧义双命中、exact-match-first 例外、webviewStale 判定（有/无可见子节点两例） | 否 |
| gen.py | fixture 日志 → golden .sh 对比，覆盖：同 step 多 act 重试去重、缺成功 verify → exit 1、changed=null 放行、wait_activity 仅在 activity 变化时插入、launch 步必插、`--steps` 插叙标签排序 | 否 |
| adb_tools.py | state_id/keywords_of 单测；screen_metrics 比例派生单测（mock wm size） | 否 |
| dump/act（含 verify 模式、ASCII 校验）/seek | 真机冒烟 | 是 |
| find_h5.py | 真机冒烟。**前提写明**：debug 包，或应用已显式开启 `setWebContentsDebuggingEnabled`（否则无 devtools socket，socket 直探判 unavailable 属预期行为，也应冒烟一次） | 是 |
| 端到端 | 两步路径：探索 → verify → gen → 冷启动复验 exit 0 | 是 |

## 11. 实施顺序

1. `adb_tools.py`（原语平移 + log_event/now_ts + screen_metrics + http_get_json）
2. `dump.py`（含 verify 模式）→ `find.py`（含 webviewStale）→ `act.py`（含 ASCII 校验）→ `seek.py`（薄入口）
3. `find_h5.py`（socket 直探 + tcp:0 + urllib + node 入口检查 + cdp.mjs 端口参数 + href|scrollY 指纹 + 坐标 x/y 偏移修正）
4. `gen.py`（配对采信 + wait_activity/activity 派生 + 不变量违反告警）
5. SKILL.md 重写
6. 删除 `explorer.py`
7. 测试 fixtures（find 的 XML 用例 + gen 的日志用例）
