#!/usr/bin/env python3
"""gen.py — 复合层·生成：从 JSONL 日志采信带 step 标记且经验证的操作，套模板产出纯 ADB 脚本。

采信规则（纯机械，ADR 0001）：
1. 筛 verify 事件：tool=dump 且带 verifyStep；
2. 每个 step S 取其最后一次 changed∈{true,null} 的 verify 作为关闭事件；无 → exit 1；
3. 该 step 的 op = 关闭事件之前最近的一条 step==S 的 act（更早的同标签 act = 重试，去重）；
   关闭窗口内出现多条同标签 act（违反单 op 单 step）→ stderr 告警 + 日志 warn，仍取最近一条；
4. wait_activity：launch 步后必插；其余步仅当 activity 相对前一步变化时插；
5. 尾部 assertion 片段 = 最后一个关闭 verify 的 activity。
"""
import argparse
import json
import os
import sys

import adb_tools as T

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(SCRIPT_DIR, "..", "templates", "run-script-template.sh")

STEP_HEAD_MARK = "# ---- step-01 launch"
STEP_TAIL_MARK = "# ---- assertion"


def load_events(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                events.append({"_i": i, **json.loads(line)})
            except Exception:
                pass  # 容忍半行/脏数据，日志是 append-only
    return events


def locate_template_segments():
    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    head_at = text.find(STEP_HEAD_MARK)
    tail_at = text.find(STEP_TAIL_MARK)
    if head_at < 0 or tail_at < 0 or tail_at <= head_at:
        raise RuntimeError("template markers not found — template structure changed?")
    return text[:head_at], text[tail_at:]


def fmt_locator(ev):
    """定位事件 → 注释里的定位依据。兼容三种事件形态：
    find（无 best，取 top[0]）/ seek:found（best）/ find_h5:found（element + coords）。"""
    best = ev.get("best")
    if not isinstance(best, dict):
        top = ev.get("top")
        best = top[0] if isinstance(top, list) and top else None
    if not isinstance(best, dict):
        best = ev.get("element")
    if not isinstance(best, dict):
        return None
    parts = []
    if best.get("rid"):
        parts.append(f"rid={best['rid']}")
    if best.get("text"):
        parts.append(f"text={str(best['text'])[:20]}")
    if best.get("cd"):
        parts.append(f"cd={str(best['cd'])[:20]}")
    if best.get("resolvedToAncestor"):
        parts.append("祖先代理")
    if best.get("cx") is not None:
        parts.append(f"中心({best['cx']},{best['cy']})")
    elif isinstance(ev.get("coords"), dict) and ev["coords"].get("x") is not None:
        parts.append(f"中心({ev['coords']['x']},{ev['coords']['y']})")
    return " ".join(parts) if parts else None


def nearest_locator(events, act_idx):
    """act 之前最近一条 find / seek:found / find_h5:found 事件 → 定位注释。"""
    for e in reversed(events[:act_idx]):
        if e.get("tool") == "find" or (e.get("tool") in ("seek", "find_h5")
                                       and e.get("event") == "found"):
            return fmt_locator(e)
    return None


def main():
    p = argparse.ArgumentParser(description="generate pure-ADB script from exploration log")
    p.add_argument("--log", required=True, help="探索期 JSONL 日志")
    p.add_argument("--steps", required=True,
                   help="有序 step 标签列表（不透明，逗号分隔，如 1,2,2b,3）")
    p.add_argument("--out", required=True)
    p.add_argument("--goal", default="")
    a = p.parse_args()

    steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    events = load_events(a.log)

    def idx(e):
        return e["_i"]

    verify_events = [e for e in events if e.get("tool") == "dump"
                     and e.get("verifyStep") is not None]
    acts = [e for e in events if e.get("tool") == "act" and e.get("step") is not None]

    # 1) 关闭每个 step
    closings = {}
    missing = []
    for s in steps:
        closes = [e for e in verify_events
                  if e.get("verifyStep") == s and e.get("changed") in (True, None)]
        if not closes:
            missing.append(s)
        else:
            closings[s] = closes[-1]
    if missing:
        T.log_event(a.log, "gen", steps=steps, ok=False,
                    error="unverified_steps", missing=missing)
        T.emit({"ok": False, "error": "unverified_steps", "missing": missing,
                "detail": "每个 step 需要 dump.py --verify-step 且 changed=true（或显式放行 null）"}, 1)

    # 2) 配对取 op（重试自然去重；不变量违反 → 告警不阻断）
    warns = []
    ops = {}
    prev_close_idx = -1
    for s in steps:
        c = closings[s]
        ci = idx(c)
        window = [e for e in acts if e.get("step") == s and prev_close_idx < idx(e) <= ci]
        if not window:
            T.log_event(a.log, "gen", steps=steps, ok=False,
                        error="act_missing", step=s)
            T.emit({"ok": False, "error": "act_missing", "step": s}, 1)
        if len(window) > 1:
            warns.append(f"step {s}: {len(window)} acts in window（违反单 op 单 step），取最近一条")
        ops[s] = window[-1]
        prev_close_idx = ci

    # 3) 模板拼装
    try:
        head, tail = locate_template_segments()
    except RuntimeError as e:
        T.emit({"ok": False, "error": "template_error", "detail": str(e)}, 1)

    run_id = os.path.splitext(os.path.basename(a.log))[0]
    pkg = None
    first_op = ops[steps[0]]
    if first_op.get("op") == "launch":
        pkg = (first_op.get("args") or {}).get("pkg")
    if not pkg:
        act0 = closings[steps[0]].get("activity") or ""
        pkg = act0.split("/")[0] if "/" in act0 else "unknown"

    head = head.replace("evidence/android-explorer-<RUN_ID>/", f"log: {a.log}")
    head = head.replace("<RUN_ID>", run_id).replace("<自然语言目标描述>",
                                                    a.goal or "(未提供)").replace(
        "<APP_PACKAGE>", pkg)
    n = len(steps)
    blocks = []
    for i, s in enumerate(steps, 1):
        op_ev = ops[s]
        op = op_ev.get("op")
        args = op_ev.get("args") or {}
        closing = closings[s]
        activity = closing.get("activity") or ""
        locator = (nearest_locator(events, idx(op_ev))
                   if op in ("tap", "text") else None)
        lines = [f"# ---- step-{i:02d} [{s}] {op} ----"]
        if locator:
            lines.append(f"# 定位: {locator}")
        label = f"[{i}/{n}]"

        if op == "launch":
            lines.append(f'echo "{label} 启动 {pkg}"')
            lines.append('adb_cmd shell cmd statusbar collapse >/dev/null 2>&1 || true')
            lines.append(f'adb_cmd shell am force-stop "{pkg}"')
            if args.get("activity"):
                lines.append(f'adb_cmd shell am start -n "{args["pkg"]}/{args["activity"]}" '
                             f'>/dev/null 2>&1')
            else:
                lines.append(f'adb_cmd shell monkey -p "{args["pkg"]}" '
                             f'-c android.intent.category.LAUNCHER 1 >/dev/null 2>&1')
            lines.append(f'wait_activity "{activity}" 12')
            lines.append("sleep 2   # 等首页内容渲染（launch 后须渲染稳定再 verify，见日志）")
        else:
            if op == "tap":
                lines.append(f'echo "{label} tap ({args.get("x")},{args.get("y")})"')
                lines.append(f'adb_cmd shell input tap "{args.get("x")}" "{args.get("y")}"')
            elif op == "text":
                esc = args.get("escaped", "")
                lines.append(f'echo "{label} input text"')
                if args.get("ime"):
                    # 与 act.py text --ime 同语义：运行时捕获原输入法，
                    # 经空输入法（如 AdbKeyboard）注入后还原，避免中文 IME 吞字
                    lines.append("ORIG_IME=$(adb_cmd shell settings get secure "
                                 "default_input_method | tr -d '\\r')")
                    lines.append(f'adb_cmd shell ime set "{args["ime"]}"')
                    lines.append(f"adb_cmd shell input text '{esc}'")
                    lines.append(
                        'adb_cmd shell ime set "$ORIG_IME" >/dev/null 2>&1 || true')
                else:
                    lines.append(f"adb_cmd shell input text '{esc}'")
            elif op == "back":
                lines.append(f'echo "{label} back"')
                lines.append("adb_cmd shell input keyevent KEYCODE_BACK")
            elif op == "key":
                lines.append(f'echo "{label} keyevent {args.get("keycode")}"')
                lines.append(f'adb_cmd shell input keyevent "{args.get("keycode")}"')
            elif op in ("swipe", "scroll"):
                cmt = ""
                if (args.get("duration") or 0) < 800:
                    cmt = "  # 警告: duration<800ms 惯性滑动，落点不可复现"
                lines.append(f'echo "{label} {op}"')
                lines.append(f'adb_cmd shell input swipe "{args.get("x1")}" "{args.get("y1")}" '
                             f'"{args.get("x2")}" "{args.get("y2")}" "{args.get("duration")}"{cmt}')
            else:
                T.log_event(a.log, "gen", steps=steps, ok=False,
                            error="unsupported_op", step=s, op=op)
                T.emit({"ok": False, "error": "unsupported_op", "step": s, "op": op}, 1)

            # wait_activity 派生：仅当该步 activity 相对前一步变化
            prev_activity = (closings[steps[i - 2]].get("activity") if i >= 2 else None)
            if activity and activity != prev_activity:
                lines.append(f'wait_activity "{activity}" 12')
            lines.append("sleep 2")
        blocks.append("\n".join(lines))

    last_activity = closings[steps[-1]].get("activity") or "<EXPECTED_ACTIVITY_FRAGMENT>"
    tail = tail.replace("<EXPECTED_ACTIVITY_FRAGMENT>", last_activity)
    tail = tail.replace("对照证据 step-*/after/ 排查", "对照探索日志排查")

    script = head + "\n".join(blocks) + "\n\n" + tail
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(a.out, 0o755)

    for w in warns:
        T.progress(f"警告: {w}")
    T.log_event(a.log, "gen", steps=steps, out=a.out, ops=n,
                warn=warns or None, ok=True)
    T.emit({"ok": True, "steps": steps, "out": a.out, "ops": n,
            "warn": warns or None}, 0)


if __name__ == "__main__":
    main()
