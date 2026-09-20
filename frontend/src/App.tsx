import { useEffect, useState } from "react";
import { Button, InlineLoading, InlineNotification, TextInput } from "@carbon/react";
import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import ApiDocs from "./ApiDocs";
import CompilerLab from "./CompilerLab";
import NodeManagement from "./NodeManagement";
import NodeStudio from "./NodeStudio";
import WorkflowLab from "./WorkflowLab";

function Login({ onReady }: { onReady: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try { await api.login(token); setToken(""); onReady(); }
    catch (value) { setError((value as Error).message); }
    finally { setBusy(false); }
  };
  return <main className="studio-login"><section><div className="product-mark"><span>IW</span><strong>Runtime Studio</strong></div><div><p className="overline">LOCAL RUNTIME CONTROL</p><h1>节点定义与执行，保持在同一个运行时。</h1><p>管理Node Registry、提取工作流草稿，并在进入tec01生产流程前完成单步和整流程验证。</p></div><small>TEST_ONLY · 内网部署 · 不保存AOPS API Key</small></section><form onSubmit={submit}><p className="overline">管理身份</p><h2>输入管理Token</h2><p>Token仅用于建立HttpOnly Studio会话，不是AOPS执行凭据。</p>{error && <InlineNotification kind="error" title="无法进入Studio" subtitle={error} hideCloseButton />}<TextInput id="admin-token" labelText="STUDIO_ADMIN_TOKEN" type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" required /><Button type="submit" disabled={busy}>{busy ? "正在验证" : "进入Runtime Studio"}</Button></form></main>;
}

function Shell({ onLogout }: { onLogout: () => void }) {
  return <div className="runtime-shell"><header className="topbar"><Link className="wordmark" to="/studio"><span>IW</span><strong>Runtime Studio</strong></Link><nav aria-label="主导航"><NavLink to="/studio">节点调试</NavLink><NavLink to="/nodes">Node管理</NavLink><NavLink to="/workflow">流程编排</NavLink><NavLink to="/compiler">草稿提取</NavLink><NavLink to="/docs">API文档</NavLink></nav><div className="identity"><span>TEST_ONLY</span><button onClick={onLogout}>退出</button></div></header><Routes><Route path="/studio" element={<NodeStudio />} /><Route path="/nodes" element={<NodeManagement />} /><Route path="/workflow" element={<WorkflowLab />} /><Route path="/compiler" element={<CompilerLab />} /><Route path="/docs" element={<ApiDocs />} /><Route path="*" element={<Navigate to="/studio" replace />} /></Routes></div>;
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean>();
  useEffect(() => { api.session().then((value) => setAuthenticated(value.authenticated)).catch(() => setAuthenticated(false)); }, []);
  if (authenticated === undefined) return <main className="boot"><InlineLoading description="检查Studio会话" /></main>;
  if (!authenticated) return <Login onReady={() => setAuthenticated(true)} />;
  return <Shell onLogout={async () => { await api.logout(); setAuthenticated(false); }} />;
}
