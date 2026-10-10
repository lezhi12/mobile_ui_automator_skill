import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE, "scripts")
FIX = os.path.join(BASE, "tests", "fixtures")
sys.path.insert(0, SCRIPTS)

import adb_tools as T  # noqa: E402


def run_tool(args, cwd=BASE):
    r = subprocess.run([sys.executable] + args, capture_output=True,
                       text=True, timeout=60, cwd=cwd)
    return r


class FindTests(unittest.TestCase):
    def find(self, fixture, desc):
        r = run_tool(["scripts/find.py", fixture, desc, "--log",
                      os.path.join(tempfile.mkdtemp(), "t.jsonl")])
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_tab_proxy_resolves_to_ancestor(self):
        out = self.find(os.path.join(FIX, "find/tab_proxy.xml"), "我的")
        self.assertTrue(out["ok"])
        self.assertTrue(out["unique"])
        self.assertEqual(out["reason"], "best-second>=150")
        self.assertTrue(out["best"]["resolvedToAncestor"])
        self.assertEqual((out["best"]["cx"], out["best"]["cy"]), (540, 2340))
        self.assertFalse(out["webviewStale"])

    def test_ambiguous_not_unique(self):
        out = self.find(os.path.join(FIX, "find/ambiguous.xml"), "确认")
        self.assertTrue(out["ok"])
        self.assertFalse(out["unique"])
        self.assertEqual(out["reason"], "ambiguous")
        self.assertEqual(out["gap"], 0)

    def test_exact_match_first_exception(self):
        out = self.find(os.path.join(FIX, "find/exact_first.xml"), "寄快递")
        self.assertTrue(out["ok"])
        self.assertTrue(out["unique"])
        self.assertEqual(out["reason"], "exact-match-first")

    def test_webview_stale_signal(self):
        out = self.find(os.path.join(FIX, "find/webview_stale.xml"), "任意")
        # 容器自身 onscreen(+40) 会成为候选（唯一性判定也是数据），但路由信号是 webviewStale
        self.assertTrue(out["webviewStale"])
        self.assertFalse(out["unique"])

    def test_webview_alive_not_stale(self):
        out = self.find(os.path.join(FIX, "find/webview_alive.xml"), "网络诊断")
        self.assertTrue(out["ok"])
        self.assertTrue(out["unique"])
        self.assertFalse(out["webviewStale"])

    def test_parse_failure_exit_1(self):
        r = run_tool(["scripts/find.py", "/nonexistent.xml", "x", "--log",
                      os.path.join(tempfile.mkdtemp(), "t.jsonl")])
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])


class AdbToolsUnitTests(unittest.TestCase):
    def test_escape_text(self):
        self.assertEqual(T.escape_text("hello world"), "hello%sworld")
        self.assertEqual(T.escape_text("a;b'c"), "abc")
        self.assertEqual(T.escape_text("50%"), "50%")

    def test_is_ascii(self):
        self.assertTrue(T.is_ascii("abc 123"))
        self.assertFalse(T.is_ascii("中文"))

    def test_state_id_deterministic_and_sensitive(self):
        xml = os.path.join(FIX, "find/tab_proxy.xml")
        s1 = T.state_id(xml, "com.foo/.Main")
        s2 = T.state_id(xml, "com.foo/.Main")
        s3 = T.state_id(xml, "com.foo/.Other")
        self.assertEqual(s1, s2)
        self.assertNotEqual(s1, s3)

    def test_screen_metrics_fallback_shape(self):
        m = T.screen_metrics()
        self.assertIn("w", m)
        self.assertIn("h", m)
        x, y1, x2, y2, dur = T.scroll_swipe_args()
        self.assertEqual(dur, 1000)
        self.assertLess(y2, y1)
        self.assertLessEqual(max(y1, y2), m["h"])


class GenTests(unittest.TestCase):
    def gen(self, fixture_name, steps, goal="打开菜鸟裹裹并点击寄快递"):
        tmpd = tempfile.mkdtemp()
        log = os.path.join(tmpd, fixture_name)
        shutil.copy(os.path.join(FIX, "gen", fixture_name), log)
        out = os.path.join(tmpd, "out.sh")
        r = run_tool(["scripts/gen.py", "--log", log, "--steps", steps,
                      "--out", out, "--goal", goal])
        return r, out, log

    def test_full_path_golden(self):
        r, out, _ = self.gen("gen_full.jsonl", "1,2,3")
        self.assertEqual(r.returncode, 0, r.stderr)
        body = json.loads(r.stdout)
        self.assertTrue(body["ok"])
        self.assertTrue(body["warn"])  # step2 重试违反单 op 单 step → 告警
        with open(out, encoding="utf-8") as f:
            script = f.read()
        # 重试去重：只采信最近一条 tap(540,2100)，第一条(600,2110)不出现
        self.assertIn('input tap "540" "2100"', script)
        self.assertNotIn("600", script)
        # launch → force-stop + monkey + wait_activity IndexActivity
        self.assertIn('am force-stop "com.cainiao.wireless"', script)
        self.assertIn('monkey -p "com.cainiao.wireless"', script)
        self.assertIn('wait_activity "com.cainiao.wireless/.ui.IndexActivity" 12', script)
        # step2 activity 变化 → wait_activity NextActivity
        self.assertIn('wait_activity "com.cainiao.wireless/.ui.NextActivity" 12', script)
        # step3 activity 变回 → wait_activity IndexActivity
        self.assertIn("input keyevent KEYCODE_BACK", script)
        # assertion = 最后一个关闭 verify 的 activity
        self.assertIn(".ui.IndexActivity", script)
        self.assertNotIn("<EXPECTED_ACTIVITY_FRAGMENT>", script)
        self.assertNotIn("<RUN_ID>", script)
        self.assertTrue(os.access(out, os.X_OK))
        # golden 对比（首次运行生成 golden，其后一致；日志绝对路径每次运行不同 → 归一化）
        golden = os.path.join(FIX, "gen/gen_full.golden.sh")
        if not os.path.exists(golden):
            shutil.copy(out, golden)
        with open(golden, encoding="utf-8") as f:
            golden_text = f.read()
        norm = lambda s: re.sub(r"^# Evidence root: log: .*$",
                                "# Evidence root: log: <LOG>", s, flags=re.M)
        self.assertEqual(norm(script), norm(golden_text))

    def test_missing_verify_rejected(self):
        src = os.path.join(FIX, "gen/gen_full.jsonl")
        with open(src, encoding="utf-8") as f:
            lines = f.read().splitlines()
        tmpd = tempfile.mkdtemp()
        log = os.path.join(tmpd, "gen_full.jsonl")
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[:-1]) + "\n")  # 去掉 step3 的关闭 verify
        out = os.path.join(tmpd, "out.sh")
        r = run_tool(["scripts/gen.py", "--log", log, "--steps", "1,2,3",
                      "--out", out, "--goal", "g"])
        self.assertEqual(r.returncode, 1)
        body = json.loads(r.stdout)
        self.assertEqual(body["error"], "unverified_steps")
        self.assertEqual(body["missing"], ["3"])

    def test_changed_null_counts_as_closing(self):
        r, out, _ = self.gen("gen_full.jsonl", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
