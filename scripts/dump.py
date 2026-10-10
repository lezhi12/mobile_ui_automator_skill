#!/usr/bin/env python3
"""dump.py — 原子层·看/验证：dump + activity + stateId；--verify-step 时兼任验证。

stdout 契约：
  普通:  {"ok":true,"activity":..,"stateId":..,"dumpPath":..,"dumpBytes":..,"ms":..}
  验证:  附加 {"verifyStep":N,"expectState":S|null,"changed":true|false|null}
exit: dump 失败 1；changed=false 是数据不是错误，exit 0。
"""
import argparse
import time

import adb_tools as T


def main():
    p = argparse.ArgumentParser(description="dump + activity + stateId (verify mode optional)")
    p.add_argument("--out", default="/tmp/explorer/dump.xml")
    p.add_argument("--verify-step", dest="verify_step", default=None,
                   help="验证模式：step 不透明标签（如 2 或 2b）")
    p.add_argument("--expect-state", dest="expect_state", default=None,
                   help="对比基准 stateId；省略则 changed=null（agent 显式放行）")
    p.add_argument("--log", default="explorer.jsonl")
    a = p.parse_args()

    t0 = time.time()
    ok, size = T.dump_primitive(a.out)
    ms = int((time.time() - t0) * 1000)
    if not ok:
        T.log_event(a.log, "dump", ok=False, error="dump_failed", ms=ms)
        T.emit({"ok": False, "error": "dump_failed", "ms": ms}, 1)

    activity = T.get_activity()
    sid = T.state_id(a.out, activity)
    out = {"ok": True, "activity": activity, "stateId": sid,
           "dumpPath": a.out, "dumpBytes": size, "ms": ms}

    if a.verify_step is not None:
        changed = None if a.expect_state is None else (sid != a.expect_state)
        out.update({"verifyStep": a.verify_step, "expectState": a.expect_state,
                    "changed": changed})
        if changed is False:
            T.progress("[verify] stateId unchanged — 操作可能未生效")

    T.log_event(a.log, "dump", **out)
    T.emit(out, 0)


if __name__ == "__main__":
    main()
