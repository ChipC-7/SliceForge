<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import type { LogEntry } from "../lib/store";

const props = defineProps<{ logs: LogEntry[] }>();
const scroller = ref<HTMLElement | null>(null);

watch(
  () => props.logs.length,
  async () => {
    await nextTick();
    const el = scroller.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);
</script>

<template>
  <div class="log-box">
    <div ref="scroller" class="log-scroll">
      <div v-if="props.logs.length === 0" class="log-empty">暂无日志</div>
      <div
        v-for="entry in props.logs"
        :key="entry.id"
        class="log-line"
        :class="`level-${entry.level}`"
      >
        <span class="log-time">{{ entry.time }}</span>
        <span class="log-text">{{ entry.text }}</span>
      </div>
    </div>
  </div>
</template>
