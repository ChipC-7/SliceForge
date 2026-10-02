/** 数字与文本格式化，语义对齐 core.format_size / core._format_duration。 */

export function formatSize(num: number | null | undefined): string {
  const n = Number(num);
  if (!Number.isFinite(n) || n < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB", "EB"];
  let value = n;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return i === 0 ? `${value} ${units[i]}` : `${value.toFixed(2)} ${units[i]}`;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

export function groupNumber(n: number | null | undefined): string {
  const v = Number(n);
  return Number.isFinite(v) ? v.toLocaleString("en-US") : "—";
}

export function nowClock(): string {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

/**
 * 每片大小的「即时换算提示」用（与 core.parse_chunk_size 同规则）。
 * 注意这只是显示层的快速反馈；真正权威的解析仍由 backend.py 在启动任务时
 * 用 core.parse_chunk_size 做一遍，两边即便有出入也只影响提示文案、不影响正确性。
 */
const SIZE_RE = /^(\d+(?:\.\d+)?)\s*([KMGTPE]?)\s*(B?)$/i;
const UNIT_MULTIPLIER: Record<string, number> = {
  "": 1,
  B: 1,
  K: 1024,
  M: 1024 ** 2,
  G: 1024 ** 3,
  T: 1024 ** 4,
  P: 1024 ** 5,
  E: 1024 ** 6,
};

/** 合法返回字节数，非法返回 null。 */
export function parseChunkSize(value: string | null | undefined): number | null {
  if (value == null) return null;
  const m = SIZE_RE.exec(value.trim());
  if (!m) return null;
  const number = Number(m[1]);
  const mult = UNIT_MULTIPLIER[m[2].toUpperCase() ?? ""] ?? 1;
  const bytes = number * mult;
  if (!Number.isFinite(bytes) || !Number.isInteger(bytes) || bytes <= 0) return null;
  return bytes;
}
