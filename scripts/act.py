#!/usr/bin/env python3
"""act.py — 原子层·动：唯一执行操作的工具（tap/text/back/key/launch/swipe/scroll）。

--step N：路径操作标记（不透明标签，如 2 或 2b）。单 op 单 step 不变量：
一个 step 恰好绑定一个 act；多操作意图拆成多个 step。临时应对不带 step。
执行后不做验证——agent 必须紧跟 `dump.py --verify-step N`。
"""
import argparse
import time

import adb_tools as T


def common_args(p):
    p.add_argument("--step", default=None,
                   help="路径操作标记（不透明标签，如 2 或 2b）")
    p.add_argument("--log", default="explorer.jsonl")
    return p


def main():
    p = argparse.ArgumentParser(description="execute one operation on device")
    sub = p.add_subparsers(dest="op", required=True)

    sp = common_args(sub.add_parser("tap"))
    sp.add_argument("--x", type=int, required=True)
    sp.add_argument("--y", type=int, required=True)

    sp = common_args(sub.add_parser("text"))
    sp.add_argument("--text", required=True)
    sp.add_argument("--ime", default=None,
                    help="可选：注入前切到此输入法（如 com.android.adbkeyboard/.AdbIME），"
                         "完成后还原默认输入法。用于中文 IME 吞掉 adb 注入文本的设备")

    common_args(sub.add_parser("back"))

    sp = common_args(sub.add_parser("key"))
    sp.add_argument("--keycode", required=True)

    sp = common_args(sub.add_parser("launch"))
    sp.add_argument("--pkg", required=True)
    sp.add_argument("--activity", default=None)

    sp = common_args(sub.add_parser("swipe"))
    sp.add_argument("--x1", type=int, required=True)
    sp.add_argument("--y1", type=int, required=True)
    sp.add_argument("--x2", type=int, required=True)
    sp.add_argument("--y2", type=int, required=True)
    sp.add_argument("--duration", type=int, required=True)

    common_args(sub.add_parser("scroll"))

    a = p.parse_args()
    args_log = {}
    warn = None
    t0 = time.time()

    if a.op == "tap":
        rc, _, err = T.adb("shell", "input", "tap", str(a.x), str(a.y))
        args_log = {"x": a.x, "y": a.y}
    elif a.op == "text":
        if not T.is_ascii(a.text):
            T.log_event(a.log, "act", op="text", step=a.step, ok=False,
                        error="non_ascii_unsupported", args={"raw": a.text},
                        ms=int((time.time() - t0) * 1000))
            T.progress("input text 仅支持 ASCII（adb 不支持 Unicode）；中文输入路径暂不支持")
            T.emit({"ok": False, "error": "non_ascii_unsupported"}, 1)
        escaped = T.escape_text(a.text)
        orig_ime = None
        if a.ime:
            # 中文 IME 会拦截 adb 注入的字符并唤起自身吞掉输入 → 经空输入法中转
            _, out_o, _ = T.adb("shell", "settings", "get", "secure",
                                "default_input_method")
            orig_ime = out_o
            T.adb("shell", "ime", "set", a.ime)
            _, out_v, _ = T.adb("shell", "settings", "get", "secure",
                                "default_input_method")
            if out_v != a.ime:  # 切换失败：还原并快速失败，宁可失败也不静默错输
                if orig_ime and orig_ime != a.ime:
                    T.adb("shell", "ime", "set", orig_ime)
                ev = {"op": a.op, "step": a.step, "ok": False,
                      "error": "ime_switch_failed", "args": {"ime": a.ime},
                      "ms": int((time.time() - t0) * 1000)}
                T.log_event(a.log, "act", **ev)
                T.progress("ime set 失败，目标输入法未生效")
                T.emit({"ok": False, "error": "ime_switch_failed"}, 1)
            rc, _, err = T.adb("shell", "input", "text", escaped)
            if orig_ime and orig_ime != a.ime:
                T.adb("shell", "ime", "set", orig_ime)
        else:
            rc, _, err = T.adb("shell", "input", "text", escaped)
        args_log = {"raw": a.text, "escaped": escaped}
        if a.ime:
            args_log["ime"] = a.ime
            args_log["orig_ime"] = orig_ime
    elif a.op == "back":
        rc, _, err = T.adb("shell", "input", "keyevent", "KEYCODE_BACK")
        args_log = {}
    elif a.op == "key":
        rc, _, err = T.adb("shell", "input", "keyevent", a.keycode)
        args_log = {"keycode": a.keycode}
    elif a.op == "launch":
        args_log = {"pkg": a.pkg, "activity": a.activity}
        if a.activity:
            rc, _, err = T.adb("shell", "am", "start", "-n", f"{a.pkg}/{a.activity}")
        else:
            rc, _, err = T.adb("shell", "monkey", "-p", a.pkg,
                               "-c", "android.intent.category.LAUNCHER", "1")
    elif a.op == "swipe":
        if a.duration < 800:
            warn = "swipe_duration_lt_800ms"
            T.progress("警告: duration<800ms 为惯性滑动，落点不可复现（仅探底扫描用）")
        args_log = {"x1": a.x1, "y1": a.y1, "x2": a.x2, "y2": a.y2, "duration": a.duration}
        rc, _, err = T.adb("shell", "input", "swipe",
                           str(a.x1), str(a.y1), str(a.x2), str(a.y2), str(a.duration))
    elif a.op == "scroll":
        x, y1, x2, y2, dur = T.scroll_swipe_args()
        args_log = {"x1": x, "y1": y1, "x2": x2, "y2": y2, "duration": dur, "derived": True}
        rc, _, err = T.adb("shell", "input", "swipe",
                           str(x), str(y1), str(x2), str(y2), str(dur))
    else:  # pragma: no cover
        T.emit({"ok": False, "error": "unknown_op"}, 2)

    ms = int((time.time() - t0) * 1000)
    ok = (rc == 0)
    ev = {"op": a.op, "step": a.step, "args": args_log, "ok": ok, "ms": ms}
    if warn:
        ev["warn"] = warn
    if not ok:
        ev["error"] = (err or "adb_failed")[:200]
    T.log_event(a.log, "act", **ev)

    out = {"ok": ok, "op": a.op, "step": a.step, "ms": ms}
    if warn:
        out["warn"] = warn
    if not ok:
        out["error"] = ev["error"]
    T.emit(out, 0 if ok else 1)


if __name__ == "__main__":
    main()
