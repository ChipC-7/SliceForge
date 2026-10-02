<script setup lang="ts">
defineProps<{ progress: number; pct: string; rate: string }>();
</script>

<template>
  <div class="row" style="gap: 12px">
    <div class="progress-track">
      <div class="progress-fill" :style="{ width: `${(progress * 100).toFixed(1)}%` }"></div>
    </div>
    <span class="pct">{{ pct }}</span>
  </div>
  <div v-if="rate" class="info-line">{{ rate }}</div>
</template>
