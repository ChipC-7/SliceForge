/**
 * 任务状态中心
 * ============
 * 同一时间只有一个任务（Rust 端强制），但切割 / 合并各自保留独立的
 * 进度与日志视图。backend.py 的每条 NDJSON 事件都带 task 字段，
 * 这里按 task 分发到对应面板，并计算速率 / 剩余时间。
 */

import { reactive, ref } from "vue";
import {
  cancelTask as invokeCancel,
  onBackendEvent,
  startTask as invokeStart,
  type LogLevel,
  type TaskKind,
} from "./backend";
import { formatDuration, formatSize } from "./format";
import { alert } from "./dialog";

export interface LogEntry {
  id: number;
  time: string;
  text: string;
  level: LogLevel;
}

export interface TaskView {
  busy: boolean;
  cancelling: boolean;
  progress: number;
  pct: string;
  rate: string;
  stage: string;
  startedAt: number;
  baseDone: number;
  logs: LogEntry[];
}

interface SplitResult {
  original_name: string;
  original_size: number;
  part_count: number;
  out_dir: string;
  elapsed: number;
}

interface MergeResult {
  output: string;
  part_count: number;
  expected_size: number;
  actual_size: number;
  size_match: boolean;
  elapsed: number;
}

function blankTask(): TaskView {
  return {
    busy: false,
    cancelling: false,
    progress: 0,
    pct: "0.0%",
    rate: "",
    stage: "",
    startedAt: 0,
    baseDone: 0,
    logs: [],
  };
}

export const tasks = reactive<Record<TaskKind, TaskView>>({
  split: blankTask(),
  merge: blankTask(),
});

export const statusText = ref("就绪 · 切割与合并均在后台执行，界面不会卡死");
/** 任务结束后自增一次，面板借此刷新 plan / inspect 预读信息。 */
export const refreshTick = ref(0);

let uid = 0;

export function pushLog(mode: TaskKind, text: string, level: LogLevel = "info"): void {
  const view = tasks[mode];
  view.logs.push({ id: ++uid, time: new Date().toLocaleTimeString("zh-CN", { hour12: false }), text, level });
  if (view.logs.length > 2000) view.logs.splice(0, 500);
}

function stageText(stage: string): string {
  return stage === "verify" ? "校验" : stage === "merge" ? "合并" : "处理";
}

function handleProgress(mode: TaskKind, event: Extract<import("./backend").BackendEvent, { type: "progress" }>): void {
  const view = tasks[mode];
  const total = Number(event.total) || 0;
  if (total <= 0) return;
  const ratio = Math.min(1, Math.max(0, Number(event.done) / total));
  view.progress = ratio;
  view.pct = `${(ratio * 100).toFixed(1)}%`;

  const elapsed = Math.max((Date.now() - view.startedAt) / 1000, 1e-6);
  const delta = Math.max(Number(event.done) - view.baseDone, 0);
  const speed = delta > 0 ? delta / elapsed : 0;
  const label = stageText(view.stage);
  if (speed > 0) {
    const eta = (total - Number(event.done)) / speed;
    view.rate = `${label} ${formatSize(event.done)} / ${formatSize(total)} · ${(speed / 1048576).toFixed(2)} MB/s · 剩余约 ${formatDuration(eta)}`;
  } else {
    view.rate = `${label} ${formatSize(event.done)} / ${formatSize(total)}`;
  }
  if (event.index && event.count) {
    statusText.value = `${label}中 · 第 ${event.index}/${event.count} 片 · ${(ratio * 100).toFixed(1)}%`;
  }
}

