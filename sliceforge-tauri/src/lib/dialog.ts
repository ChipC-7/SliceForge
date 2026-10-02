/** 应用内对话框（比原生对话框好看、且与主题一致），Promise 风格调用。 */

import { ref } from "vue";

export interface ModalState {
  kind: "alert" | "confirm";
  title: string;
  message: string;
  okLabel: string;
  cancelLabel: string;
  tone: "info" | "warning" | "danger";
  resolve: (value: boolean) => void;
}

export const modal = ref<ModalState | null>(null);

function openModal(state: Omit<ModalState, "resolve" | "cancelLabel"> & { cancelLabel?: string }) {
  return new Promise<boolean>((resolve) => {
    modal.value = { cancelLabel: "取消", ...state, resolve };
  });
}

export function alert(title: string, message: string, tone: "info" | "warning" | "danger" = "info") {
  return openModal({ kind: "alert", title, message, okLabel: "知道了", tone });
}

export function confirm(
  title: string,
  message: string,
  okLabel = "确定",
  tone: "info" | "warning" | "danger" = "info",
) {
  return openModal({ kind: "confirm", title, message, okLabel, tone });
}

/** 由 App.vue 的模态框调用。 */
export function settleModal(value: boolean): void {
  const current = modal.value;
  if (!current) return;
  modal.value = null;
  current.resolve(value);
}
