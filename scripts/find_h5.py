#!/usr/bin/env python3
"""find_h5.py — 复合层·H5：WebView 页面经 CDP 定位（socket 直探，无 debug 包判断）。

触发前提：find.py 报 webviewStale=true（uiautomator 对该页失效）。
stuck 判定用 href|scrollY 指纹（native stateId 在 H5 页面恒定，会误判）。
坐标换算含 x/y 容器偏移：screen = webViewLeft/Top + css * dpr。
只返回坐标，绝不执行操作（找/动分离）。
"""
import argparse
import json
import os
import subprocess
import time

import adb_tools as T

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DUMP = "/tmp/explorer/h5-dump.xml"
EXPR_FILE = "/tmp/explorer/h5-expr.js"


def node_available():
    """Node >= 21（原生 WebSocket）。入口检查一次，循环内不重复。"""
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False
        r2 = subprocess.run(["node", "-e", "console.log(typeof WebSocket)"],
                            capture_output=True, text=True, timeout=5)
        return r2.stdout.strip() == "function"
    except Exception:
        return False


def probe_cdp():
    """socket 直探 → tcp:0 临时端口 → /json 取 page target。返回 (port, targetId) 或 (None, reason)。"""
    pkg = T.get_current_package()
    if not pkg or pkg == "unknown":
        return None, "package_unknown"
    rc, out, _ = T.adb("shell", "pidof", pkg, timeout=5)
    pid = out.split()[0] if out.split() else ""
    if rc != 0 or not pid:
        return None, "process_not_running"
    rc, out, _ = T.adb("shell", "cat", "/proc/net/unix", timeout=5)
    if rc != 0 or f"webview_devtools_remote_{pid}" not in out:
        return None, "cdp_socket_not_found"
    rc, out, _ = T.adb("forward", "tcp:0", f"localabstract:webview_devtools_remote_{pid}", timeout=5)
    port = out.strip()
    if rc != 0 or not port.isdigit():
        return None, "forward_failed"
    try:
        targets = T.http_get_json(f"http://127.0.0.1:{port}/json")
    except Exception:
        return None, "http_failed"
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return None, "no_page_target"
    return port, pages[0]["id"]


def build_js(keywords, left, top):
    esc = [k.replace("'", "\\'") for k in keywords]
    return """(function() {
  const dpr = window.devicePixelRatio || 1;
  const webViewLeftPhysical = """ + str(left) + """;
  const webViewTopPhysical = """ + str(top) + """;
  const keywords = ['""" + "', '".join(esc) + """'];

  const candidates = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let node;
  while (node = walker.nextNode()) {
    if (node.textContent) {
      if (keywords.every(kw => node.textContent.includes(kw))) {
        const rect = node.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          candidates.push({ node: node, rect: rect, area: rect.width * rect.height });
        }
      }
    }
  }
  if (candidates.length === 0) {
    return { found: false,
             fingerprint: location.href + '|' + window.scrollY,
             scrollY: window.scrollY, innerHeight: window.innerHeight };
  }
  candidates.sort((a, b) => a.area - b.area);
  const cand = candidates[0];
  const node = cand.node, rect = cand.rect;

  let clickableElement = null, clickableRect = null, depth = 0, cur = node;
  while (cur && cur !== document.body && depth < 10) {
    const style = getComputedStyle(cur);
    const cRect = cur.getBoundingClientRect();
    if ((style.cursor === 'pointer' || cur.onclick || ['A','BUTTON'].includes(cur.tagName))
        && cRect.width > 0 && cRect.height > 0) {
      clickableElement = cur; clickableRect = cRect; break;
    }
    cur = cur.parentElement; depth++;
  }
  if (!clickableElement) { clickableElement = node; clickableRect = rect; }

  return {
    found: true,
    text: node.textContent.trim().substring(0, 200),
    cssCx: Math.round(clickableRect.x + clickableRect.width / 2),
    cssCy: Math.round(clickableRect.y + clickableRect.height / 2),
    inViewport: clickableRect.y >= 0 && clickableRect.y < window.innerHeight,
    tagName: clickableElement.tagName,
    className: (clickableElement.className || '').substring(0, 80),
    dpr: dpr,
    screenX: webViewLeftPhysical + Math.round(clickableRect.x + clickableRect.width / 2) * dpr,
    screenY: webViewTopPhysical + Math.round(clickableRect.y + clickableRect.height / 2) * dpr,
    fingerprint: location.href + '|' + window.scrollY,
    scrollY: window.scrollY, innerHeight: window.innerHeight
  };
})()"""


