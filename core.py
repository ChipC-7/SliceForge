#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SliceForge · 文件切片工坊 —— 核心逻辑
=====================================
把大文件切成若干小切片（便于通过限制单文件大小的平台逐个发送），
接收方拿到全部切片后可校验 SHA-256 完整性并流式合并还原原文件。

本模块只包含与界面无关的纯逻辑（解析 / 切割 / 清单 / 校验 / 合并）以及
把耗时任务丢进子线程的 TaskRunner，**不依赖任何 GUI 库**，
因此可以脱离界面直接 import 并做单元测试。

界面：Tauri 2 + Vue 3（见 sliceforge-tauri/），通过 backend.py 的
NDJSON 子进程协议调用本模块；逻辑行为由 test_sliceforge.py 与
test_backend.py 锁定。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "APP_VERSION",
    "BUF_SIZE",
    "DEFAULT_CHUNK_TEXT",
    "MANIFEST_NAME",
    "CancelledError",
    "SplitError",
    "TaskRunner",
    "format_size",
    "load_manifest",
    "merge_parts",
    "output_path_for",
    "part_name",
    "parts_dir_for",
    "parse_chunk_size",
    "pick_log_tag",
    "sha256_of_file",
    "split_file",
    "verify_manifest",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

APP_NAME = "SliceForge"
APP_TITLE = "SliceForge · 文件切片工坊"
APP_VERSION = "1.0.0"

#: 流式读写缓冲区：4MB，保证大文件不会一次性读入内存
BUF_SIZE = 4 * 1024 * 1024

#: 每片大小输入框默认值
DEFAULT_CHUNK_TEXT = "10M"

#: 清单文件名
MANIFEST_NAME = "manifest.json"

#: 清单格式版本号，便于将来升级兼容
MANIFEST_VERSION = 1

#: 单位表：1024 进制（K/M/G/T/P/E），空串与 B 表示字节
_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024 ** 2,
    "MB": 1024 ** 2,
    "G": 1024 ** 3,
    "GB": 1024 ** 3,
    "T": 1024 ** 4,
    "TB": 1024 ** 4,
    "P": 1024 ** 5,
    "PB": 1024 ** 5,
    "E": 1024 ** 6,
    "EB": 1024 ** 6,
}

#: 每片大小输入格式：数字 + 可选空格 + 可选单位 + 可选 B（忽略大小写）
#: 只允许数字与单位之间出现空格，"5 5M" 这类夹带字符会被判为非法
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGTPE]?)\s*(B?)$")

#: SHA-256 十六进制串
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")



# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #

class SplitError(Exception):
    """面向用户的业务错误（输入非法、文件缺失、校验失败等），消息可直接展示。"""


class CancelledError(Exception):
    """用户主动取消，属于正常中断，不当作错误处理。"""


# --------------------------------------------------------------------------- #
# 纯逻辑：解析与命名
# --------------------------------------------------------------------------- #

def parse_chunk_size(value) -> int:
    """把用户输入的每片大小解析为字节数。

    支持 ``"5M"`` / ``"512K"`` / ``"2G"`` / ``"10MB"`` / ``"1.5M"`` 以及纯字节数
    ``"2048"``、整数 ``2048``；忽略大小写与空格。

    :raises SplitError: 输入为空、格式非法、不是整数字节或 <= 0
    """
    if value is None:
        raise SplitError("每片大小不能为空，请输入如 10M、512K、2G 或纯字节数。")
    if isinstance(value, bool):
        raise SplitError("每片大小格式不正确，请输入如 10M、512K、2G 或纯字节数。")
    if isinstance(value, int):
        number, mult = float(value), 1
    else:
        text = str(value).strip()  # 只去掉首尾空白，内部夹带字符应判为非法
        if not text:
            raise SplitError("每片大小不能为空，请输入如 10M、512K、2G 或纯字节数。")
        m = _SIZE_RE.match(text.upper())
        if not m:
            raise SplitError(
                "每片大小格式不正确：%r\n"
                "正确写法示例：5M、512K、2G、100MB、1.5M，或直接填字节数 10485760。" % (value,)
            )
        number = float(m.group(1))
        mult = _UNITS[m.group(2) + m.group(3)]

    exact = number * mult
    if exact <= 0:
        raise SplitError("每片大小必须大于 0，请输入如 10M、512K、2G 或纯字节数。")
    result = int(exact)
    if result <= 0 or abs(exact - result) > 1e-6:
        raise SplitError("每片大小必须能换算成整数个字节（不支持 0.5 字节这类取值）。")
    return result


