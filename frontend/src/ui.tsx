import { Tag } from "@carbon/react";

export const pretty = (value: unknown) => JSON.stringify(value, null, 2);
export const parseObject = (value: string, label: string) => {
  const parsed = JSON.parse(value);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label}必须是JSON对象`);
  return parsed as Record<string, unknown>;
};
export const parseArray = (value: string, label: string) => {
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed) || !parsed.every((item) => item && typeof item === "object" && !Array.isArray(item))) throw new Error(`${label}必须是JSON对象数组`);
  return parsed as Record<string, unknown>[];
};
export function statusLabel(value: string) {
  const labels: Record<string, string> = { DRAFT: "本地草稿", PENDING: "尚未开始", PLANNED: "待确认计划", QUEUED: "排队中", DISPATCHING: "正在下发", RUNNING: "执行中", WAITING: "等待输入", WAITING_INPUT: "等待人工输入", PAUSE_REQUESTED: "准备暂停", PAUSED: "已暂停", CANCEL_REQUESTED: "正在取消", CANCELLED: "已取消", SUCCEEDED: "已完成", FAILED: "失败", UNKNOWN: "结果未知", SKIPPED: "已跳过", READY: "已就绪", UNSUPPORTED: "含已停用节点", EXTRACTING: "提取中", SYNCED: "已同步", DISABLED: "已停用", ENABLED: "已启用", TEST: "真实调试" };
  return labels[value] || value;
}
export function Status({ value }: { value: string }) {
  const kind = value === "SUCCEEDED" || value === "READY" ? "green" : value === "FAILED" ? "red" : value.startsWith("WAITING") ? "warm-gray" : "cool-gray";
  return <Tag type={kind as "green"} title={value}>{statusLabel(value)}</Tag>;
}
