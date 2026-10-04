<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { settleModal, modal } from "./lib/dialog";
import { initBackendEvents, statusText, tasks } from "./lib/store";
import {
  THEME_CATALOG,
  applyTheme,
  loadSavedTheme,
  saveTheme,
} from "./lib/themes";
import MergePanel from "./components/MergePanel.vue";
import SplitPanel from "./components/SplitPanel.vue";

const APP_VERSION = "1.0.0";

const activeTab = ref<"split" | "merge">("split");
const themeKey = ref(loadSavedTheme());

const media = window.matchMedia("(prefers-color-scheme: dark)");
const systemDark = ref(media.matches);
const onMediaChange = (event: MediaQueryListEvent): void => {
  systemDark.value = event.matches;
};
media.addEventListener("change", onMediaChange);

// 「跟随系统」模式下系统明暗切换时也要重算配色
watch([themeKey, systemDark], ([key]) => applyTheme(key));
watch(themeKey, (key) => saveTheme(key));

const statusTone = computed(() => {
  if (tasks.split.busy || tasks.merge.busy) return "busy";
  if (statusText.value.includes("失败") || statusText.value.includes("不一致")) return "error";
  if (statusText.value.includes("完成")) return "ok";
  return "";
});

onMounted(async () => {
  applyTheme(themeKey.value);
  await initBackendEvents();
});

onBeforeUnmount(() => media.removeEventListener("change", onMediaChange));
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div>
        <h1 class="app-title">切片工坊</h1>
        <p class="app-subtitle">大文件切割 / SHA-256 校验 / 流式合并还原 · v{{ APP_VERSION }}</p>
      </div>
      <div class="header-spacer"></div>
      <div class="theme-picker">
        <span class="theme-dot" aria-hidden="true"></span>
        <span>主题</span>
        <select v-model="themeKey" class="text-input theme-select">
          <option v-for="theme in THEME_CATALOG" :key="theme.key" :value="theme.key">
            {{ theme.label }}
          </option>
        </select>
      </div>
    </header>

    <nav class="tab-bar" role="tablist">
      <div
        class="tab-indicator"
        :style="{ transform: `translateX(${activeTab === 'split' ? '0%' : '100%'})` }"
      ></div>
      <button
        class="tab-item"
        :class="{ active: activeTab === 'split' }"
        role="tab"
        @click="activeTab = 'split'"
      >
        <span>✂ 切割</span>
      </button>
      <button
        class="tab-item"
        :class="{ active: activeTab === 'merge' }"
        role="tab"
        @click="activeTab = 'merge'"
      >
        <span>⬇ 合并</span>
      </button>
    </nav>

    <main class="panel-host">
      <SplitPanel v-show="activeTab === 'split'" />
      <MergePanel v-show="activeTab === 'merge'" />
    </main>

    <footer class="statusbar">
      <div class="status-main">
        <span class="status-dot" :class="statusTone"></span>
        <span>{{ statusText }}</span>
      </div>
      <span class="faint">缓冲区 4.00 MB · 全程流式读写</span>
    </footer>

    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="modal"
          class="modal-overlay"
          @click.self="settleModal(modal.kind !== 'confirm')"
        >
          <div class="modal-card">
            <h3 class="modal-title">{{ modal.title }}</h3>
            <p class="modal-message">{{ modal.message }}</p>
            <div class="modal-actions">
              <button
                v-if="modal.kind === 'confirm'"
                class="btn btn-ghost"
                @click="settleModal(false)"
              >
                {{ modal.cancelLabel }}
              </button>
              <button
                class="btn"
                :class="modal.tone === 'info' ? 'btn-primary' : 'btn-ghost danger'"
                @click="settleModal(true)"
              >
                {{ modal.okLabel }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
