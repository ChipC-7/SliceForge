<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { pickDir, query, type InspectInfo } from "../lib/backend";
import { alert, confirm } from "../lib/dialog";
import { formatSize } from "../lib/format";
import { pushLog, refreshTick, requestCancel, runTask, tasks } from "../lib/store";
import LogView from "./LogView.vue";
import ProgressBlock from "./ProgressBlock.vue";

const view = tasks.merge;
const dirPath = ref("");
const info = ref<InspectInfo | null>(null);

const manifestLine = computed(() => {
  const data = info.value;
  if (!data) return "（选择文件夹后显示清单信息）";
  if (!data.ok) return `⚠ ${data.error ?? "清单不可用"}`;
  const base =
    `原始文件：${data.original_name} · 大小：${formatSize(data.original_size)} · ` +
    `每片：${formatSize(data.chunk_size)} · 总片数：${data.part_count}`;
  const missing = data.missing ?? [];
  return missing.length > 0 ? `${base} · ⚠ 缺失 ${missing.length} 片` : base;
});

const manifestTone = computed(() => (info.value?.ok ? "" : "tone-warning"));

const outLine = computed(() => {
  const data = info.value;
  if (!data || !data.ok) return "";
  const base = `将还原到：${data.out_path}`;
  return data.out_exists ? `${base} · ⚠ 该文件已存在，合并时会询问是否覆盖` : base;
});

const canStart = computed(
  () => view.busy === false && dirPath.value.trim() !== "" && info.value?.ok === true,
);

async function browse(): Promise<void> {
  const picked = await pickDir("选择包含切片与 manifest.json 的文件夹");
  if (picked) dirPath.value = picked;
}

function cleanPath(): string {
  return dirPath.value.trim().replace(/^"|"$/g, "");
}

async function reloadInfo(): Promise<void> {
  const text = cleanPath();
  if (!text) {
    info.value = null;
    return;
  }
  try {
    info.value = await query("inspect", text);
  } catch {
    info.value = { ok: false, error: "读取清单失败" };
  }
}

let timer: number | undefined;
watch(dirPath, () => {
  info.value = null;
  window.clearTimeout(timer);
  timer = window.setTimeout(reloadInfo, 180);
});

watch(refreshTick, reloadInfo);

async function start(): Promise<void> {
  if (view.busy) return;
  const text = cleanPath();
  if (!text) {
    await alert("无法开始", "请先选择包含切片与 manifest.json 的文件夹。", "warning");
    return;
  }
  const data = info.value;
  if (!data || !data.ok) {
    await alert("清单不可用", data?.error ?? "该文件夹里没有可用的 manifest.json。", "warning");
    return;
  }
  let overwrite = false;
  if (data.out_exists) {
    overwrite = await confirm(
      "覆盖确认",
      `输出文件已存在：\n${data.out_path}\n\n是否覆盖它？`,
      "覆盖",
      "warning",
    );
    if (!overwrite) return;
  }
  pushLog("merge", `任务开始：合并 ${text.split(/[\\/]/).pop()}`, "info");
  pushLog("merge", `共 ${data.part_count} 片，目标文件 ${data.out_path}`, "dim");
  await runTask("merge", text, { overwrite });
}
</script>

<template>
  <div class="panel-grid">
    <h3 class="section-title">1 · 选择包含全部切片与 manifest.json 的文件夹</h3>
    <div class="row">
      <input
        v-model="dirPath"
        class="text-input"
        style="flex: 1"
        placeholder="点「浏览…」选择 .parts 文件夹，或直接粘贴路径"
        spellcheck="false"
      />
      <button class="btn btn-ghost" @click="browse">浏览…</button>
    </div>

    <h3 class="section-title">2 · 清单信息</h3>
    <div class="info-line" :class="manifestTone">{{ manifestLine }}</div>
    <div v-if="outLine" class="info-line">{{ outLine }}</div>

    <div class="row" style="margin-top: 2px">
      <button class="btn btn-primary btn-big" :disabled="!canStart" @click="start">
        ▶ 校验并合并
      </button>
      <button
        class="btn btn-ghost danger btn-big"
        :disabled="!view.busy || view.cancelling"
        @click="requestCancel('merge')"
      >
        ■ 取消
      </button>
    </div>

    <ProgressBlock :progress="view.progress" :pct="view.pct" :rate="view.rate" />
    <LogView :logs="view.logs" />
  </div>
</template>