def parts_dir_for(src) -> Path:
    """切片输出目录：``原文件路径 + .parts``。"""
    src = Path(src)
    return src.parent / (src.name + ".parts")


def part_name(original_name: str, index: int, total: int = 0) -> str:
    """生成切片文件名：``原文件名.partNNNN``。

    序号补零至少 4 位；当总片数超过 4 位时自动加宽，
    从而保证「按文件名排序」恰好等于「正确的合并顺序」。
    """
    if index < 1:
        raise ValueError("切片序号必须从 1 开始")
    width = max(4, len(str(max(int(total or 0), index))))
    return "%s.part%0*d" % (original_name, width, index)


def format_size(num) -> str:
    """把字节数格式化成便于阅读的字符串，如 ``1.50 MB``。"""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return str(num)
    if n == 0:
        return "0 B"
    if n < 1024:
        return "%d B" % int(n) if n == int(n) else "%.2f B" % n
    units = ("KB", "MB", "GB", "TB", "PB")
    value = n
    for unit in units:
        value /= 1024.0
        if value < 1024 or unit == units[-1]:
            return "%.2f %s" % (value, unit)
    return "%.2f %s" % (value, units[-1])


def _safe_basename(name: str) -> str:
    """校验清单中的文件名：必须是纯文件名，禁止任何路径成分或目录穿越。

    同时兼容 Windows 反斜杠与 POSIX 正斜杠；含分隔符、``..``、为空或非法时抛错。
    返回规范化（去首尾空白）后的文件名。
    """
    if not isinstance(name, str):
        raise SplitError("清单中的文件名必须是字符串，当前为 %r。" % (name,))
    text = name.strip()
    if not text:
        raise SplitError("清单中的文件名不能为空。")
    flat = text.replace("\\", "/")  # 统一分隔符后再检查
    if "/" in flat or text in (".", "..") or ".." in flat:
        raise SplitError("清单中的文件名 %r 不合法（不允许包含路径或目录穿越）。" % (name,))
    return text


def _check_cancel(cancel) -> None:
    """检查取消标志；已取消则抛出 CancelledError。"""
    if cancel is not None and cancel.is_set():
        raise CancelledError("操作已被取消。")


def _is_int(value) -> bool:
    """判断 JSON 解出来的值是否为真正的整数（排除 bool）。"""
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# 纯逻辑：哈希
# --------------------------------------------------------------------------- #

