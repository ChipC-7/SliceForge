# -*- coding: utf-8 -*-
"""
SliceForge 核心逻辑单元测试

只测试与界面无关的纯逻辑：大小解析、命名规则、切割、清单、校验、合并、取消。
这些函数都在 core.py 中，不依赖任何 GUI 库；界面（Tauri + Vue）侧的
桥接协议另见 test_backend.py。

运行方式：  python -m unittest test_sliceforge -v
"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("core.py")


def _load_module():
    """按文件路径加载被测的核心逻辑模块。"""
    spec = importlib.util.spec_from_file_location("sliceforge_core_under_test", str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _make_file(path, size, seed=b"x"):
    """生成指定大小的测试文件（内容可复现）。"""
    block = (seed * 4096)[:4096]
    with open(path, "wb") as fp:
        remaining = size
        while remaining > 0:
            chunk = block[:min(len(block), remaining)]
            fp.write(chunk)
            remaining -= len(chunk)
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 每片大小解析
# --------------------------------------------------------------------------- #

class TestParseChunkSize(unittest.TestCase):
    """每片大小输入解析：支持带单位与纯字节数，非法输入要友好报错。"""

    def test_unit_megabyte(self):
        self.assertEqual(mod.parse_chunk_size("5M"), 5 * 1024 * 1024)

    def test_unit_kilobyte(self):
        self.assertEqual(mod.parse_chunk_size("512K"), 512 * 1024)

    def test_unit_gigabyte(self):
        self.assertEqual(mod.parse_chunk_size("2G"), 2 * 1024 * 1024 * 1024)

    def test_plain_bytes_string(self):
        self.assertEqual(mod.parse_chunk_size("2048"), 2048)

    def test_plain_bytes_int(self):
        self.assertEqual(mod.parse_chunk_size(4096), 4096)

    def test_fractional_value(self):
        self.assertEqual(mod.parse_chunk_size("1.5M"), 1572864)

    def test_case_and_space_insensitive(self):
        self.assertEqual(mod.parse_chunk_size("  5 m  "), 5 * 1024 * 1024)

    def test_optional_b_suffix(self):
        self.assertEqual(mod.parse_chunk_size("5MB"), 5 * 1024 * 1024)
        self.assertEqual(mod.parse_chunk_size("5mb"), 5 * 1024 * 1024)
        self.assertEqual(mod.parse_chunk_size("5 B"), 5)

    def test_terabyte_and_petabyte(self):
        self.assertEqual(mod.parse_chunk_size("1T"), 1024 ** 4)
        self.assertEqual(mod.parse_chunk_size("1P"), 1024 ** 5)

    def test_zero_raises(self):
        with self.assertRaises(mod.SplitError):
            mod.parse_chunk_size("0")
        with self.assertRaises(mod.SplitError):
            mod.parse_chunk_size("0M")

    def test_negative_raises(self):
        with self.assertRaises(mod.SplitError):
            mod.parse_chunk_size("-5M")

    def test_illegal_input_raises(self):
        for bad in ("", "   ", "abc", "5X", "M", "5 5M", "1.2.3M", None, object()):
            with self.assertRaises(mod.SplitError, msg="未拒绝非法输入 %r" % (bad,)):
                mod.parse_chunk_size(bad)

    def test_bool_is_rejected(self):
        with self.assertRaises(mod.SplitError):
            mod.parse_chunk_size(True)

    def test_error_message_is_friendly(self):
        """报错文本要告诉用户怎么写，而不是抛栈信息。"""
        try:
            mod.parse_chunk_size("abc")
        except mod.SplitError as exc:
            message = str(exc)
            self.assertIn("每片大小", message)
            self.assertIn("示例", message)
        else:
            self.fail("应抛出 SplitError")

    def test_sub_byte_fraction_rejected(self):
        with self.assertRaises(mod.SplitError):
            mod.parse_chunk_size("0.5")


# --------------------------------------------------------------------------- #
# 命名规则
# --------------------------------------------------------------------------- #

class TestNaming(unittest.TestCase):
    """输出目录与切片命名规则。"""

    def test_parts_dir_suffix(self):
        got = mod.parts_dir_for(Path("D:/data/video.mp4"))
        self.assertEqual(got, Path("D:/data/video.mp4.parts"))

    def test_parts_dir_keeps_original_directory(self):
        got = mod.parts_dir_for("/home/user/a/b.bin")
        self.assertEqual(str(got.parent), str(Path("/home/user/a")))

    def test_part_name_zero_padded_four_digits(self):
        self.assertEqual(mod.part_name("video.mp4", 3), "video.mp4.part0003")
        self.assertEqual(mod.part_name("video.mp4", 1), "video.mp4.part0001")

    def test_part_name_widens_beyond_9999_and_stays_sorted(self):
        total = 12000
        self.assertEqual(mod.part_name("a.bin", 5, total), "a.bin.part00005")
        self.assertEqual(mod.part_name("a.bin", 10000, total), "a.bin.part10000")
        names = [mod.part_name("a.bin", i, total) for i in range(1, total + 1)]
        self.assertEqual(names, sorted(names), "按文件名排序必须等于合并顺序")

    def test_part_name_rejects_bad_index(self):
        with self.assertRaises(ValueError):
            mod.part_name("a.bin", 0)

    def test_format_size(self):
        self.assertEqual(mod.format_size(0), "0 B")
        self.assertEqual(mod.format_size(512), "512 B")
        self.assertEqual(mod.format_size(1536), "1.50 KB")
        self.assertEqual(mod.format_size(5 * 1024 * 1024), "5.00 MB")
        self.assertEqual(mod.format_size(3 * 1024 ** 3), "3.00 GB")


# --------------------------------------------------------------------------- #
# 切割
# --------------------------------------------------------------------------- #

class TestSplit(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="sf_split_"))

    def tearDown(self):
        shutil.rmtree(str(self.work), ignore_errors=True)

    def test_split_produces_expected_parts_and_manifest(self):
        src = _make_file(self.work / "big.bin", 10 * 1024 * 1024)
        out_dir = self.work / "out"
        logs = []
        result = mod.split_file(src, chunk_size="4M", out_dir=out_dir,
                                log=logs.append)

        self.assertEqual(result["part_count"], 3)
        self.assertEqual(result["original_size"], 10 * 1024 * 1024)
        names = sorted(p.name for p in out_dir.iterdir())
        self.assertEqual(names, ["big.bin.part0001", "big.bin.part0002",
                                 "big.bin.part0003", "manifest.json"])
        self.assertEqual((out_dir / "big.bin.part0001").stat().st_size, 4 * 1024 * 1024)
        self.assertEqual((out_dir / "big.bin.part0003").stat().st_size, 2 * 1024 * 1024)

        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["original_name"], "big.bin")
        self.assertEqual(manifest["original_size"], 10 * 1024 * 1024)
        self.assertEqual(manifest["chunk_size"], 4 * 1024 * 1024)
        self.assertEqual(manifest["part_count"], 3)
        self.assertEqual(len(manifest["parts"]), 3)
        for item in manifest["parts"]:
            self.assertEqual(item["sha256"], _sha256(out_dir / item["name"]))
            self.assertEqual(item["size"], (out_dir / item["name"]).stat().st_size)
        self.assertTrue(any("完成" in line for line in logs))

    def test_default_output_dir_is_name_plus_parts(self):
        src = _make_file(self.work / "a.dat", 100)
        result = mod.split_file(src, chunk_size=64)
        self.assertEqual(Path(result["out_dir"]), mod.parts_dir_for(src))
        self.assertTrue(Path(result["out_dir"]).is_dir())

    def test_small_file_becomes_single_part(self):
        """文件小于单片大小时切成 1 片。"""
        src = _make_file(self.work / "small.bin", 10)
        result = mod.split_file(src, chunk_size="10M")
        self.assertEqual(result["part_count"], 1)
        out_dir = Path(result["out_dir"])
        self.assertTrue((out_dir / "small.bin.part0001").is_file())
        self.assertEqual((out_dir / "small.bin.part0001").stat().st_size, 10)

    def test_empty_file_raises_friendly_error(self):
        src = self.work / "empty.bin"
        src.write_bytes(b"")
        with self.assertRaises(mod.SplitError) as ctx:
            mod.split_file(src, chunk_size="1M")
        self.assertIn("空文件", str(ctx.exception))

    def test_missing_source_raises(self):
        with self.assertRaises(mod.SplitError) as ctx:
            mod.split_file(self.work / "nope.bin", chunk_size="1M")
        self.assertIn("找不到", str(ctx.exception))

    def test_directory_source_raises(self):
        with self.assertRaises(mod.SplitError):
            mod.split_file(self.work, chunk_size="1M")

    def test_illegal_chunk_size_raises_split_error(self):
        src = _make_file(self.work / "x.bin", 100)
        with self.assertRaises(mod.SplitError):
            mod.split_file(src, chunk_size="abc")
        with self.assertRaises(mod.SplitError):
            mod.split_file(src, chunk_size="0")

    def test_existing_manifest_requires_overwrite(self):
        src = _make_file(self.work / "x.bin", 300)
        out_dir = self.work / "out"
        mod.split_file(src, chunk_size=100, out_dir=out_dir)
        with self.assertRaises(mod.SplitError):
            mod.split_file(src, chunk_size=100, out_dir=out_dir)
        # 显式允许覆盖后应成功
        result = mod.split_file(src, chunk_size=100, out_dir=out_dir, overwrite=True)
        self.assertEqual(result["part_count"], 3)

    def test_progress_callback_reports_monotonic_bytes(self):
        src = _make_file(self.work / "p.bin", 1024 * 1024)
        seen = []
        mod.split_file(src, chunk_size="256K",
                       progress=lambda done, total, i, n: seen.append((done, total, i, n)))
        self.assertTrue(seen)
        dones = [s[0] for s in seen]
        self.assertEqual(dones, sorted(dones), "已处理字节数必须单调不减")
        self.assertEqual(seen[-1][0], 1024 * 1024)
        self.assertTrue(all(s[1] == 1024 * 1024 for s in seen))
        self.assertEqual(seen[-1][2], seen[-1][3])

    def test_cancel_midway_cleans_up_partial_files(self):
        """取消后不应留下本次生成的半成品切片。"""
        src = _make_file(self.work / "c.bin", 1024 * 1024)
        out_dir = self.work / "out"
        cancel = threading.Event()

        def _progress(done, total, index, count):
            cancel.set()  # 收到第一次进度就要求取消

        with self.assertRaises(mod.CancelledError):
            mod.split_file(src, chunk_size="128K", out_dir=out_dir,
                           progress=_progress, cancel=cancel)
        leftovers = [p.name for p in out_dir.iterdir() if p.name.endswith(tuple(
            ".part%04d" % i for i in range(1, 20)))] if out_dir.exists() else []
        self.assertEqual(leftovers, [])
        self.assertFalse((out_dir / "manifest.json").exists())

    def test_cancel_before_start(self):
        src = _make_file(self.work / "c2.bin", 100)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(mod.CancelledError):
            mod.split_file(src, chunk_size=50, cancel=cancel)

    def test_split_uses_streaming_not_whole_file(self):
        """缓冲区必须小于文件大小，证明是流式读写。"""
        self.assertLess(mod.BUF_SIZE, 16 * 1024 * 1024)
        src = _make_file(self.work / "s.bin", 16 * 1024 * 1024)
        result = mod.split_file(src, chunk_size="8M", buf_size=64 * 1024)
        self.assertEqual(result["part_count"], 2)


# --------------------------------------------------------------------------- #
# 清单读取与校验
# --------------------------------------------------------------------------- #

class TestManifest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="sf_manifest_"))
        self.src = _make_file(self.work / "m.bin", 5000, seed=b"m")
        self.out_dir = self.work / "m.bin.parts"
        mod.split_file(self.src, chunk_size=2000, out_dir=self.out_dir)

    def tearDown(self):
        shutil.rmtree(str(self.work), ignore_errors=True)

    def _manifest_path(self):
        return self.out_dir / "manifest.json"

    def _rewrite(self, **changes):
        data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        data.update(changes)
        self._manifest_path().write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_load_manifest_ok(self):
        manifest = mod.load_manifest(self.out_dir)
        self.assertEqual(manifest["original_name"], "m.bin")
        self.assertEqual(manifest["part_count"], 3)
        self.assertEqual([p["index"] for p in manifest["parts"]], [1, 2, 3])

    def test_missing_manifest_raises(self):
        os.remove(str(self._manifest_path()))
        with self.assertRaises(mod.SplitError) as ctx:
            mod.load_manifest(self.out_dir)
        self.assertIn("manifest.json", str(ctx.exception))

    def test_broken_json_raises(self):
        self._manifest_path().write_text("{ not json", encoding="utf-8")
        with self.assertRaises(mod.SplitError):
            mod.load_manifest(self.out_dir)

    def test_missing_field_raises(self):
        for field in ("original_name", "original_size", "chunk_size",
                      "part_count", "parts"):
            data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
            data.pop(field)
            self._manifest_path().write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(mod.SplitError, msg="字段 %s 缺失未被发现" % field):
                mod.load_manifest(self.out_dir)

    def test_part_count_mismatch_raises(self):
        self._rewrite(part_count=99)
        with self.assertRaises(mod.SplitError) as ctx:
            mod.load_manifest(self.out_dir)
        self.assertIn("不一致", str(ctx.exception))

    def test_non_integer_size_raises(self):
        self._rewrite(original_size="abc")
        with self.assertRaises(mod.SplitError):
            mod.load_manifest(self.out_dir)

    def test_zero_chunk_size_raises(self):
        self._rewrite(chunk_size=0)
        with self.assertRaises(mod.SplitError):
            mod.load_manifest(self.out_dir)

    def test_bad_sha256_value_raises(self):
        data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        data["parts"][0]["sha256"] = "not-a-hash"
        self._manifest_path().write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(mod.SplitError) as ctx:
            mod.load_manifest(self.out_dir)
        self.assertIn("SHA-256", str(ctx.exception))

    def test_part_entry_missing_field_raises(self):
        data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        data["parts"][1].pop("size")
        self._manifest_path().write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(mod.SplitError):
            mod.load_manifest(self.out_dir)

    def test_path_traversal_in_original_name_is_rejected(self):
        """清单里若写了 ../evil.bin，必须拒绝，不能写到上级目录。"""
        self._rewrite(original_name="../evil.bin")
        with self.assertRaises(mod.SplitError):
            mod.load_manifest(self.out_dir)

    def test_path_traversal_in_part_name_is_rejected(self):
        data = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        data["parts"][0]["name"] = "../../evil.part0001"
        self._manifest_path().write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(mod.SplitError):
            mod.load_manifest(self.out_dir)

    def test_output_path_uses_manifest_name_in_parent_dir(self):
        manifest = mod.load_manifest(self.out_dir)
        self.assertEqual(mod.output_path_for(self.out_dir, manifest), self.work / "m.bin")

    def test_verify_manifest_passes_on_intact_parts(self):
        manifest = mod.load_manifest(self.out_dir)
        self.assertEqual(mod.verify_manifest(manifest, self.out_dir), [])

    def test_verify_reports_missing_part_by_index(self):
        os.remove(str(self.out_dir / "m.bin.part0002"))
        manifest = mod.load_manifest(self.out_dir)
        problems = mod.verify_manifest(manifest, self.out_dir)
        self.assertTrue(problems)
        self.assertIn("第 2/3 片", problems[0])
        self.assertIn("缺失", problems[0])
        self.assertIn("m.bin.part0002", problems[0])

    def test_verify_reports_corrupted_part_by_index(self):
        target = self.out_dir / "m.bin.part0001"
        with open(target, "r+b") as fp:
            fp.write(b"CORRUPTED")
        manifest = mod.load_manifest(self.out_dir)
        problems = mod.verify_manifest(manifest, self.out_dir)
        self.assertTrue(problems)
        self.assertIn("第 1/3 片", problems[0])
        self.assertIn("m.bin.part0001", problems[0])
        self.assertTrue("校验失败" in problems[0] or "大小不符" in problems[0])

    def test_verify_reports_all_problems_not_just_first(self):
        os.remove(str(self.out_dir / "m.bin.part0001"))
        target = self.out_dir / "m.bin.part0002"
        with open(target, "r+b") as fp:
            fp.write(b"BROKEN")
        manifest = mod.load_manifest(self.out_dir)
        problems = mod.verify_manifest(manifest, self.out_dir)
        joined = "\n".join(problems)
        self.assertIn("第 1/3 片", joined)
        self.assertIn("第 2/3 片", joined)

    def test_verify_warns_about_renamed_extra_part(self):
        os.rename(str(self.out_dir / "m.bin.part0003"),
                  str(self.out_dir / "renamed.part0003"))
        manifest = mod.load_manifest(self.out_dir)
        logs = []
        problems = mod.verify_manifest(manifest, self.out_dir, log=logs.append)
        self.assertTrue(problems)
        self.assertTrue(any("未登记" in line for line in logs),
                        "应提示发现被改名的切片文件")

    def test_verify_respects_cancel(self):
        manifest = mod.load_manifest(self.out_dir)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(mod.CancelledError):
            mod.verify_manifest(manifest, self.out_dir, cancel=cancel)


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #

class TestMerge(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="sf_merge_"))
        self.size = 7 * 1024 * 1024 + 123
        # 源文件放在 send 目录，切片与还原放在 recv 目录，
        # 模拟「发送方切割 → 接收方在另一台机器/目录合并」的真实场景，
        # 这样合并的默认输出路径（切片目录的上级）不会和源文件重名。
        self.send = self.work / "send"
        self.recv = self.work / "recv"
        self.send.mkdir()
        self.recv.mkdir()
        self.src = _make_file(self.send / "orig.bin", self.size, seed=b"o")
        self.out_dir = self.recv / "orig.bin.parts"
        mod.split_file(self.src, chunk_size="2M", out_dir=self.out_dir)
        # 合并默认还原到切片目录的上一级，即 recv/orig.bin
        self.restored = self.recv / "orig.bin"

    def tearDown(self):
        shutil.rmtree(str(self.work), ignore_errors=True)

    def test_round_trip_restores_identical_file(self):
        result = mod.merge_parts(self.out_dir)
        out_path = Path(result["output"])
        self.assertEqual(out_path, self.restored)
        self.assertTrue(result["size_match"])
        self.assertEqual(result["actual_size"], self.size)
        self.assertEqual(_sha256(str(out_path)), _sha256(str(self.src)))
        self.assertFalse((self.recv / "orig.bin.merging").exists(),
                         "临时文件必须被替换掉")

    def test_merge_into_custom_output_path(self):
        target = self.work / "restored" / "copy.bin"
        result = mod.merge_parts(self.out_dir, out_path=target)
        self.assertTrue(target.is_file())
        self.assertEqual(result["actual_size"], self.size)
        self.assertEqual(_sha256(str(target)), _sha256(str(self.src)))

    def test_existing_output_requires_overwrite(self):
        self.restored.write_bytes(b"old content")
        with self.assertRaises(mod.SplitError) as ctx:
            mod.merge_parts(self.out_dir)
        self.assertIn("已存在", str(ctx.exception))
        self.assertEqual(self.restored.read_bytes(), b"old content",
                         "未确认覆盖前不得改动原文件")
        result = mod.merge_parts(self.out_dir, overwrite=True)
        self.assertTrue(result["size_match"])
        self.assertEqual(_sha256(str(self.restored)), _sha256(str(self.src)))

    def test_merge_aborts_on_corrupted_part_and_writes_nothing(self):
        target = self.out_dir / "orig.bin.part0002"
        with open(target, "r+b") as fp:
            fp.write(b"DAMAGED")
        with self.assertRaises(mod.SplitError) as ctx:
            mod.merge_parts(self.out_dir)
        message = str(ctx.exception)
        self.assertIn("第 2/", message)
        self.assertIn("中止", message)
        self.assertFalse(self.restored.exists(), "校验失败时不得产出错误文件")
        self.assertFalse((self.recv / "orig.bin.merging").exists())

    def test_merge_aborts_on_missing_part(self):
        os.remove(str(self.out_dir / "orig.bin.part0003"))
        with self.assertRaises(mod.SplitError) as ctx:
            mod.merge_parts(self.out_dir)
        self.assertIn("缺失", str(ctx.exception))
        self.assertFalse(self.restored.exists())

    def test_merge_without_manifest_raises(self):
        os.remove(str(self.out_dir / "manifest.json"))
        with self.assertRaises(mod.SplitError):
            mod.merge_parts(self.out_dir)

    def test_merge_nonexistent_dir_raises(self):
        with self.assertRaises(mod.SplitError):
            mod.merge_parts(self.work / "nothing_here")

    def test_merge_file_instead_of_dir_raises(self):
        with self.assertRaises(mod.SplitError):
            mod.merge_parts(self.src)

    def test_merge_progress_and_stage_callbacks(self):
        """合并分「校验」「合并」两个阶段，进度各自从 0 计到满，需分阶段验证。"""
        stages = []
        seen = []
        bounds = []  # 每个阶段在 seen 中的起始下标

        def on_stage(name):
            stages.append(name)
            bounds.append(len(seen))

        mod.merge_parts(self.out_dir, stage=on_stage,
                        progress=lambda done, total, i, n: seen.append((done, total, i, n)))
        self.assertEqual(stages, ["verify", "merge"])
        bounds.append(len(seen))
        # 每个阶段内部的已处理字节数必须单调不减
        for k in range(len(bounds) - 1):
            segment = [s[0] for s in seen[bounds[k]:bounds[k + 1]]]
            self.assertEqual(segment, sorted(segment),
                             "阶段 %s 的进度必须单调不减" % stages[k])
        # 合并阶段最终应到达完整大小
        self.assertEqual(seen[-1][0], self.size)

    def test_merge_cancel_leaves_no_output(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(mod.CancelledError):
            mod.merge_parts(self.out_dir, cancel=cancel)
        self.assertFalse(self.restored.exists())

    def test_merge_cancel_during_write_cleans_temp_file(self):
        cancel = threading.Event()

        def _progress(done, total, index, count):
            if done > 0:
                cancel.set()

        with self.assertRaises(mod.CancelledError):
            mod.merge_parts(self.out_dir, progress=_progress, cancel=cancel)
        self.assertFalse(self.restored.exists())
        self.assertFalse((self.recv / "orig.bin.merging").exists(),
                         "取消后必须清掉临时文件")

    def test_size_mismatch_is_flagged(self):
        """清单原始大小被改小时，合并结果应报告大小不一致。"""
        path = self.out_dir / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["original_size"] = data["original_size"] - 1
        path.write_text(json.dumps(data), encoding="utf-8")
        result = mod.merge_parts(self.out_dir)
        self.assertFalse(result["size_match"])
        self.assertEqual(result["actual_size"], self.size)

    def test_merge_small_single_part_file(self):
        src = _make_file(self.send / "tiny.bin", 37)
        out_dir = self.recv / "tiny.bin.parts"
        mod.split_file(src, chunk_size="10M", out_dir=out_dir)
        result = mod.merge_parts(out_dir)
        self.assertTrue(result["size_match"])
        self.assertEqual(result["part_count"], 1)
        self.assertEqual(Path(result["output"]).read_bytes(), src.read_bytes())


# --------------------------------------------------------------------------- #
# 线程调度
# --------------------------------------------------------------------------- #

class TestTaskRunner(unittest.TestCase):
    def test_success_event(self):
        runner = mod.TaskRunner()
        self.assertTrue(runner.start("split", lambda: {"ok": 1}))
        runner.thread.join(timeout=5)
        events = runner.drain()
        self.assertIn(("split", "done", {"ok": 1}), events)

    def test_split_error_event(self):
        runner = mod.TaskRunner()

        def boom():
            raise mod.SplitError("坏了")

        runner.start("merge", boom)
        runner.thread.join(timeout=5)
        self.assertIn(("merge", "error", "坏了"), runner.drain())

    def test_cancelled_event(self):
        runner = mod.TaskRunner()

        def work():
            raise mod.CancelledError("取消")

        runner.start("split", work)
        runner.thread.join(timeout=5)
        self.assertIn(("split", "cancelled", None), runner.drain())

    def test_unexpected_exception_is_reported_not_raised(self):
        runner = mod.TaskRunner()

        def boom():
            raise RuntimeError("意外")

        runner.start("merge", boom)
        runner.thread.join(timeout=5)
        events = runner.drain()
        self.assertEqual(len(events), 1)
        mode, kind, payload = events[0]
        self.assertEqual((mode, kind), ("merge", "error"))
        self.assertIn("意外", payload)

    def test_busy_blocks_second_start(self):
        runner = mod.TaskRunner()
        gate = threading.Event()
        runner.start("split", gate.wait)
        self.assertTrue(runner.busy)
        self.assertFalse(runner.start("merge", lambda: None))
        gate.set()
        runner.thread.join(timeout=5)

    def test_drain_respects_limit(self):
        runner = mod.TaskRunner()
        for i in range(10):
            runner.events.put(("split", "log", str(i)))
        self.assertEqual(len(runner.drain(limit=4)), 4)
        self.assertEqual(len(runner.drain()), 6)


# --------------------------------------------------------------------------- #
# 界面辅助函数（不需要真实窗口）
# --------------------------------------------------------------------------- #

class TestUiHelpers(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(mod._format_duration(30), "30 秒")
        self.assertEqual(mod._format_duration(125), "2 分 5 秒")
        self.assertEqual(mod._format_duration(3725), "1 小时 2 分")

    def test_safe_basename_rejects_traversal(self):
        self.assertEqual(mod._safe_basename("a.bin"), "a.bin")
        for bad in ("../a.bin", "C:/x/a.bin", "/etc/passwd", "", "..", "  "):
            with self.assertRaises(mod.SplitError, msg="未拒绝 %r" % bad):
                mod._safe_basename(bad)

    def test_core_has_no_gui_dependency(self):
        """core.py 不得 import 任何 GUI 库，否则脱离界面就无法测试。"""
        source = SCRIPT.read_text(encoding="utf-8")
        for bad in ("import ttkbootstrap", "import flet", "import tkinter"):
            self.assertNotIn(bad, source, "core.py 不应依赖 %s" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
