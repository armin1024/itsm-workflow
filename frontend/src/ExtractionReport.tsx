import { InlineNotification } from "@carbon/react";

export default function ExtractionReport({ outcome, publishable, diagnostics }: { outcome?: string; publishable?: boolean; diagnostics?: unknown }) {
  const report = diagnostics && typeof diagnostics === "object" ? diagnostics as Record<string, unknown> : {};
  const failures = Array.isArray(report.failedOperations) ? report.failedOperations as Record<string, unknown>[] : [];
  const issues = Array.isArray(report.blockingIssues) ? report.blockingIssues as Record<string, unknown>[] : [];
  const partial = outcome === "PARTIAL_SUCCESS" || publishable === false;
  return <section className="extraction-report" aria-label="提取完整性诊断">
    <InlineNotification kind={partial || !outcome ? "warning" : "info"} hideCloseButton
      title={partial ? "提取部分成功，存在异常遗漏或待核实依赖" : outcome ? "草稿提取完整，仍需人工审核" : "未提供完整性诊断"}
      subtitle={partial ? "允许编辑和保存草稿。请核实遗漏步骤及输入来源，正式发布须由tec01完成人工处理与发布校验。" : outcome ? "完整性检查不代替业务审核或执行确认。" : "这是历史提取记录，不能自动认定完整；建议重新提取。"} />
    {report.auditOperationCount != null && <p>审计操作 {String(report.auditOperationCount)} 条 · 保留 {String(report.acceptedOperationCount ?? 0)} 条 · 正常过滤 {String(report.ignoredOperationCount ?? 0)} 条 · 处理异常 {String(report.failedOperationCount ?? 0)} 条</p>}
    {failures.length > 0 && <ol>{failures.map((item, index) => <li key={`${item.auditIndex}-${index}`}><strong>操作 {String(item.operationId ?? "无ID")} · auditTimeline[{String(item.auditIndex)}]</strong><p>{String(item.message)}</p><small>{String(item.stage)} · {String(item.code)}</small></li>)}</ol>}
    {issues.map((item, index) => <p key={index}><strong>待核实：</strong>{String(item.message)} {Array.isArray(item.nodeIds) ? item.nodeIds.join("、") : ""}</p>)}
  </section>;
}