function handleDone(mode: TaskKind, raw: Record<string, unknown>): void {
  const view = tasks[mode];
  view.progress = 1;
  view.pct = "100.0%";
  if (mode === "split") {
    const result = raw as unknown as SplitResult;
    view.rate = `完成 · ${result.part_count} 片 · ${formatSize(result.original_size)} · 耗时 ${Number(result.elapsed).toFixed(1)} 秒`;
    statusText.value = `切割完成：${result.out_dir}`;
    void alert(
      "切割完成",
      `已把 ${result.original_name} 切成 ${result.part_count} 片。\n\n` +
        `输出目录：\n${result.out_dir}\n\n清单：manifest.json\n请把整个文件夹（含 manifest.json）一起发送给对方。`,
    );
  } else {
    const result = raw as unknown as MergeResult;
    view.rate = `完成 · ${formatSize(Math.max(result.actual_size, 0))} · 耗时 ${Number(result.elapsed).toFixed(1)} 秒`;
    if (result.size_match) {
      statusText.value = `合并完成：${result.output}`;
      void alert(
        "合并完成",
        `已还原文件：${result.output}\n\n大小：${formatSize(result.actual_size)}（与清单一致 ✓）\n` +
          `切片数：${result.part_count}\nSHA-256 校验：全部通过`,
      );
    } else {
      statusText.value = "合并完成，但大小不一致";
      void alert(
        "大小不一致",
        `文件已合并输出：\n${result.output}\n\n清单记录的原始大小：${formatSize(result.expected_size)}\n` +
          `实际输出大小：${formatSize(Math.max(result.actual_size, 0))}\n\n两者不一致，输出文件很可能不完整，请勿直接使用。`,
        "warning",
      );
    }
  }
}

/** 是否所有面板都空闲。 */
export function anyBusy(): boolean {
  return tasks.split.busy || tasks.merge.busy;
}

/** 启动任务；返回 false 表示没启动（被校验或忙碌拦截）。 */
export async function runTask(
  mode: TaskKind,
  path: string,
  options: { chunk?: string; overwrite?: boolean } = {},
): Promise<boolean> {
  if (anyBusy()) {
    await alert("请稍候", "已有任务正在执行，请等待完成或先取消。");
    return false;
  }
  const view = tasks[mode];
  view.busy = true;
  view.cancelling = false;
  view.progress = 0;
  view.pct = "0.0%";
  view.rate = "";
  view.stage = "";
  view.startedAt = Date.now();
  view.baseDone = 0;
  statusText.value = `正在执行 ${mode === "split" ? "切割" : "合并"} 任务…`;
  try {
    await invokeStart(mode, path, options.chunk, options.overwrite);
    return true;
  } catch (error) {
    view.busy = false;
    statusText.value = "任务启动失败";
    await alert("无法开始", String(error), "danger");
    return false;
  }
}

export async function requestCancel(mode: TaskKind): Promise<void> {
  if (!tasks[mode].busy) return;
  tasks[mode].cancelling = true;
  statusText.value = "正在取消，请稍候…";
  pushLog(mode, "⚠ 已请求取消，正在中断…", "warn");
  try {
    await invokeCancel();
  } catch (error) {
    statusText.value = `取消失败：${String(error)}`;
  }
}

/** 订阅 backend.py 的事件流；在 App.vue 挂载时调用一次。 */
export async function initBackendEvents(): Promise<void> {
  await onBackendEvent((event) => {
    const view = tasks[event.task];
    if (!view) return;
    switch (event.type) {
      case "log":
        pushLog(event.task, event.text, event.level);
        break;
      case "stage":
        view.stage = event.stage;
        view.startedAt = Date.now();
        view.baseDone = 0;
        view.progress = 0;
        view.pct = "0.0%";
        statusText.value =
          event.stage === "verify" ? "校验切片完整性…" : "正在合并写入…";
        break;
      case "progress":
        handleProgress(event.task, event);
        break;
      case "cancelled":
        view.busy = false;
        view.cancelling = false;
        view.rate = "已取消";
        pushLog(event.task, "⚠ 任务已取消，未产出可用结果。", "warn");
        statusText.value = "已取消";
        refreshTick.value += 1;
        break;
      case "error":
        view.busy = false;
        view.cancelling = false;
        pushLog(event.task, `✗ ${event.message}`, "error");
        statusText.value = "任务失败";
        void alert("任务失败", event.message, "danger");
        refreshTick.value += 1;
        break;
      case "done":
        view.busy = false;
        view.cancelling = false;
        handleDone(event.task, event.result);
        refreshTick.value += 1;
        break;
    }
  });
}
