import { useEffect, useState } from "react";
import { Button, InlineLoading, InlineNotification, Tag } from "@carbon/react";
import { api } from "./api";

export type RuntimeConfiguration = {
  runtimeVersion: string; readOnly: boolean; storage: string; serverDatabase: boolean; executorId: string;
  logging: { directory: string; level: string; retentionDays: number; maxTotalBytes: number; maxFileBytes: number; cleanupIntervalSeconds: number };
  tec01: {
    baseUrl: string; serviceTokenConfigured: boolean; runCallbackPath: string; extractionCallbackPath: string;
    runCallbackUrl: string; extractionCallbackUrl: string; timeoutSeconds: number; callbackMaxAttempts: number;
    callbackWaitSeconds: number; extractionMaxAttempts: number;
  };
};

function Field({ label, value }: { label: string; value: string | number }) {
  return <div><dt>{label}</dt><dd><code>{value || "未配置"}</code></dd></div>;
}

export default function RuntimeConfig() {
  const [data, setData] = useState<RuntimeConfiguration>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true); setError("");
    try { setData(await api.configuration()); }
    catch (value) { setError((value as Error).message); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);
  return <main className="page runtime-config">
    <header className="page-title"><div><p className="overline">RUNTIME CONFIGURATION</p><h1>运行配置</h1><p>查看服务端当前生效值。修改配置文件后需重启服务；此页面不提供修改入口。</p></div><Button kind="tertiary" disabled={busy} onClick={load}>刷新配置</Button></header>
    {error && <InlineNotification kind="error" title="无法读取配置" subtitle={error} hideCloseButton />}
    {busy && <InlineLoading description="读取运行配置" />}
    {data && <>
      <div className="runtime-config-meta"><Tag type="teal">只读</Tag><span>版本 {data.runtimeVersion}</span><span>执行器 {data.executorId}</span></div>
      <section className="runtime-config-section"><h2>tec01 连接</h2><dl><Field label="TEC01_BASE_URL" value={data.tec01.baseUrl} /><Field label="TEC01_SERVICE_TOKEN" value={data.tec01.serviceTokenConfigured ? "已配置（不展示令牌）" : "未配置"} /></dl>{!data.tec01.baseUrl && <p>未配置 tec01 地址，本地调试仍可使用；开启状态同步前请先配置连接。</p>}</section>
      <div className="runtime-config-callbacks">
        <section className="runtime-config-section"><h2>执行回报</h2><p>节点开始、节点完成、流程释放均使用此接口。</p><dl><Field label="TEC01_RUN_CALLBACK_PATH" value={data.tec01.runCallbackPath} /><Field label="完整地址模板 · POST" value={data.tec01.runCallbackUrl} /><Field label="单次请求超时（秒，生效上限30秒）" value={data.tec01.timeoutSeconds} /><Field label="TEC01_CALLBACK_MAX_ATTEMPTS · 最多尝试次数" value={data.tec01.callbackMaxAttempts} /><Field label="TEC01_CALLBACK_WAIT_SECONDS · 单次回报总等待（秒）" value={data.tec01.callbackWaitSeconds} /></dl><p>对5xx、连接异常和超时有限重试，4xx及无效确认不重试；达到次数或总等待上限即停止。收到匹配本次节点的已保存确认后，才继续下一节点。</p></section>
        <section className="runtime-config-section"><h2>提取回报</h2><p>提取进度和最终结果均使用此接口。</p><dl><Field label="TEC01_EXTRACTION_CALLBACK_PATH" value={data.tec01.extractionCallbackPath} /><Field label="完整地址模板 · POST" value={data.tec01.extractionCallbackUrl} /><Field label="单次请求超时（秒）" value={data.tec01.timeoutSeconds} /><Field label="最多尝试次数 · 提取既有策略" value={data.tec01.extractionMaxAttempts} /></dl><p>占位符由服务替换为实际 ID，无需手工拼接。变更地址不改变回报结构或确认协议。</p></section>
      </div>
      <section className="runtime-config-section"><h2>本地数据与运行内存</h2><p>知识、草稿、计划和调试结果由当前浏览器的 IndexedDB 保存，服务端不使用数据库。清除站点数据或更换访问域名不会迁移这些本地记录，请先导出备份。</p><p>执行中的计划仅保存在服务端内存；服务重启会释放活动运行。生产数据由 tec01 保存，AOPS_API_KEY 不写入浏览器存储。</p></section>
      <section className="runtime-config-section"><h2>运维日志 · 只读策略</h2><dl><Field label="LOG_DIR" value={data.logging.directory} /><Field label="LOG_LEVEL" value={data.logging.level} /><Field label="LOG_RETENTION_DAYS" value={data.logging.retentionDays} /><Field label="LOG_MAX_TOTAL_MB · MiB" value={data.logging.maxTotalBytes / 1024 / 1024} /><Field label="LOG_MAX_FILE_MB · MiB" value={data.logging.maxFileBytes / 1024 / 1024} /><Field label="LOG_CLEANUP_INTERVAL_SECONDS" value={data.logging.cleanupIntervalSeconds} /></dl><p>日志是安全元数据滚动文件，不是业务数据库。实际占用与清理结果可在“日志”菜单查看；journalctl副本由系统管理。</p></section>
    </>}
  </main>;
}
