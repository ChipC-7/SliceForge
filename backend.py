#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SliceForge · 文件切片工坊 —— 命令行桥接层（供 Tauri / Vue 前端调用）
====================================================================
把 core.py 的切割 / 合并以「子进程 + NDJSON」的方式暴露给桌面前端，
所有切片、校验、合并、清单逻辑仍只此一份（core.py），界面端零重复。

用法
----
    python3 backend.py plan    --src <待切割文件>          # 切割页预读
    python3 backend.py inspect --dir <切片文件夹>          # 合并页清单预读
    python3 backend.py split   --src <文件> --chunk 10M [--overwrite]
    python3 backend.py merge   --dir <文件夹> [--overwrite]

流式协议（stdout，每行一个 JSON 对象，UTF-8）
---------------------------------------------
    {"type": "log",      "level": "info|ok|warn|error|dim", "text": "..."}
    {"type": "progress", "done": N, "total": N, "index": N, "count": N}
    {"type": "stage",    "stage": "verify" | "merge"}
    {"type": "done",     "result": {...}}                   # 终结事件
    {"type": "error",    "message": "..."}                  # 终结事件
    {"type": "cancelled"}                                   # 终结事件

取消：向 stdin 写一行 ``cancel``。backend 会在下一个检查点优雅中断
（清理未完成切片，与图形界面行为一致），而不是被强杀。

plan / inspect 为一次性命令：输出一行 JSON 后退出，退出码 0 表示命令本身成功
（inspect 里清单缺失等属于业务结果，放在 ``ok`` 字段里而不是退出码）。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path

from core import (
    MANIFEST_NAME,
    SplitError,
    TaskRunner,
    load_manifest,
    merge_parts,
    output_path_for,
    parse_chunk_size,
    parts_dir_for,
    pick_log_tag,
    split_file,
)


