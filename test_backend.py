# -*- coding: utf-8 -*-
"""
backend.py（NDJSON 桥接层）协议测试

验证子进程协议本身：事件流顺序、plan/inspect 预读、错误路径、优雅取消。
不依赖任何 GUI 库，也不开窗口。

运行方式：  python -m unittest test_backend -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).with_name("backend.py")


class NDJSONBackendTestCase(unittest.TestCase):
    """以子进程方式驱动 backend.py，断言 NDJSON 事件流。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="sliceforge_backend_test_"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    # ---------- 工具 ----------

    def run_oneshot(self, *args: str):
        """跑一次性命令（plan / inspect），返回解析后的 JSON。"""
        proc = subprocess.run(
            [sys.executable, str(ROOT), *args],
            capture_output=True, text=True, cwd=str(self.work), check=False,
        )
        self.assertEqual(proc.returncode, 0, f"命令失败：{proc.stderr}")
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        self.assertTrue(lines, f"没有输出：{proc.stderr}")
        return json.loads(lines[-1])

    def run_stream(self, *args: str, cancel_after: float | None = None):
        """跑流式命令（split / merge），收集全部事件。

        cancel_after 给定时，启动该秒数后向 stdin 写入 cancel 模拟用户取消。
        """
        proc = subprocess.Popen(
            [sys.executable, str(ROOT), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, cwd=str(self.work),
        )
        events: list[dict] = []

        def reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        if cancel_after is not None:
            time.sleep(cancel_after)
            assert proc.stdin is not None
            try:
                proc.stdin.write("cancel\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # 进程可能已经结束
        reader_thread.join(timeout=60)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("backend 子进程 10 秒内没有退出")
        return events

    def make_file(self, name: str, size: int) -> Path:
        path = self.work / name
        block = b"\xa5" * (1024 * 1024)
        with open(path, "wb") as fp:
            written = 0
            while written < size:
                n = min(len(block), size - written)
                fp.write(block[:n])
                written += n
        return path

    # ---------- 一次性命令 ----------

    def test_plan_reports_parts_dir_and_manifest(self):
        src = self.make_file("demo.bin", 5 * 1024 * 1024)
        info = self.run_oneshot("plan", "--src", str(src))
        self.assertTrue(info["exists"])
        self.assertEqual(info["size"], 5 * 1024 * 1024)
        self.assertEqual(info["parts_dir"], str(self.work / "demo.bin.parts"))
        self.assertFalse(info["manifest_exists"])

    def test_plan_on_missing_file(self):
        info = self.run_oneshot("plan", "--src", str(self.work / "nope.bin"))
        self.assertFalse(info["exists"])
        self.assertIsNone(info["size"])

    def test_inspect_on_folder_without_manifest(self):
        info = self.run_oneshot("inspect", "--dir", str(self.work))
        self.assertFalse(info["ok"])
        self.assertIn("manifest", info["error"])

    def test_split_merge_roundtrip_and_protocol_order(self):
        src = self.make_file("roundtrip.bin", 5 * 1024 * 1024)

        split_events = self.run_stream("split", "--src", str(src), "--chunk", "2M")
        kinds = [e["type"] for e in split_events]
        self.assertEqual(kinds[-1], "done", f"最后一个事件应为 done：{kinds}")
        self.assertTrue(all(e["task"] == "split" for e in split_events))
        self.assertIn("progress", kinds)
        self.assertIn("log", kinds)
        done = split_events[-1]["result"]
        self.assertEqual(done["part_count"], 3)

        parts_dir = self.work / "roundtrip.bin.parts"
        manifest = json.loads((parts_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["part_count"], 3)

        info = self.run_oneshot("inspect", "--dir", str(parts_dir))
        self.assertTrue(info["ok"])
        self.assertEqual(info["part_count"], 3)
        self.assertEqual(info["missing"], [])
        self.assertTrue(info["out_exists"])  # 还原目标 = 原文件本身已存在

        merge_events = self.run_stream("merge", "--dir", str(parts_dir), "--overwrite")
        self.assertEqual(merge_events[-1]["type"], "done")
        self.assertTrue(all(e["task"] == "merge" for e in merge_events))
        self.assertTrue(merge_events[-1]["result"]["size_match"])
        stages = [e["stage"] for e in merge_events if e["type"] == "stage"]
        self.assertIn("verify", stages)
        self.assertIn("merge", stages)

    def test_invalid_chunk_size_emits_error(self):
        src = self.make_file("bad.bin", 1024)
        events = self.run_stream("split", "--src", str(src), "--chunk", "abc")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("每片大小", events[-1]["message"])

    def test_graceful_cancel_cleans_partial_parts(self):
        src = self.make_file("big.bin", 64 * 1024 * 1024)
        events = self.run_stream("split", "--src", str(src), "--chunk", "1M",
                                 cancel_after=0.05)
        self.assertEqual(events[-1]["type"], "cancelled",
                         f"应立即收到 cancelled：{events[-1]}")
        leftovers = os.listdir(self.work / "big.bin.parts")
        self.assertEqual(leftovers, [], "取消后不应留下未完成切片")

    def test_merge_with_missing_parts_reports_error(self):
        src = self.make_file("hole.bin", 3 * 1024 * 1024)
        self.run_stream("split", "--src", str(src), "--chunk", "1M")
        parts_dir = self.work / "hole.bin.parts"
        (parts_dir / "hole.bin.part0002").unlink()
        events = self.run_stream("merge", "--dir", str(parts_dir), "--overwrite")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("缺失", events[-1]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