def run_cdp(port, target_id):
    cmd = ["node", os.path.join(SCRIPT_DIR, "cdp.mjs"), EXPR_FILE]
    if target_id:
        cmd.append(target_id)
    cmd.append(port)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.stderr:
        T.progress(f"[cdp] {r.stderr.strip()[:200]}")
    content = r.stdout.strip()
    if not content:
        return None
    try:
        resp = json.loads(content)
    except Exception:
        return None
    return (resp.get("result") or {}).get("value")


def main():
    p = argparse.ArgumentParser(description="seek a DOM element via CDP (WebView pages)")
    p.add_argument("desc")
    p.add_argument("--target-id", dest="target_id", default=None)
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--max-stuck", type=int, default=6)
    p.add_argument("--log", default="explorer.jsonl")
    a = p.parse_args()

    t0 = time.time()

    def failed(reason, extra=None):
        out = {"ok": False, "status": "failed", "mode": "cdp", "reason": reason,
               "attempts": attempts, "fingerprints": fingerprints[-20:],
               "ms": int((time.time() - t0) * 1000)}
        if extra:
            out.update(extra)
        T.log_event(a.log, "find_h5", event="failed", desc=a.desc, **out)
        T.emit(out, 1)

    # 1) 外部依赖（入口一次）
    if not node_available():
        T.log_event(a.log, "find_h5", event="failed", desc=a.desc,
                    reason="node_unavailable", mode="cdp")
        T.progress("需要 Node >= 21（原生 WebSocket），请升级 Node 后重试")
        attempts, fingerprints = 0, []
        failed("node_unavailable")

    # 2) CDP 探测（App 重启后 PID 变化，每次重新 forward；tcp:0 临时端口无冲突）
    port, target_or_reason = probe_cdp()
    if port is None:
        attempts, fingerprints = 0, []
        failed(target_or_reason)
    target_id = a.target_id or target_or_reason

    # 3) 容器 bounds（入口 dump 取一次，循环内复用）
    ok, _ = T.dump_primitive(TMP_DUMP)
    bounds = T.webview_container_bounds(TMP_DUMP) if ok else None
    if bounds is None:
        attempts, fingerprints = 0, []
        failed("webview_container_not_found")
    left, top = bounds[0], bounds[1]
    T.progress(f"[h5] port={port} target={target_id} container=({left},{top})")

    keywords = T.keywords_of(a.desc)
    attempts = 0
    stable = 0
    prev_fp = None
    fingerprints = []
    last_value = None

    while time.time() - t0 < a.timeout:
        attempts += 1
        with open(EXPR_FILE, "w", encoding="utf-8") as f:
            f.write(build_js(keywords, left, top))
        value = run_cdp(port, target_id)
        fp = (value or {}).get("fingerprint") if isinstance(value, dict) else None
        if fp:
            fingerprints.append(fp)
        T.log_event(a.log, "find_h5", event="attempt", desc=a.desc, n=attempts,
                    mode="cdp", targetId=target_id, port=port,
                    fingerprint=fp, found=bool(value and value.get("found")))
        T.progress(f"[h5 {attempts:02d}] found={bool(value and value.get('found'))} "
                   f"inViewport={bool(value and value.get('inViewport'))} fp={fp}")

        if value and value.get("found"):
            if value.get("inViewport"):
                coords = {"x": int(value["screenX"]), "y": int(value["screenY"])}
                out = {"ok": True, "status": "found", "mode": "cdp",
                       "coords": coords,
                       "element": {"text": value.get("text"), "tag": value.get("tagName"),
                                   "class": value.get("className"), "dpr": value.get("dpr"),
                                   "cssCx": value.get("cssCx"), "cssCy": value.get("cssCy")},
                       "attempts": attempts, "ms": int((time.time() - t0) * 1000)}
                T.log_event(a.log, "find_h5", event="found", desc=a.desc, **out)
                T.emit(out, 0)
            # 命中但离屏 → 慢滑一屏重试
        else:
            if fp and prev_fp is not None and fp == prev_fp:
                stable += 1
            elif fp:
                stable = 0
            prev_fp = fp
            if stable >= a.max_stuck:
                last_value = value
                break

        x, y1, x2, y2, dur = T.scroll_swipe_args()
        T.adb("shell", "input", "swipe", str(x), str(y1), str(x2), str(y2), str(dur))
        time.sleep(1.0)
        time.sleep(0.6)
        last_value = value

    reason = "stuck" if stable >= a.max_stuck else "timeout"
    out = {"ok": False, "status": "failed", "mode": "cdp", "reason": reason,
           "attempts": attempts, "fingerprints": fingerprints[-20:],
           "lastElement": last_value if isinstance(last_value, dict) else None,
           "ms": int((time.time() - t0) * 1000)}
    T.log_event(a.log, "find_h5", event="failed", desc=a.desc, **out)
    T.emit(out, 1)


if __name__ == "__main__":
    main()