def _emit(payload: dict) -> None:
    """向 stdout 写一行 NDJSON 并立即刷新（前端逐行读取、逐行渲染）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# 一次性命令：预读信息
# --------------------------------------------------------------------------- #

def cmd_plan(args) -> int:
    """切割页预读：源文件信息 + 输出目录 + 是否已有旧清单。"""
    src = Path(args.src)
    info = {"src": str(src), "exists": False, "size": None,
            "parts_dir": None, "manifest_exists": False}
    try:
        info["exists"] = src.is_file()
        if info["exists"]:
            info["size"] = src.stat().st_size
        out_dir = parts_dir_for(src)
        info["parts_dir"] = str(out_dir)
        info["manifest_exists"] = (out_dir / MANIFEST_NAME).exists()
    except OSError as exc:
        info["error"] = "读取文件信息失败：%s" % exc
    except SplitError as exc:
        info["error"] = str(exc)
    _emit(info)
    return 0


def cmd_inspect(args) -> int:
    """合并页预读：清单摘要 + 缺片情况 + 将还原到的路径。"""
    folder = Path(args.dir)
    if not folder.is_dir():
        _emit({"ok": False, "error": "该路径不是文件夹：%s" % folder})
        return 0
    try:
        manifest = load_manifest(folder)
    except SplitError as exc:
        _emit({"ok": False, "error": str(exc).splitlines()[0]})
        return 0
    missing = [p["name"] for p in manifest["parts"] if not (folder / p["name"]).is_file()]
    try:
        out_path = output_path_for(folder, manifest)
        out_exists = out_path.exists()
        out_text = str(out_path)
    except (SplitError, OSError) as exc:
        out_text, out_exists = "", False
        missing = missing or []
        _emit({"ok": True, "original_name": manifest["original_name"],
               "original_size": manifest["original_size"],
               "chunk_size": manifest["chunk_size"],
               "part_count": manifest["part_count"],
               "missing": missing, "error": str(exc)})
        return 0
    _emit({
        "ok": True,
        "original_name": manifest["original_name"],
        "original_size": manifest["original_size"],
        "chunk_size": manifest["chunk_size"],
        "part_count": manifest["part_count"],
        "missing": missing,
        "out_path": out_text,
        "out_exists": out_exists,
    })
    return 0


# --------------------------------------------------------------------------- #
# 流式命令：split / merge
# --------------------------------------------------------------------------- #

def _forward(runner: TaskRunner, finished: threading.Event) -> None:
    """把 TaskRunner 队列里的事件转成 NDJSON 写到 stdout，直到终结事件。

    每条事件都带 ``task`` 字段（split / merge），前端据此分发到对应面板。
    """
    while True:
        try:
            mode, kind, payload = runner.events.get(timeout=0.1)
        except queue.Empty:
            continue
        head = {"task": mode}
        if kind == "log":
            _emit({**head, "type": "log",
                   "level": pick_log_tag(str(payload)), "text": str(payload)})
        elif kind == "progress":
            done, total, index, count = payload
            _emit({**head, "type": "progress", "done": done, "total": total,
                   "index": index, "count": count})
        elif kind == "stage":
            _emit({**head, "type": "stage", "stage": payload})
        elif kind == "cancelled":
            _emit({**head, "type": "cancelled"})
            break
        elif kind == "error":
            _emit({**head, "type": "error", "message": str(payload)})
            break
        elif kind == "done":
            _emit({**head, "type": "done", "result": payload})
            break
    finished.set()


def _watch_stdin(runner: TaskRunner, started: threading.Event) -> None:
    """监听 stdin：收到 ``cancel`` 就请求优雅取消（保留切片清理逻辑）。

    必须等 ``started`` 置位后再设取消标志：TaskRunner.start() 会先 clear()
    取消事件，若取消请求在任务启动前到达，直接置位会被吞掉。
    """
    try:
        for line in sys.stdin:
            if line.strip().lower() == "cancel":
                started.wait()
                runner.request_cancel()
                break
    except Exception:
        pass  # stdin 不可读时忽略：前端仍可强杀进程


def cmd_run(kind: str, args) -> int:
    """执行 split / merge：起任务线程 + 事件转发线程，直到任务结束。"""
    src = Path(args.src) if kind == "split" else None
    folder = Path(args.dir) if kind == "merge" else None

    runner = TaskRunner()
    try:
        chunk = parse_chunk_size(args.chunk) if kind == "split" else None
    except SplitError as exc:
        _emit({"type": "error", "message": str(exc)})
        return 1

    def progress_cb(done, total, index, count):
        runner.events.put((kind, "progress", (done, total, index, count)))

    def log_cb(text):
        runner.events.put((kind, "log", text))

    def stage_cb(name):
        runner.events.put((kind, "stage", name))

    def task():
        if kind == "split":
            return split_file(
                src, chunk_size=chunk, out_dir=parts_dir_for(src),
                progress=progress_cb, log=log_cb,
                cancel=runner.cancel_event, overwrite=args.overwrite,
            )
        return merge_parts(
            folder, out_path=None,
            progress=progress_cb, log=log_cb, stage=stage_cb,
            cancel=runner.cancel_event, overwrite=args.overwrite,
        )

    finished = threading.Event()
    started = threading.Event()
    threading.Thread(target=_watch_stdin, args=(runner, started), daemon=True).start()
    threading.Thread(target=_forward, args=(runner, finished), daemon=True).start()

    if not runner.start(kind, task):
        started.set()  # 放开 stdin 监听线程，让它随进程退出
        _emit({"type": "error", "message": "任务未能启动。"})
        return 1
    started.set()

    finished.wait()
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend", description="SliceForge 桥接层")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="切割页预读")
    p.add_argument("--src", required=True)
    p.set_defaults(func=lambda a: cmd_plan(a))

    p = sub.add_parser("inspect", help="合并页清单预读")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=lambda a: cmd_inspect(a))

    p = sub.add_parser("split", help="切割文件")
    p.add_argument("--src", required=True)
    p.add_argument("--chunk", default="10M")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=lambda a: cmd_run("split", a))

    p = sub.add_parser("merge", help="校验并合并切片")
    p.add_argument("--dir", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=lambda a: cmd_run("merge", a))

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
