#!/usr/bin/env python3
"""seek.py — 复合层·翻找：dump→find→unique，不中则进程内慢滑一屏再来。

终止：found / timeout / stuck（连续 max-stuck 轮 stateId 不变 = 到底或卡死）。
只返回坐标，绝不执行点击（找/动分离）。
"""
import argparse
import time

import adb_tools as T

TMP_DUMP = "/tmp/explorer/seek-dump.xml"


def main():
    p = argparse.ArgumentParser(description="seek a uniquely-scored candidate by scrolling")
    p.add_argument("desc")
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--max-stuck", type=int, default=6)
    p.add_argument("--log", default="explorer.jsonl")
    a = p.parse_args()

    t0 = time.time()
    deadline = t0 + a.timeout
    attempts = 0
    stable = 0
    last_state = None
    state_ids = []
    last_top2 = None

    while time.time() < deadline:
        attempts += 1
        scrolled = attempts > 1
        ok, _ = T.dump_primitive(TMP_DUMP)
        if not ok:
            T.log_event(a.log, "seek", event="attempt", desc=a.desc, n=attempts,
                        stateId=None, unique=False, topS=None, scrolled=scrolled)
            T.progress(f"[seek {attempts:02d}] dump_failed")
            x, y1, x2, y2, dur = T.scroll_swipe_args()
            T.adb("shell", "input", "swipe", str(x), str(y1), str(x2), str(y2), str(dur))
            time.sleep(1.0)
            continue

        activity = T.get_activity()
        sid = T.state_id(TMP_DUMP, activity)
        state_ids.append(sid)
        _, cands = T.rank(TMP_DUMP, a.desc)
        best = cands[0] if cands else None
        second = cands[1] if len(cands) > 1 else None
        unique = T.check_uniqueness(best, second)
        gap = (best["s"] - second["s"]) if (best and second) else None
        last_top2 = [T.slim(c) for c in cands[:2]]

        T.log_event(a.log, "seek", event="attempt", desc=a.desc, n=attempts,
                    stateId=sid, unique=bool(unique),
                    topS=best["s"] if best else None, gap=gap, scrolled=scrolled)
        T.progress(f"[seek {attempts:02d}] state={sid} unique={unique} "
                   f"topS={best['s'] if best else None} gap={gap}")

        if unique:
            out = {"ok": True, "status": "found",
                   "coords": {"x": best["cx"], "y": best["cy"]},
                   "best": T.slim(best), "attempts": attempts,
                   "ms": int((time.time() - t0) * 1000)}
            T.log_event(a.log, "seek", event="found", desc=a.desc, **out)
            T.emit(out, 0)

        if last_state is not None and sid == last_state:
            stable += 1
        else:
            stable = 0
        last_state = sid

        x, y1, x2, y2, dur = T.scroll_swipe_args()
        T.adb("shell", "input", "swipe", str(x), str(y1), str(x2), str(y2), str(dur))
        time.sleep(1.0)
        if stable >= a.max_stuck:
            break
        time.sleep(0.6)

    reason = "stuck" if stable >= a.max_stuck else "timeout"
    out = {"ok": False, "status": "failed", "reason": reason,
           "attempts": attempts, "stateIds": state_ids[-20:],
           "lastTop2": last_top2, "ms": int((time.time() - t0) * 1000)}
    T.log_event(a.log, "seek", event="failed", desc=a.desc, **out)
    T.emit(out, 1)


if __name__ == "__main__":
    main()