def sha256_of_file(path, buf_size: int = BUF_SIZE, on_bytes=None, cancel=None) -> str:
    """流式计算文件的 SHA-256，不会把整个文件读入内存。

    :param on_bytes: 每读一块回调一次，参数为本块字节数（用于进度统计）
    :param cancel: ``threading.Event``，置位后抛出 CancelledError
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            _check_cancel(cancel)
            block = fp.read(buf_size)
            if not block:
                break
            digest.update(block)
            if on_bytes is not None:
                on_bytes(len(block))
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 纯逻辑：切割
# --------------------------------------------------------------------------- #

def split_file(src, chunk_size=DEFAULT_CHUNK_TEXT, out_dir=None,
               buf_size: int = BUF_SIZE, progress=None, log=None,
               cancel=None, overwrite: bool = False) -> dict:
    """把 ``src`` 按 ``chunk_size`` 切成多片，输出到 ``out_dir`` 并生成清单。

    :param src: 待切割文件路径
    :param chunk_size: 每片大小，整数（字节）或字符串（如 "10M"）
    :param out_dir: 输出目录，默认为 ``原文件名.parts``
    :param progress: 回调 ``progress(done_bytes, total_bytes, part_index, part_count)``
    :param log: 回调 ``log(text)``，用于输出每片完成信息
    :param cancel: ``threading.Event``，置位后中断并清理本次产生的切片
    :param overwrite: 输出目录已有清单时是否允许覆盖
    :return: 摘要字典（片数、输出目录、耗时等）
    :raises SplitError: 参数或文件状态不合法
    :raises CancelledError: 用户取消
    """
    src = Path(src)
    if not src.exists():
        raise SplitError("找不到要切割的文件：\n%s" % src)
    if src.is_dir():
        raise SplitError("所选路径是文件夹，请选择一个文件：\n%s" % src)
    if not src.is_file():
        raise SplitError("所选路径不是普通文件，无法切割：\n%s" % src)

    chunk = chunk_size if _is_int(chunk_size) else parse_chunk_size(chunk_size)

    try:
        size = src.stat().st_size
    except OSError as exc:
        raise SplitError("读取文件大小失败：%s" % exc)

    if size == 0:
        raise SplitError(
            "所选文件是空文件（0 字节），没有需要切割的内容。\n%s" % src
        )

    # 总片数可由文件大小直接算出，因此命名宽度在写入前就能确定
    total = max(1, int(math.ceil(size / float(chunk))))
    out_dir = Path(out_dir) if out_dir else parts_dir_for(src)
    manifest_path = out_dir / MANIFEST_NAME

    if manifest_path.exists() and not overwrite:
        raise SplitError(
            "输出目录中已存在 %s：\n%s\n"
            "如需覆盖原有切片，请确认后重试。" % (MANIFEST_NAME, out_dir)
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SplitError("无法创建输出目录 %s：%s" % (out_dir, exc))

    created = []          # 本次真正写入的切片路径，失败/取消时用于清理
    parts = []            # 清单中的切片记录
    done = 0
    started = time.time()

    try:
        with open(src, "rb") as fin:
            for index in range(1, total + 1):
                _check_cancel(cancel)
                name = part_name(src.name, index, total)
                part_path = out_dir / name
                digest = hashlib.sha256()
                remaining = chunk
                written = 0

                with open(part_path, "wb") as fout:
                    created.append(part_path)
                    while remaining > 0:
                        _check_cancel(cancel)
                        block = fin.read(min(buf_size, remaining))
                        if not block:
                            break
                        fout.write(block)
                        digest.update(block)
                        step = len(block)
                        written += step
                        remaining -= step
                        done += step
                        if progress is not None:
                            progress(done, size, index, total)

                if written == 0:
                    raise SplitError(
                        "写入第 %d/%d 片（%s）时没有读到任何数据，"
                        "源文件可能在切割过程中被修改或截断。" % (index, total, name)
                    )
                if written > chunk:
                    raise SplitError("内部错误：第 %d 片大小超出预设的每片大小。" % index)

                parts.append({
                    "index": index,
                    "name": name,
                    "size": written,
                    "sha256": digest.hexdigest(),
                })
                if log is not None:
                    log("[%d/%d] %s 完成 · %s" % (index, total, name, format_size(written)))

        if done != size:
            raise SplitError(
                "切割过程中源文件大小发生了变化（预期 %s，实际读取 %s），"
                "请确认文件未被其他程序占用后重试。" % (format_size(size), format_size(done))
            )

        manifest = {
            "format_version": MANIFEST_VERSION,
            "tool": APP_NAME,
            "tool_version": APP_VERSION,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "original_name": src.name,
            "original_size": size,
            "chunk_size": chunk,
            "part_count": total,
            "parts": parts,
        }
        tmp_manifest = out_dir / (MANIFEST_NAME + ".tmp")
        with open(tmp_manifest, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, ensure_ascii=False, indent=2)
        os.replace(str(tmp_manifest), str(manifest_path))

    except BaseException:
        _cleanup(created, log)
        raise

    elapsed = time.time() - started
    if log is not None:
        log("切割完成：共 %d 片，合计 %s，耗时 %.1f 秒" % (total, format_size(size), elapsed))
    return {
        "mode": "split",
        "source": str(src),
        "original_name": src.name,
        "original_size": size,
        "chunk_size": chunk,
        "part_count": total,
        "out_dir": str(out_dir),
        "manifest": str(manifest_path),
        "elapsed": elapsed,
    }


def _cleanup(paths, log=None) -> None:
    """删除本次运行中生成的切片文件（只删自己刚写出来的产物）。"""
    removed = 0
    for path in paths:
        try:
            if Path(path).exists():
                Path(path).unlink()
                removed += 1
        except OSError:
            pass
    if removed and log is not None:
        log("已清理本次生成的 %d 个未完成切片文件。" % removed)


# --------------------------------------------------------------------------- #
# 纯逻辑：清单读取与校验
# --------------------------------------------------------------------------- #

def load_manifest(parts_dir) -> dict:
    """读取并严格校验 ``manifest.json``，返回规范化后的清单字典。

    规范化内容：``original_name`` 去掉任何路径成分（防目录穿越），
    ``parts`` 补齐 ``index`` 并把 ``sha256`` 统一为小写。

    :raises SplitError: 清单缺失、格式错误、字段缺失或取值非法
    """
    parts_dir = Path(parts_dir)
    path = parts_dir / MANIFEST_NAME
    if not path.is_file():
        raise SplitError(
            "所选文件夹中没有找到清单文件 %s：\n%s\n"
            "请选择切割时生成的「.parts」文件夹。" % (MANIFEST_NAME, path)
        )
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as exc:
        raise SplitError("清单文件 %s 读取失败（可能已损坏或不是合法 JSON）：%s" % (MANIFEST_NAME, exc))

    if not isinstance(data, dict):
        raise SplitError("清单文件格式不正确：顶层应为 JSON 对象。")

    for key in ("original_name", "original_size", "chunk_size", "part_count", "parts"):
        if key not in data:
            raise SplitError("清单文件缺少必要字段：%s" % key)

    original_name = _safe_basename(data["original_name"])

    for key in ("original_size", "chunk_size", "part_count"):
        if not _is_int(data[key]):
            raise SplitError("清单字段 %s 必须是整数，当前为 %r。" % (key, data[key]))
    if data["chunk_size"] <= 0:
        raise SplitError("清单字段 chunk_size 必须大于 0，当前为 %r。" % (data["chunk_size"],))
    if data["original_size"] < 0:
        raise SplitError("清单字段 original_size 不能为负数。")
    if data["part_count"] <= 0:
        raise SplitError("清单字段 part_count 必须大于 0，当前为 %r。" % (data["part_count"],))

    raw_parts = data["parts"]
    if not isinstance(raw_parts, list):
        raise SplitError("清单字段 parts 必须是数组。")
    if len(raw_parts) != data["part_count"]:
        raise SplitError(
            "清单记录的总片数（%d）与 parts 列表长度（%d）不一致，清单可能不完整。"
            % (data["part_count"], len(raw_parts))
        )

    cleaned = []
    for i, item in enumerate(raw_parts, 1):
        if not isinstance(item, dict):
            raise SplitError("清单第 %d 片记录格式不正确（应为对象）。" % i)
        for key in ("name", "size", "sha256"):
            if key not in item:
                raise SplitError("清单第 %d 片缺少字段：%s" % (i, key))
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise SplitError("清单第 %d 片的文件名无效：%r" % (i, name))
        if _safe_basename(name) != name.strip():
            raise SplitError("清单第 %d 片的文件名不合法（不允许包含路径）：%r" % (i, name))
        if not _is_int(item["size"]) or item["size"] < 0:
            raise SplitError("清单第 %d 片（%s）的 size 必须是非负整数。" % (i, name))
        sha = item["sha256"]
        if not isinstance(sha, str) or not _SHA256_RE.match(sha.strip()):
            raise SplitError("清单第 %d 片（%s）的 SHA-256 值不合法：%r" % (i, name, sha))
        cleaned.append({
            "index": i,
            "name": name.strip(),
            "size": item["size"],
            "sha256": sha.strip().lower(),
        })

    result = dict(data)
    result["original_name"] = original_name
    result["parts"] = cleaned
    return result


def output_path_for(parts_dir, manifest) -> Path:
    """合并后的输出文件路径：切片目录的上一级 + 清单中的原始文件名。"""
    return Path(parts_dir).parent / manifest["original_name"]


def verify_manifest(manifest, parts_dir, progress=None, cancel=None,
                    log=None, buf_size: int = BUF_SIZE):
    """按清单逐一校验切片的「存在性 / 大小 / SHA-256」。

    会完整跑一遍全部切片再返回，从而一次性列出所有问题（而不是遇到第一个就停）。

    :return: 问题列表，为空表示全部通过
    :raises CancelledError: 用户取消
    """
    parts_dir = Path(parts_dir)
    parts = manifest["parts"]
    count = len(parts)
    total = manifest["original_size"] or sum(p["size"] for p in parts)
    expected = {p["name"] for p in parts}
    problems = []
    done = 0

    for i, item in enumerate(parts, 1):
        _check_cancel(cancel)
        path = parts_dir / item["name"]
        if not path.is_file():
            problems.append(
                "✗ 第 %d/%d 片缺失：%s" % (i, count, item["name"])
            )
            done += item["size"]
            if progress is not None:
                progress(min(done, total), total, i, count)
            continue

        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            problems.append("✗ 第 %d/%d 片无法读取：%s（%s）" % (i, count, item["name"], exc))
            done += item["size"]
            if progress is not None:
                progress(min(done, total), total, i, count)
            continue

        if actual_size != item["size"]:
            problems.append(
                "✗ 第 %d/%d 片大小不符：%s（清单 %s，实际 %s）"
                % (i, count, item["name"], format_size(item["size"]), format_size(actual_size))
            )

        def _bump(step, _i=i):
            """把单块字节数累加到总进度并回调界面。"""
            nonlocal done
            done += step
            if progress is not None:
                progress(min(done, total), total, _i, count)

        try:
            digest = sha256_of_file(path, buf_size=buf_size, on_bytes=_bump, cancel=cancel)
        except OSError as exc:
            problems.append("✗ 第 %d/%d 片读取失败：%s（%s）" % (i, count, item["name"], exc))
            continue

        if digest != item["sha256"]:
            problems.append(
                "✗ 第 %d/%d 片校验失败（SHA-256 不匹配，文件可能损坏或被修改）：%s"
                % (i, count, item["name"])
            )
        elif log is not None:
            log("✓ 第 %d/%d 片校验通过：%s" % (i, count, item["name"]))

    # 目录里出现「清单中没有登记」的切片文件，通常意味着切片被改过名
    try:
        for entry in sorted(parts_dir.iterdir()):
            if entry.is_file() and entry.name not in expected and ".part" in entry.name:
                if log is not None:
                    log("⚠ 发现未登记的切片文件：%s（清单中没有它，切片可能被改名）" % entry.name)
    except OSError:
        pass

    return problems


# --------------------------------------------------------------------------- #
# 纯逻辑：合并
# --------------------------------------------------------------------------- #

def merge_parts(parts_dir, out_path=None, buf_size: int = BUF_SIZE,
                progress=None, log=None, stage=None, cancel=None,
                overwrite: bool = False, verify: bool = True) -> dict:
    """校验并流式合并切片，还原原始文件。

    流程：读清单 → 逐片校验 SHA-256（有问题立刻中止，绝不产出错误文件）
    → 按清单顺序流式写入临时文件 → 原子替换为目标文件 → 核对总大小。

    :param parts_dir: 含全部切片与 manifest.json 的文件夹
    :param out_path: 输出文件路径，默认按清单还原到切片目录的上一级
    :param stage: 回调 ``stage(name)``，取值 "verify" / "merge"，用于界面区分阶段
    :param verify: 是否执行 SHA-256 校验（默认执行，强烈不建议关闭）
    :param overwrite: 目标文件已存在时是否允许覆盖
    :return: 摘要字典，含 ``size_match`` 表示总大小是否与清单一致
    """
    parts_dir = Path(parts_dir)
    if not parts_dir.exists():
        raise SplitError("找不到切片文件夹：\n%s" % parts_dir)
    if not parts_dir.is_dir():
        raise SplitError("所选路径不是文件夹：\n%s" % parts_dir)

    manifest = load_manifest(parts_dir)
    parts = manifest["parts"]
    count = len(parts)
    expected_size = manifest["original_size"]
    started = time.time()

    if verify:
        if stage is not None:
            stage("verify")
        if log is not None:
            log("开始校验 %d 个切片的 SHA-256（%s）…" % (count, manifest["original_name"]))
        problems = verify_manifest(manifest, parts_dir, progress=progress,
                                   cancel=cancel, log=log, buf_size=buf_size)
        if problems:
            raise SplitError(
                "切片校验未通过，已中止合并（没有生成任何输出文件）：\n"
                + "\n".join(problems)
            )
        if log is not None:
            log("全部 %d 个切片校验通过 ✓" % count)

    out_path = Path(out_path) if out_path else output_path_for(parts_dir, manifest)
    if out_path.exists() and not overwrite:
        raise SplitError("输出文件已存在：\n%s\n如需覆盖请先确认。" % out_path)
    if out_path.is_dir():
        raise SplitError("输出路径已存在且是文件夹：\n%s" % out_path)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SplitError("无法创建输出目录 %s：%s" % (out_path.parent, exc))

    # 先写临时文件，全部成功后再原子替换，避免留下半成品被误当成完整文件
    tmp_path = out_path.with_name(out_path.name + ".merging")
    total = expected_size or sum(p["size"] for p in parts)
    done = 0

    if stage is not None:
        stage("merge")
    if log is not None:
        log("开始合并到：%s" % out_path)

    try:
        with open(tmp_path, "wb") as fout:
            for i, item in enumerate(parts, 1):
                _check_cancel(cancel)
                path = parts_dir / item["name"]
                try:
                    with open(path, "rb") as fin:
                        while True:
                            _check_cancel(cancel)
                            block = fin.read(buf_size)
                            if not block:
                                break
                            fout.write(block)
                            done += len(block)
                            if progress is not None:
                                progress(done, total, i, count)
                except OSError as exc:
                    raise SplitError("读取第 %d/%d 片失败：%s（%s）" % (i, count, path, exc))
                if log is not None:
                    log("[%d/%d] %s 已写入 · 累计 %s" % (i, count, item["name"], format_size(done)))
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    try:
        os.replace(str(tmp_path), str(out_path))
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise SplitError("写入输出文件失败：%s（%s）" % (out_path, exc))

    try:
        actual_size = out_path.stat().st_size
    except OSError:
        actual_size = -1
    size_match = (actual_size == expected_size)
    elapsed = time.time() - started

    if not size_match and log is not None:
        log("⚠ 合并后大小与清单不一致：清单 %s，实际 %s"
            % (format_size(expected_size), format_size(max(actual_size, 0))))
    elif log is not None:
        log("合并完成：%s（%s），耗时 %.1f 秒" % (out_path.name, format_size(actual_size), elapsed))

    return {
        "mode": "merge",
        "parts_dir": str(parts_dir),
        "output": str(out_path),
        "output_name": out_path.name,
        "part_count": count,
        "expected_size": expected_size,
        "actual_size": actual_size,
        "size_match": size_match,
        "verified": bool(verify),
        "elapsed": elapsed,
    }


# --------------------------------------------------------------------------- #
# 线程调度：工作线程 + 队列（界面永不直接被子线程操作）
# --------------------------------------------------------------------------- #

class TaskRunner(object):
    """把耗时任务放进子线程执行，通过队列把事件安全地交回主线程。"""

    def __init__(self):
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, mode: str, func) -> bool:
        """启动一个后台任务；已有任务在跑时返回 False。"""
        if self.busy:
            return False
        self.cancel_event.clear()
        self.thread = threading.Thread(
            target=self._run, args=(mode, func),
            name="SliceForge-%s" % mode, daemon=True,
        )
        self.thread.start()
        return True

    def request_cancel(self) -> None:
        """请求取消当前任务（工作线程会在下一个检查点中断）。"""
        self.cancel_event.set()

    def _run(self, mode: str, func) -> None:
        """子线程入口：统一把结果 / 错误 / 取消转成队列事件。"""
        try:
            result = func()
        except CancelledError:
            self.events.put((mode, "cancelled", None))
        except SplitError as exc:
            self.events.put((mode, "error", str(exc)))
        except Exception as exc:  # 兜底：任何未预期异常都不让程序崩溃
            detail = traceback.format_exc(limit=3).strip()
            self.events.put((mode, "error", "发生未预期的错误：%s: %s\n%s"
                                            % (type(exc).__name__, exc, detail))
                            )
        else:
            self.events.put((mode, "done", result))

    def drain(self, limit: int = 200):
        """取出至多 limit 条待处理事件（由界面用 after 周期调用）。"""
        batch = []
        for _ in range(limit):
            try:
                batch.append(self.events.get_nowait())
            except queue.Empty:
                break
        return batch



def _format_duration(seconds) -> str:
    """把秒数格式化为「1 小时 2 分 3 秒」这类简短文本。"""
    try:
        s = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分 %d 秒" % (s // 60, s % 60)
    return "%d 小时 %d 分" % (s // 3600, (s % 3600) // 60)


def pick_log_tag(text: str) -> str:
    """按日志内容选颜色标签（ok / warn / error / info），供各界面共用。

    core 内部产生的日志行以 ✓ ✗ ⚠ 等符号开头，这里据此归类，
    各界面（Tk / Flet / Tauri）都用同一份规则，避免颜色语义漂移。
    """
    head = text[:1]
    if head in ("✓", "["):
        return "ok" if head == "✓" else "info"
    if head == "✗" or "失败" in text or "错误" in text:
        return "error"
    if head == "⚠" or "不一致" in text or "警告" in text:
        return "warn"
    return "info"
