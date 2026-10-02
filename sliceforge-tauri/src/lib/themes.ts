/**
 * 主题系统
 * ========
 * 与 Flet 版同一套主题目录（同样的中文名与种子色），方便两版对比。
 * 配色不由每个主题手工写死，而是从「种子色 + 明暗模式」推导出整套
 * CSS 变量：中性色带一点种子色相（更协调），主色按明暗调整明度保证对比度。
 */

export type ThemeMode = "system" | "light" | "dark";

export interface ThemeOption {
  key: string;
  label: string;
  mode: ThemeMode;
  /** 主色种子；跟随系统时也用它做主色 */
  seed: string;
}

export const THEME_CATALOG: ThemeOption[] = [
  { key: "system", label: "跟随系统（自动）", mode: "system", seed: "#5e81ac" },
  { key: "nord-dark", label: "北欧夜色（深蓝灰·暗）", mode: "dark", seed: "#5e81ac" },
  { key: "nord-light", label: "北欧晨光（浅蓝白·亮）", mode: "light", seed: "#5e81ac" },
  { key: "tokyo-night", label: "东京之夜（靛蓝紫·暗）", mode: "dark", seed: "#7aa2f7" },
  { key: "catppuccin", label: "卡布奇诺（紫棕·暗）", mode: "dark", seed: "#cba6f7" },
  { key: "dracula", label: "德古拉（紫黑·暗）", mode: "dark", seed: "#bd93f9" },
  { key: "solarized", label: "暖阳纸白（米黄·亮）", mode: "light", seed: "#b58900" },
  { key: "everforest", label: "森林晨雾（浅绿·亮）", mode: "light", seed: "#8da101" },
];

export const DEFAULT_THEME = "nord-dark";
const STORAGE_KEY = "sliceforge.theme";

function hexToHsl(hex: string): [number, number] {
  const m = hex.replace("#", "");
  const r = parseInt(m.slice(0, 2), 16) / 255;
  const g = parseInt(m.slice(2, 4), 16) / 255;
  const b = parseInt(m.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h: number;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return [h * 360, s * 100];
}

function hsl(h: number, s: number, l: number, a = 1): string {
  return a >= 1
    ? `hsl(${h.toFixed(1)} ${s.toFixed(1)}% ${l.toFixed(1)}%)`
    : `hsl(${h.toFixed(1)} ${s.toFixed(1)}% ${l.toFixed(1)}% / ${a})`;
}

function prefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function resolveDark(option: ThemeOption): boolean {
  if (option.mode === "dark") return true;
  if (option.mode === "light") return false;
  return prefersDark();
}

/** 把主题写到 <html> 的 CSS 变量上；neutral 色也带种子色相，观感更一体。 */
export function applyTheme(key: string): ThemeOption {
  const option =
    THEME_CATALOG.find((t) => t.key === key) ??
    THEME_CATALOG.find((t) => t.key === DEFAULT_THEME)!;
  const dark = resolveDark(option);
  const [h, s] = hexToHsl(option.seed);
  const root = document.documentElement;
  const set = (k: string, v: string) => root.style.setProperty(k, v);

  if (dark) {
    set("--bg", hsl(h, 18, 8.5));
    set("--surface", hsl(h, 15, 12.5));
    set("--surface-2", hsl(h, 13, 17));
    set("--surface-3", hsl(h, 12, 21));
    set("--border", hsl(h, 13, 24));
    set("--border-strong", hsl(h, 12, 30));
    set("--text", hsl(h, 22, 93));
    set("--text-dim", hsl(h, 12, 64));
    set("--text-faint", hsl(h, 10, 48));
    set("--primary", hsl(h, Math.min(72, s + 16), 68));
    set("--primary-hover", hsl(h, Math.min(74, s + 16), 76));
    set("--on-primary", hsl(h, 30, 10));
    set("--primary-soft", hsl(h, Math.min(60, s + 10), 45, 0.28));
    set("--success", hsl(148, 48, 58));
    set("--warning", hsl(38, 75, 62));
    set("--danger", hsl(4, 68, 66));
    set("--shadow-1", `0 1px 2px ${hsl(h, 20, 4, 0.4)}`);
    set("--shadow-2", `0 12px 32px ${hsl(h, 20, 3, 0.45)}`);
    set("--overlay", hsl(h, 18, 4, 0.62));
  } else {
    set("--bg", hsl(h, 26, 96.5));
    set("--surface", hsl(h, 32, 99.5));
    set("--surface-2", hsl(h, 24, 94));
    set("--surface-3", hsl(h, 22, 90));
    set("--border", hsl(h, 18, 87));
    set("--border-strong", hsl(h, 16, 78));
    set("--text", hsl(h, 24, 13));
    set("--text-dim", hsl(h, 12, 40));
    set("--text-faint", hsl(h, 10, 56));
    set("--primary", hsl(h, Math.min(64, s + 20), 40));
    set("--primary-hover", hsl(h, Math.min(66, s + 20), 33));
    set("--on-primary", hsl(h, 40, 99));
    set("--primary-soft", hsl(h, Math.min(70, s + 24), 88));
    set("--success", hsl(150, 45, 33));
    set("--warning", hsl(36, 80, 38));
    set("--danger", hsl(2, 66, 46));
    set("--shadow-1", `0 1px 2px ${hsl(h, 24, 30, 0.08)}`);
    set("--shadow-2", `0 12px 32px ${hsl(h, 24, 25, 0.14)}`);
    set("--overlay", hsl(h, 20, 15, 0.4));
  }
  root.style.colorScheme = dark ? "dark" : "light";
  document.body.classList.toggle("dark", dark);
  return option;
}

export function loadSavedTheme(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

export function saveTheme(key: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, key);
  } catch {
    /* 忽略：无 localStorage 时仅不记忆 */
  }
}
