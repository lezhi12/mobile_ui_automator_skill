#!/usr/bin/env python3
"""共享原语库：所有工具的唯一实现处（禁止各工具复制逻辑导致实现漂移）。

包含：adb 封装、dump 原语、UI 树解析与确定性打分、唯一性判定、状态指纹、
慢滑语义（按设备度量比例派生）、JSONL 日志、urllib HTTP 探测。
"""
import sys, os, re, json, time, hashlib, subprocess
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

UNIQUENESS = 150
STOPWORDS = {"的", "了", "个", "按钮", "图标", "选项", "项", "页", "页签", "标签",
             "界面", "栏", "菜单", "入口", "点击", "进入", "打开"}
DYNAMIC_RE = re.compile(r"^\+?\d+$|^\(\d+\)$|^\d+条$|未读|消息.*\d+")

# 设备度量：运行探测 + 进程内缓存（失败回落 1080x2400）
_metrics_cache = None


def adb(*args, timeout=30):
    r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def now_ts():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(log_path, tool, **fields):
    """追加一行 JSONL（UTF-8、ensure_ascii=False、ts=ISO8601 本地时区）。"""
    if not log_path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(log_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        rec = {"ts": now_ts(), "tool": tool, **fields}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # 日志失败不阻断主流程，只报 stderr
        print(f"[log] append failed: {e}", file=sys.stderr)


def emit(obj, code=0):
    """stdout 永远恰好一个 JSON 对象；exit code 由调用方决定。"""
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def progress(msg):
    print(msg, file=sys.stderr, flush=True)


def screen_metrics():
    """wm size 探测 W/H 并缓存；打分 onscreen 与 scroll 坐标按比例派生，不硬编码。"""
    global _metrics_cache
    if _metrics_cache is None:
        w, h = 1080, 2400
        try:
            rc, out, _ = adb("shell", "wm", "size", timeout=5)
            m = re.search(r"(\d+)x(\d+)", out)
            if rc == 0 and m:
                w, h = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        _metrics_cache = {"w": w, "h": h}
    return _metrics_cache


def scroll_swipe_args():
    """探索翻找慢滑：0.5W,0.75H -> 0.5W,0.33H @1000ms（1px/ms 慢速无惯性，幅度<=视口）。"""
    m = screen_metrics()
    x = m["w"] // 2
    return x, int(m["h"] * 0.75), x, int(m["h"] * 0.33), 1000


def get_activity():
    for pat in ("mCurrentFocus", "mFocusedApp", "mResumedActivity"):
        rc, out, _ = adb("shell", "dumpsys", "window")
        m = re.search(rf"{pat}[^\n]*?([\w.]+/[\w.$]+)", out)
        if m:
            return m.group(1)
    return "unknown"


def get_current_package():
    activity = get_activity()
    if "/" in activity:
        return activity.split("/")[0]
    return None


def dump_primitive(local_path):
    """2 次重试 + <hierarchy> 校验 + 1.5s 退避。所有 dump 调用的统一入口。"""
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    for attempt in range(2):
        adb("shell", "uiautomator", "dump", "/sdcard/wd_tmp.xml")
        adb("pull", "/sdcard/wd_tmp.xml", local_path)
        try:
            with open(local_path, encoding="utf-8") as f:
                content = f.read()
            if len(content) > 0 and "<hierarchy" in content:
                return True, len(content)
        except FileNotFoundError:
            pass
        time.sleep(1.5)
    return False, 0


def parse_nodes(xml_path):
    tree = ET.parse(xml_path)
    parent_map = {c: p for p in tree.iter() for c in p}
    nodes = []
    for el in tree.iter("node"):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        nodes.append({
            "rid": el.get("resource-id", "") or "",
            "text": el.get("text", "") or "",
            "cd": el.get("content-desc", "") or "",
            "class": el.get("class", "") or "",
            "clickable": el.get("clickable", "false") == "true",
            "focusable": el.get("focusable", "false") == "true",
            "longclickable": el.get("long-clickable", "false") == "true",
            "bounds": [x1, y1, x2, y2],
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "area": (x2 - x1) * (y2 - y1),
            "_el": el,
        })
    el2n = {n["_el"]: n for n in nodes}
    for n in nodes:
        p = parent_map.get(n["_el"])
        n["_parent"] = el2n.get(p) if p is not None else None
    return nodes


def visible(n):
    x1, y1, x2, y2 = n["bounds"]
    if x1 == 0 and y1 == 0 and x2 == 0 and y2 == 0:
        return False
    return n["area"] > 1


def resolve_view(n):
    """零尺寸文本节点（典型：底部 tab CheckedTextView）回溯最近可见祖先。"""
    if visible(n):
        return n
    p = n.get("_parent")
    hops = 0
    while p is not None and hops < 8:
        if visible(p):
            proxy = dict(n)
            proxy["bounds"] = p["bounds"]; proxy["cx"] = p["cx"]; proxy["cy"] = p["cy"]
            proxy["area"] = p["area"]; proxy["class"] = p["class"]
            proxy["clickable"] = p["clickable"] or n["clickable"]
            proxy["focusable"] = p["focusable"] or n["focusable"]
            proxy["longclickable"] = p["longclickable"] or n["longclickable"]
            proxy["resolved"] = True
            proxy["ancestorRid"] = p["rid"]
            return proxy
        p = p.get("_parent"); hops += 1
    return None


def state_id(xml_path, activity):
    """状态指纹：Activity + 顶层可见节点签名，sha256 前 16 位。只用于变化检测。"""
    try:
        nodes = parse_nodes(xml_path)
    except Exception:
        nodes = []
    sig_parts = []
    for n in nodes:
        if n["rid"] or n["text"] or n["cd"]:
            sig_parts.append("|".join([
                n["rid"][:24].strip(), n["text"][:24].strip(),
                n["cd"][:24].strip(), n["class"][:24].strip()]))
        if len(sig_parts) >= 15:
            break
    raw = activity + "||" + ";;".join(sig_parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def keywords_of(desc):
    kws = []
    for part in re.split(r"[\s、,，→>\-]+", desc):
        core = part
        for sw in sorted(STOPWORDS, key=len, reverse=True):
            core = core.replace(sw, "")
        if core and core not in kws:
            kws.append(core)
    return kws or [desc]


def rid_tail(rid):
    return rid.split(":id/")[-1] if ":id/" in rid else rid.split("/")[-1]


def score_node(n, kws):
    s = 0
    reasons = []
    if not visible(n):
        return None
    rid_t = rid_tail(n["rid"]).lower()
    rid_parts = re.split(r"[_\W]+", rid_t)
    text = n["text"].replace("\u3000", "").replace("\n", "").replace(" ", "").strip().lower()
    cd = n["cd"].replace("\u3000", "").replace("\n", "").replace(" ", "").strip().lower()
    kl = [k.lower() for k in kws]

    if kl and rid_t and rid_t in kl:
        s += 1200; reasons.append("rid==kw(+1200)")
    else:
        hit_parts = [k for k in kl if any(k and k in p for p in rid_parts if p)]
        if rid_t and hit_parts:
            s += 900; reasons.append("rid~kw(+900)")
        elif rid_t and any(k and (rid_t.startswith(k) or rid_t.endswith(k)) for k in kl):
            s += 850; reasons.append("rid~prefix/suffix(+850)")

    if text:
        if text in kl:
            s += 700; reasons.append("text==kw(+700)")
        elif all(k in text for k in kl if k):
            s += 600; reasons.append("text~ALL(+600)")
        else:
            or_hits = sum(1 for k in kl if k and k in text)
            if or_hits:
                s += 250 * or_hits; reasons.append(f"text~OR x{or_hits}(+{250*or_hits})")

    if cd:
        if cd in kl:
            s += 550; reasons.append("cd==kw(+550)")
        elif any(k and k in cd for k in kl):
            s += 480; reasons.append("cd~kw(+480)")

    for k in kl:
        if re.fullmatch(r"[a-z0-9_]+", k or "") and k in n["class"].lower().split(".")[-1]:
            s += 250; reasons.append("class~kw(+250)")
            break

    if n["clickable"] or n["focusable"] or n["longclickable"]:
        s += 60; reasons.append("clickable(+60)")
    m = screen_metrics()
    x1, y1, x2, y2 = n["bounds"]
    if 0 <= x1 and 0 <= y1 and x2 <= m["w"] and y2 <= m["h"]:
        s += 40; reasons.append("onscreen(+40)")
    if n["area"] < 20 * 20:
        s -= 80; reasons.append("tiny(-80)")
    if DYNAMIC_RE.search(n["text"].strip()):
        s -= 120; reasons.append("dynamic(-120)")
    if s <= 0:
        return None
    return s, reasons


def rank(xml_path, desc):
    kws = keywords_of(desc)
    cands = []
    seen = {}
    for n in parse_nodes(xml_path):
        v = resolve_view(n)
        if v is None:
            continue
        r = score_node(v, kws)
        if r:
            s, reasons = r
            c = {"s": s, "reasons": reasons, **v}
            key = (tuple(c["bounds"]), c["text"], c["cd"])
            if key in seen:
                idx = seen[key]
                if s > cands[idx]["s"]:
                    cands[idx] = c
                continue
            seen[key] = len(cands)
            cands.append(c)
    cands.sort(key=lambda c: -c["s"])
    return kws, cands


def slim(c):
    out = {"s": c["s"], "rid": c["rid"], "text": c["text"], "cd": c["cd"],
           "class": c["class"].split(".")[-1], "bounds": c["bounds"],
           "cx": c["cx"], "cy": c["cy"], "reasons": c["reasons"]}
    if c.get("resolved"):
        out["resolvedToAncestor"] = True
        if c.get("ancestorRid"):
            out["ancestorRid"] = c["ancestorRid"]
    return out


def is_exact_match(reasons):
    return any("text==kw(+700)" in r or "rid==kw(+1200)" in r for r in reasons)


def is_contain_match(reasons):
    return any("text~ALL(+600)" in r or "cd~kw(+480)" in r for r in reasons)


def check_uniqueness(best, second):
    """U=150 阈值 + exact-match-first 例外（best 完全匹配档 vs second 仅包含匹配档）。"""
    if best is None:
        return False
    if second is None:
        return True
    raw_gap = best["s"] - second["s"]
    if raw_gap >= UNIQUENESS:
        return True
    if is_exact_match(best.get("reasons", [])) and is_contain_match(second.get("reasons", [])):
        return True
    return False


def webview_stale(xml_path):
    """H5 路由信号：存在 WebView 容器且其所有后代节点均无可见 text/resource-id。"""
    try:
        tree = ET.parse(xml_path)
    except Exception:
        return False
    for el in tree.iter("node"):
        if "WebView" not in (el.get("class", "") or ""):
            continue
        descendants = [d for d in el.iter("node") if d is not el]
        if not any((d.get("text") or "").strip() or (d.get("resource-id") or "").strip()
                   for d in descendants):
            return True
    return False


def webview_container_bounds(xml_path):
    """WebView 容器物理 bounds (x1,y1,x2,y2)；找不到或零尺寸返回 None。"""
    try:
        tree = ET.parse(xml_path)
    except Exception:
        return None
    for el in tree.iter("node"):
        if "WebView" not in (el.get("class", "") or ""):
            continue
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds", ""))
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            if (x2 - x1) * (y2 - y1) > 1:
                return (x1, y1, x2, y2)
    return None


def http_get_json(url, timeout=3):
    """urllib 实现（替代 curl），供 CDP HTTP 探测用。"""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# text 转义：空格 -> %s；剥离设备侧 shell 元字符（act.py 与 gen.py 必须共用此函数）
_TEXT_META = set("'\"`$;&|<>\\*?!(){}[]~^\n\r\t#")


def escape_text(s):
    parts = []
    for ch in s:
        if ch == " ":
            parts.append("%s")
        elif ch in _TEXT_META:
            continue
        else:
            parts.append(ch)
    return "".join(parts)


def is_ascii(s):
    return all(ord(c) < 128 for c in s)
