/** Tauri 后端（Rust 命令 + backend.py 子进程事件）的薄封装。 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

export type TaskKind = "split" | "merge";
export type LogLevel = "info" | "ok" | "warn" | "error" | "dim";

export interface PlanInfo {
  src: string;
  exists: boolean;
  size: number | null;
  parts_dir: string | null;
  manifest_exists: boolean;
  error?: string;
}

export interface InspectInfo {
  ok: boolean;
  error?: string;
  original_name?: string;
  original_size?: number;
  chunk_size?: number;
  part_count?: number;
  missing?: string[];
  out_path?: string;
  out_exists?: boolean;
}

export type BackendEvent = {
  task: TaskKind;
} & (
  | { type: "log"; level: LogLevel; text: string }
  | { type: "progress"; done: number; total: number; index: number | null; count: number | null }
  | { type: "stage"; stage: string }
  | { type: "done"; result: Record<string, unknown> }
  | { type: "error"; message: string }
  | { type: "cancelled" }
);

/** 一次性预读（plan / inspect），同步返回单行 JSON。 */
export function query(kind: "plan" | "inspect", path: string): Promise<PlanInfo & InspectInfo> {
  return invoke("query", { kind, path });
}

export function startTask(
  kind: TaskKind,
  path: string,
  chunk?: string,
  overwrite = false,
): Promise<void> {
  return invoke("start_task", { kind, path, chunk: chunk ?? null, overwrite });
}

export function cancelTask(): Promise<void> {
  return invoke("cancel_task");
}

export async function pickFile(title: string): Promise<string | null> {
  const picked = await open({ multiple: false, directory: false, title });
  return typeof picked === "string" ? picked : null;
}

export async function pickDir(title: string): Promise<string | null> {
  const picked = await open({ multiple: false, directory: true, title });
  return typeof picked === "string" ? picked : null;
}

export function onBackendEvent(handler: (event: BackendEvent) => void): Promise<UnlistenFn> {
  return listen<BackendEvent>("backend-event", (event) => handler(event.payload));
}
