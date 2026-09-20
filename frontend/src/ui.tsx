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
export function Status({ value }: { value: string }) {
  const kind = value === "SUCCEEDED" || value === "READY" ? "green" : value === "FAILED" ? "red" : value.startsWith("WAITING") ? "warm-gray" : "cool-gray";
  return <Tag type={kind as "green"}>{value}</Tag>;
}
