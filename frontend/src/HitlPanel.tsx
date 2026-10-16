import { useEffect, useState } from "react";
import { Button, InlineNotification, Select, SelectItem, TextArea, TextInput } from "@carbon/react";
import { pretty } from "./ui";

export function typedValue(raw: string, type: string): unknown {
  if (type === "integer" || type === "number") {
    if (raw.trim() === "") throw new Error("请输入数值");
    const number = Number(raw);
    if (!Number.isFinite(number) || (type === "integer" && !Number.isInteger(number))) throw new Error(type === "integer" ? "请输入整数" : "请输入有效数值");
    return number;
  }
  if (type === "boolean") return raw === "true";
  if (type === "array" || type === "object") {
    const value = JSON.parse(raw);
    if (type === "array" ? !Array.isArray(value) : !value || typeof value !== "object" || Array.isArray(value)) throw new Error(`请输入${type === "array" ? "数组" : "对象"}JSON`);
    return value;
  }
  return raw;
}
export function ValueInput({ id, label, type, value, onChange, options, required, description }: { id: string; label: string; type: string; value: string; onChange: (value: string) => void; options?: unknown[]; required?: boolean; description?: string }) {
  const labelText = `${label}${required ? " *" : ""}`;
  if (options || type === "boolean") return <Select id={id} labelText={labelText} helperText={description} value={value} onChange={(event) => onChange(event.target.value)}><SelectItem value="" text="请选择" />{(options || [true, false]).map((option) => <SelectItem key={String(option)} value={String(option)} text={String(option)} />)}</Select>;
  if (type === "array" || type === "object") return <TextArea id={id} labelText={`${labelText}（${type} JSON）`} helperText={description} rows={3} value={value} onChange={(event) => onChange(event.target.value)} />;
  return <TextInput id={id} labelText={labelText} helperText={description} value={value} type={type === "number" || type === "integer" ? "number" : "text"} onChange={(event) => onChange(event.target.value)} />;
}

export default function HitlPanel({ interaction, busy, onSubmit, onCancel }: { interaction: Record<string, unknown>; busy: boolean; onSubmit: (value: Record<string, unknown>) => void; onCancel: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({}), [error, setError] = useState("");
  const fields = (interaction.fields || []) as Array<Record<string, unknown>>;
  useEffect(() => { setValues({}); setError(""); }, [interaction]);
  if (interaction.kind && interaction.kind !== "HITL_FORM") return <section className="hitl-panel"><InlineNotification kind="error" title="此人工节点已不受支持" subtitle="当前版本仅支持HITL表单输入。请保留原始记录，改用表单节点后重新创建测试计划。" hideCloseButton /><Button kind="danger--ghost" onClick={onCancel}>移除本地计划</Button></section>;
  const submit = () => {
    setError("");
    try {
      {
        const result: Record<string, unknown> = {};
        for (const field of fields) {
          const name = String(field.name), raw = values[name] ?? (field.default == null ? "" : String(field.default));
          if (raw === "" && field.required === false) continue;
          if (raw === "" && field.required !== false) throw new Error(`请填写${String(field.label || name)}`);
          const value = typedValue(raw, String(field.type || "string"));
          if (typeof value === "number" && ((field.minimum != null && value < Number(field.minimum)) || (field.maximum != null && value > Number(field.maximum)))) throw new Error(`${String(field.label || name)}不在允许范围内`);
          result[name] = value;
        }
        onSubmit({ action: "SUBMIT", values: result });
      }
    } catch (error) { setError((error as Error).message); }
  };
  return <section className="hitl-panel"><header><div><p className="overline">HUMAN IN THE LOOP</p><h2>{String(interaction.title || "等待人工处理")}</h2><p>{String(interaction.description || "核对前置结果后填写或选择后续步骤的参数。")}</p></div><span>由用户确认</span></header>{interaction.context != null && <details><summary>查看前置节点上下文</summary><pre>{pretty(interaction.context)}</pre></details>}{error && <InlineNotification kind="error" title="输入未完成" subtitle={error} hideCloseButton />}<div className="typed-input-grid">{fields.map((field) => <ValueInput key={String(field.name)} id={`hitl-${String(field.name)}`} label={String(field.label || field.name)} type={String(field.type || "string")} required={field.required !== false} options={field.enum as unknown[] | undefined} description={String(field.description || "")} value={values[String(field.name)] ?? String(field.default ?? "")} onChange={(value) => setValues((current) => ({ ...current, [String(field.name)]: value }))} />)}</div><div className="panel-actions"><Button kind="danger--ghost" disabled={busy} onClick={onCancel}>取消运行并移除</Button><Button disabled={busy} onClick={submit}>确认参数并继续</Button></div></section>;
}
