#!/usr/bin/env python3
"""find.py — 原子层·找（纯函数，不碰设备）：打分 + unique 判定 + webviewStale 提示。

stdout: {"ok","keywords","unique","gap","webviewStale","best","runnerUp","top","reason"}
exit: XML 解析失败 1；unique=false / webviewStale=true / 无候选均为数据，exit 0。
硬规则：unique=false 禁止对 best 坐标执行 act（宁可失败也不点错）。
"""
import argparse

import adb_tools as T


def main():
    p = argparse.ArgumentParser(description="score candidates in a dump XML")
    p.add_argument("xml", help="dump XML 文件路径")
    p.add_argument("desc", help="自然语言描述")
    p.add_argument("--log", default="explorer.jsonl")
    a = p.parse_args()

    try:
        kws, cands = T.rank(a.xml, a.desc)
        stale = T.webview_stale(a.xml)
    except Exception as e:
        T.log_event(a.log, "find", desc=a.desc, ok=False, error="parse_failed")
        T.emit({"ok": False, "error": "parse_failed", "detail": str(e)}, 1)

    best = cands[0] if cands else None
    second = cands[1] if len(cands) > 1 else None
    unique = T.check_uniqueness(best, second)
    gap = (best["s"] - second["s"]) if (best and second) else None

    if best is None:
        reason = "no-candidate"
    elif second is None:
        reason = "唯一命中"
    elif gap >= T.UNIQUENESS:
        reason = "best-second>=150"
    elif T.is_exact_match(best["reasons"]) and T.is_contain_match(second["reasons"]):
        reason = "exact-match-first"
    else:
        reason = "ambiguous"

    out = {"ok": bool(cands), "keywords": kws, "unique": bool(unique), "gap": gap,
           "webviewStale": bool(stale),
           "best": T.slim(best) if best else None,
           "runnerUp": T.slim(second) if second else None,
           "top": [T.slim(c) for c in cands[:5]],
           "reason": reason}
    T.log_event(a.log, "find", desc=a.desc, keywords=kws, unique=bool(unique),
                gap=gap, webviewStale=bool(stale),
                topS=best["s"] if best else None,
                top=[T.slim(c) for c in cands[:5]])
    T.emit(out, 0)


if __name__ == "__main__":
    main()
