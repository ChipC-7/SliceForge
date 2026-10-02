<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { pickFile, query, type PlanInfo } from "../lib/backend";
import { alert, confirm } from "../lib/dialog";
import { formatSize, groupNumber, parseChunkSize } from "../lib/format";
import { pushLog, refreshTick, requestCancel, runTask, tasks } from "../lib/store";
import LogView from "./LogView.vue";
import ProgressBlock from "./ProgressBlock.vue";

const QUICK_SIZES = ["1M", "10M", "50M", "100M", "500M", "1G"];

const view = tasks.split;
const srcPath = ref("");
const chunk = ref("10M");
const plan = ref<PlanInfo | null>(null);

const chunkBytes = computed(() => parseChunkSize(chunk.value));
const chunkInvalid = computed(() => chunk.value.trim() !== "" && chunkBytes.value === null);
const chunkHint = computed(() => {
  if (chunk.value.trim() === "") return "请输入每片大小，如 5M / 512K / 2G / 100MB 或纯字节数";
  if (chunkBytes.value === null)
    return "每片大小格式不正确：正确写法如 5M、512K、2G、100MB 或纯字节数";
  return `= ${formatSize(chunkBytes.value)}（${groupNumber(chunkBytes.value)} 字节）`;
});

const outLine = computed(() => {
  const info = plan.value;
  if (!info) return "（选择文件后自动确定）";
  if (info.error) return `⚠ ${info.error}`;
  if (!info.parts_dir) return "（选择文件后自动确定）";
  const base = `输出目录：${info.parts_dir}`;
  if (!info.exists) return `${base} · （文件不存在）`;
  return `${base} · 源文件大小：${formatSize(info.size)}`;
});

const canStart = computed(() => view.busy === false && srcPath.value.trim() !== "");

async function browse(): Promise<void> {
  const picked = await pickFile("选择要切割的文件");
  if (picked) srcPath.value = picked;
}

function cleanPath(): string {
  return srcPath.value.trim().replace(/^"|"$/g, "");
}

async function reloadPlan(): Promise<void> {
  const text = cleanPath();
  if (!text) {
    plan.value = null;
    return;
  }
  try {
    plan.value = await query("plan", text);
  } catch {
    plan.value = null;
  }
}

let timer: number | undefined;
watch(srcPath, () => {
  plan.value = null;
  window.clearTimeout(timer);
  timer = window.setTimeout(reloadPlan, 180);
});

watch(refreshTick, reloadPlan);

async function start(): Promise<void> {
  if (view.busy) return;
  const text = cleanPath();
  if (!text) {
    await alert("无法开始", "请先选择要切割的文件。", "warning");
    return;
  }
  const info = plan.value;
  if (!info || !info.exists) {
    await alert("无法开始", `找不到该文件或它不是普通文件：\n${text}`, "warning");
    return;
  }
  if (info.size === 0) {
    await alert("空文件", "所选文件是空文件（0 字节），没有需要切割的内容。", "warning");
    return;
  }
  if (chunkBytes.value === null) {
    await alert("每片大小不合法", "每片大小格式不正确：正确写法如 5M、512K、2G、100MB 或纯字节数。", "warning");
    return;
  }
  let overwrite = false;
  if (info.manifest_exists) {
    overwrite = await confirm(
      "覆盖确认",
      `输出目录中已存在切片清单：\n${info.parts_dir}\n\n是否覆盖原有切片？`,
      "覆盖",
      "warning",
    );
    if (!overwrite) return;
  }
  const bytes = chunkBytes.value ?? 0;
  const total = Math.max(1, Math.ceil((info.size ?? 0) / bytes));
  const name = text.split(/[\\/]/).pop() ?? text;
  pushLog("split", `任务开始：切割 ${name}`, "info");
  pushLog("split", `每片大小 ${formatSize(bytes)}，预计 ${total} 片，输出目录 ${info.parts_dir}`, "dim");
  await runTask("split", text, { chunk: chunk.value.trim(), overwrite });
}
</script>

<template>
  <div class="panel-grid">
    <h3 class="section-title">1 · 选择待切割文件</h3>
    <div class="row">
      <input
        v-model="srcPath"
        class="text-input"
        style="flex: 1"
        placeholder="点「浏览…」选择，或直接粘贴文件路径"
        spellcheck="false"
      />
      <button class="btn btn-ghost" @click="browse">浏览…</button>
    </div>

    <h3 class="section-title">2 · 设置每片大小</h3>
    <div class="card stack">
      <div class="row">
        <input
          v-model="chunk"
          class="text-input"
          :class="{ invalid: chunkInvalid }"
          style="width: 170px"
          spellcheck="false"
        />
        <span class="muted">支持 5M / 512K / 2G / 100MB，也可直接填字节数</span>
      </div>
      <div class="row row-wrap">
        <span class="muted">快捷：</span>
        <button
          v-for="size in QUICK_SIZES"
          :key="size"
          class="btn btn-ghost btn-chip"
          @click="chunk = size"
        >
          {{ size }}
        </button>
      </div>
      <div class="info-line" :class="{ 'tone-warning': chunkInvalid }">{{ chunkHint }}</div>
    </div>

    <h3 class="section-title">3 · 输出目录</h3>
    <div class="info-line">{{ outLine }}</div>

    <div class="row" style="margin-top: 2px">
      <button class="btn btn-primary btn-big" :disabled="!canStart" @click="start">
        ▶ 开始切割
      </button>
      <button
        class="btn btn-ghost danger btn-big"
        :disabled="!view.busy || view.cancelling"
        @click="requestCancel('split')"
      >
        ■ 取消
      </button>
    </div>

    <ProgressBlock :progress="view.progress" :pct="view.pct" :rate="view.rate" />
    <LogView :logs="view.logs" />
  </div>
</template>
