import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  InlineLoading,
  InlineNotification,
  Modal,
  ProgressBar,
  Tag,
  TextArea,
  TextInput,
} from "@carbon/react";
import {
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import { api } from "./api";
import WorkflowCanvas from "./WorkflowCanvas";
import WorkflowEditor, {
  newWorkflow,
  type EditableWorkflow,
} from "./WorkflowEditor";
import type {
  Knowledge,
  NodeStatus,
  Run,
  WorkflowEvent,
  WorkflowNode,
} from "./types";
import { withBase } from "./runtime";
import { ListPagination, ListToolbar } from "./ListControls";
import { useListQuery } from "./useListQuery";
import type { KnowledgeSummary, Paginated, RunSummary } from "./types";
import type { WorkflowVersionSummary } from "./types";
import type { NodeCatalogItem } from "./types";

type User = { uid: string; isAdmin: boolean; isOperator: boolean; features?: { studio?: boolean } };

const statusLabel: Record<string, string> = {
  DRAFT: "草稿",
  PENDING_REVIEW: "待审核",
  PUBLISHED: "已发布",
  WAITING_PLAN_APPROVAL: "等待计划确认",
  QUEUED: "排队中",
  RUNNING: "执行中",
  PAUSE_REQUESTED: "准备暂停",
  PAUSED: "已暂停",
  WAITING_INPUT: "等待输入",
  WAITING_NODE_APPROVAL: "等待节点批准",
  WAITING_CREDENTIAL: "凭据已过期",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  CANCEL_REQUESTED: "正在取消",
  CANCELLED: "已取消",
  UNKNOWN: "结果未知",
};

function StatusTag({ status }: { status: string }) {
  const type =
    status === "SUCCEEDED" || status === "PUBLISHED"
      ? "green"
      : status === "FAILED" || status === "CANCELLED"
        ? "red"
        : status === "RUNNING"
          ? "teal"
          : status.startsWith("WAITING") ||
              status === "UNKNOWN" ||
              status === "PENDING_REVIEW"
            ? "warm-gray"
            : "cool-gray";
  return <Tag type={type as "green"}>{statusLabel[status] || status}</Tag>;
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onLogin(await api.login(key));
      setKey("");
    } catch (value) {
      setError(value instanceof Error ? value.message : "登录失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login-page">
      <section className="login-context">
        <div className="product-mark">
          <span>ITSM</span>
          <strong>Workflow Control</strong>
        </div>
        <div>
          <p className="overline">PRODUCTION OPERATIONS</p>
          <h1>
            每一步都有状态，
            <br />
            每一次恢复都有依据。
          </h1>
          <p>通过节点级审计、中断和恢复控制 AOPS 生产操作。</p>
        </div>
        <small>内网部署 · 不发送运行数据到外部平台</small>
      </section>
      <section className="login-form-wrap">
        <form className="login-form" onSubmit={submit}>
          <p className="overline">身份验证</p>
          <h2>进入执行中心</h2>
          <p>使用个人 AOPS API Key 验证 UID。凭据仅加密保存于运行期。</p>
          {error && (
            <InlineNotification
              kind="error"
              title="无法登录"
              subtitle={error}
              hideCloseButton
            />
          )}
          <TextInput
            id="api-key"
            labelText="AOPS API Key"
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            required
            autoComplete="off"
          />
          <Button type="submit" disabled={busy}>
            {busy ? "正在验证" : "验证并进入"}
          </Button>
        </form>
      </section>
    </main>
  );
}

function Shell({ user, onLogout }: { user: User; onLogout: () => void }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link className="wordmark" to="/runs">
          <span>IW</span>
          <strong>ITSM Workflow</strong>
        </Link>
        <nav aria-label="主导航">
          <NavLink to="/runs">执行中心</NavLink>
          <NavLink to="/runs/new">创建运行</NavLink>
          <NavLink to="/knowledge">知识与版本</NavLink>
          {user.isAdmin && <NavLink to="/transfers">导入导出</NavLink>}
          {user.isAdmin && user.features?.studio && <NavLink to="/studio">节点Studio</NavLink>}
        </nav>
        <div className="identity">
          <span>{user.uid}</span>
          <button onClick={onLogout}>退出</button>
        </div>
      </header>
      <Routes>
        <Route path="/runs" element={<RunList user={user} />} />
        <Route path="/runs/new" element={<NewRun />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="/knowledge" element={<KnowledgeList user={user} />} />
        <Route path="/knowledge/new" element={<NewKnowledge />} />
        <Route path="/knowledge/extract" element={<ExtractKnowledge />} />
        <Route
          path="/knowledge/:knowledgeId/edit"
          element={<EditKnowledge />}
        />
        <Route path="/transfers" element={user.isAdmin ? <TransferCenter /> : <Navigate to="/knowledge" replace />} />
        <Route path="/studio" element={user.isAdmin && user.features?.studio ? <StudioPage /> : <Navigate to="/runs" replace />} />
        <Route
          path="/knowledge/:knowledgeId"
          element={<KnowledgeDetail user={user} />}
        />
        <Route path="*" element={<Navigate to="/runs" replace />} />
      </Routes>
    </div>
  );
}

function RunList({ user }: { user: User }) {
  const query = useListQuery("statusGroup", "");
  const exactKey = JSON.stringify(query.exactFilters);
  const [data, setData] = useState<Paginated<RunSummary> | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date>();
  const requestSequence = useRef(0);
  const load = useCallback(
    async (background = false) => {
      const sequence = ++requestSequence.current;
      if (!background) setLoading(true);
      setError("");
      try {
        const result = await api.runs({
          page: query.page,
          pageSize: query.pageSize,
          keyword: query.keyword,
          statusGroup: query.status || undefined,
          ...query.exactFilters,
        });
        if (sequence === requestSequence.current) {
          setData(result);
          setLastUpdated(new Date());
          if (result.page !== query.page) query.setPage(result.page);
        }
      } catch (value) {
        if (sequence === requestSequence.current)
          setError((value as Error).message);
      } finally {
        if (sequence === requestSequence.current) setLoading(false);
      }
    },
    [query.page, query.pageSize, query.keyword, query.status, exactKey],
  );
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    const hasActive = data?.items.some(
      (item) => !["SUCCEEDED", "FAILED", "CANCELLED"].includes(item.status),
    );
    if (!hasActive) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, 10000);
    return () => window.clearInterval(timer);
  }, [data?.items, load]);
  return (
    <main className="page">
      <header className="page-title">
        <div>
          <p className="overline">RUN CONTROL</p>
          <h1>执行中心</h1>
          <p>查看当前节点、等待原因和生产执行结果。</p>
        </div>
        <Button as={Link} to="/runs/new">
          创建运行
        </Button>
      </header>
      <ListToolbar
        keyword={query.draftKeyword}
        placeholder="工单 ID / 运行 ID / 经验名称 / 发起人 UID"
        status={query.status}
        statusOptions={[
          { value: "", label: "全部状态" },
          { value: "ACTIVE", label: "执行中" },
          { value: "WAITING", label: "等待处理" },
          { value: "SUCCEEDED", label: "已完成" },
          { value: "FAILED", label: "失败或取消" },
        ]}
        loading={loading}
        lastUpdated={lastUpdated}
        onKeywordChange={query.setDraftKeyword}
        onKeywordCommit={query.commitKeyword}
        onStatusChange={query.setStatus}
        onRefresh={() => void load()}
        onClear={query.clear}
        exactValues={query.exactFilters}
        onExactChange={query.setExactFilter}
        exactFields={[
          { key: "runId", label: "运行 ID" },
          { key: "knowledgeId", label: "知识 ID" },
          { key: "workflowVersionId", label: "版本 ID" },
          { key: "ticketId", label: "工单 ID" },
          ...(user.isAdmin ? [{ key: "initiatedBy", label: "发起人 UID" }] : []),
          { key: "currentNodeId", label: "当前节点 ID" },
          { key: "createdFrom", label: "创建时间起", type: "datetime-local" as const },
          { key: "createdTo", label: "创建时间止", type: "datetime-local" as const },
        ]}
      />
      {error && (
        <InlineNotification
          kind="error"
          title="无法加载运行"
          subtitle={error}
          hideCloseButton
        />
      )}
      {loading && data === null ? (
        <RunSkeleton />
      ) : data && data.items.length === 0 ? (
        <section className="empty-state">
          <span>00</span>
          <h2>
            {query.keyword || query.status
              ? "当前条件没有匹配运行"
              : "还没有运行记录"}
          </h2>
          <p>
            {query.keyword || query.status
              ? "调整关键词或状态，或者清空查询条件。"
              : "从一条已发布经验创建计划，确认后才会进入执行队列。"}
          </p>
          {query.keyword || query.status ? (
            <Button onClick={query.clear}>清空查询条件</Button>
          ) : (
            <Button as={Link} to="/runs/new">
              创建第一条运行
            </Button>
          )}
        </section>
      ) : data ? (
        <>
          <section
            className={`run-table ${loading ? "is-refreshing" : ""}`}
            aria-label="运行记录"
          >
            <div className="run-row header">
              <span>状态</span>
              <span>工单 / 运行</span>
              <span>当前节点</span>
              <span>发起人</span>
              <span>进度</span>
              <span>创建时间</span>
            </div>
            {data.items.map((run) => (
              <Link
                className="run-row"
                to={`/runs/${run.runId}`}
                key={run.runId}
              >
                <StatusTag status={run.status} />
                <span>
                  <strong>#{run.ticketId}</strong>
                  <small>{run.runId}</small>
                </span>
                <span>
                  {run.currentNodeId ||
                    run.waitingReason ||
                    statusLabel[run.status] ||
                    "尚未开始"}
                </span>
                <span>{run.initiatedBy}</span>
                <span>
                  {run.progress.current ?? 0}/{run.progress.total ?? 0}
                </span>
                <time>{new Date(run.createdAt).toLocaleString()}</time>
              </Link>
            ))}
          </section>
          <ListPagination
            page={data.page}
            pageSize={data.pageSize}
            total={data.total}
            totalPages={data.totalPages}
            loading={loading}
            onPageChange={query.setPage}
            onPageSizeChange={query.setPageSize}
          />
        </>
      ) : null}
    </main>
  );
}

function RunSkeleton() {
  return (
    <div className="skeleton-list" aria-label="正在加载">
      {[1, 2, 3].map((i) => (
        <div key={i}>
          <i />
          <i />
          <i />
        </div>
      ))}
    </div>
  );
}

function NewRun() {
  const navigate = useNavigate();
  const [knowledge, setKnowledge] = useState<KnowledgeSummary[]>([]);
  const [selectedKnowledge, setSelectedKnowledge] = useState<Knowledge | null>(
    null,
  );
  const [knowledgeKeyword, setKnowledgeKeyword] = useState("");
  const [knowledgeId, setKnowledgeId] = useState("");
  const [ticketId, setTicketId] = useState("");
  const [parameters, setParameters] = useState<
    Record<string, string | boolean>
  >({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api
        .knowledge({
          status: "PUBLISHED",
          pageSize: 20,
          keyword: knowledgeKeyword.trim(),
        })
        .then((value) => {
          setKnowledge(value.items);
          setKnowledgeId((current) =>
            value.items.some((item) => item.knowledgeId === current)
              ? current
              : value.items[0]?.knowledgeId || "",
          );
        })
        .catch((value) => setError(value.message));
    }, 350);
    return () => window.clearTimeout(timer);
  }, [knowledgeKeyword]);
  useEffect(() => {
    setSelectedKnowledge(null);
    if (knowledgeId)
      api
        .knowledgeById(knowledgeId)
        .then(setSelectedKnowledge)
        .catch((value) => setError(value.message));
  }, [knowledgeId]);
  const parameterDefinitions = selectedKnowledge
    ? Array.from(
        new Map(
          selectedKnowledge.workflowDefinition.nodes
            .flatMap((node) =>
              node.inputs.map((input) => ({ ...input, nodeTitle: node.title })),
            )
            .filter(
              (input) => input.source.kind === "RUN_INPUT" && input.source.key,
            )
            .map((input) => [String(input.source.key), input]),
        ).entries(),
      ).map(([key, input]) => ({ key, ...input }))
    : [];
  useEffect(() => {
    setParameters(
      Object.fromEntries(
        parameterDefinitions.map((item) => [
          item.key,
          item.type === "boolean" ? false : "",
        ]),
      ),
    );
  }, [knowledgeId, selectedKnowledge?.updatedAt]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const parsed = Object.fromEntries(
        parameterDefinitions.map((item) => {
          const raw = parameters[item.key];
          if (item.type === "integer")
            return [item.key, Number.parseInt(String(raw), 10)];
          if (item.type === "number") return [item.key, Number(String(raw))];
          if (item.type === "boolean") return [item.key, Boolean(raw)];
          if (item.type === "object" || item.type === "array")
            return [item.key, JSON.parse(String(raw))];
          return [item.key, String(raw)];
        }),
      );
      const run = await api.plan({
        knowledgeId,
        ticketId: Number(ticketId),
        parameters: parsed,
      });
      navigate(`/runs/${run.runId}`);
    } catch (value) {
      setError(value instanceof Error ? value.message : "无法生成计划");
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="page narrow">
      <header className="page-title">
        <div>
          <p className="overline">NEW RUN</p>
          <h1>创建执行计划</h1>
          <p>计划生成后不会立即执行。请先核对节点、数据库和参数。</p>
        </div>
      </header>
      <form className="form-stack" onSubmit={submit}>
        {error && (
          <InlineNotification
            kind="error"
            title="无法生成计划"
            subtitle={error}
            hideCloseButton
          />
        )}
        <TextInput
          id="knowledge-option-keyword"
          labelText="筛选已发布经验"
          value={knowledgeKeyword}
          onChange={(event) => setKnowledgeKeyword(event.target.value)}
          placeholder="名称、摘要、知识 ID 或来源工单"
        />
        <label className="select-label">
          已发布经验
          <select
            value={knowledgeId}
            onChange={(event) => setKnowledgeId(event.target.value)}
            required
          >
            <option value="">选择经验</option>
            {knowledge.map((item) => (
              <option value={item.knowledgeId} key={item.knowledgeId}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <TextInput
          id="ticket"
          labelText="工单 ID"
          type="number"
          min="1"
          value={ticketId}
          onChange={(event) => setTicketId(event.target.value)}
          required
        />
        <section className="run-parameters">
          <div>
            <h2>运行参数</h2>
            <p>根据所选经验自动生成。前置节点输出绑定不会在这里重复填写。</p>
          </div>
          {parameterDefinitions.length === 0 ? (
            <p className="inline-empty">该经验没有需要人工填写的运行参数。</p>
          ) : (
            <div className="parameter-fields">
              {parameterDefinitions.map((item) =>
                item.type === "boolean" ? (
                  <label className="select-label" key={item.key}>
                    {item.key}
                    <select
                      value={String(parameters[item.key] ?? false)}
                      onChange={(event) =>
                        setParameters((current) => ({
                          ...current,
                          [item.key]: event.target.value === "true",
                        }))
                      }
                    >
                      <option value="false">否 / false</option>
                      <option value="true">是 / true</option>
                    </select>
                    <small>
                      {item.description || `用于节点：${item.nodeTitle}`}
                    </small>
                  </label>
                ) : item.type === "object" || item.type === "array" ? (
                  <TextArea
                    key={item.key}
                    id={`parameter-${item.key}`}
                    labelText={`${item.key} · ${item.type}`}
                    value={String(parameters[item.key] ?? "")}
                    onChange={(event) =>
                      setParameters((current) => ({
                        ...current,
                        [item.key]: event.target.value,
                      }))
                    }
                    helperText={
                      item.description ||
                      `用于节点：${item.nodeTitle}；请输入合法 JSON`
                    }
                    required={item.required !== false}
                  />
                ) : (
                  <TextInput
                    key={item.key}
                    id={`parameter-${item.key}`}
                    labelText={`${item.key} · ${item.type}`}
                    type={
                      item.type === "integer" || item.type === "number"
                        ? "number"
                        : "text"
                    }
                    step={item.type === "number" ? "any" : undefined}
                    value={String(parameters[item.key] ?? "")}
                    onChange={(event) =>
                      setParameters((current) => ({
                        ...current,
                        [item.key]: event.target.value,
                      }))
                    }
                    helperText={
                      item.description || `用于节点：${item.nodeTitle}`
                    }
                    required={item.required !== false}
                  />
                ),
              )}
            </div>
          )}
        </section>
        <div className="form-actions">
          <Button kind="secondary" as={Link} to="/runs">
            取消
          </Button>
          <Button
            type="submit"
            disabled={busy || !knowledgeId || !selectedKnowledge}
          >
            {busy ? "正在生成" : "生成计划"}
          </Button>
        </div>
      </form>
    </main>
  );
}

function RunDetail() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<Run | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [artifact, setArtifact] = useState<Record<string, unknown> | null>(
    null,
  );
  const [error, setError] = useState("");
  const load = () =>
    api
      .run(runId)
      .then((value) => {
        setRun(value);
        setSelected(
          (current) =>
            current || value.currentNodeId || value.workflow.entryNodeId,
        );
      })
      .catch((value) => setError(value.message));
  useEffect(() => {
    load();
    if (run && ["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.status))
      return;
    const events = new EventSource(withBase(`/api/v1/runs/${runId}/events`), {
      withCredentials: true,
    });
    [
      "RUN_STARTED",
      "RUN_SUCCEEDED",
      "RUN_FAILED",
      "RUN_UNKNOWN",
      "NODE_STARTED",
      "NODE_SUCCEEDED",
      "NODE_FAILED",
      "NODE_SKIPPED",
      "INTERRUPT_OPENED",
      "INTERRUPT_RESOLVED",
      "CREDENTIAL_REQUIRED",
    ].forEach((type) => events.addEventListener(type, load));
    return () => events.close();
  }, [runId, run?.status]);
  useEffect(() => {
    setArtifact(null);
    if (selected && run?.nodeStatuses[selected] === "SUCCEEDED")
      api
        .artifact(runId, selected)
        .then((value) => setArtifact(value.data))
        .catch(() => {});
  }, [selected, run?.nodeStatuses?.[selected], runId]);
  if (error)
    return (
      <main className="page">
        <InlineNotification
          kind="error"
          title="无法加载运行"
          subtitle={error}
          hideCloseButton
        />
      </main>
    );
  if (!run)
    return (
      <main className="page">
        <InlineLoading description="加载运行状态" />
      </main>
    );
  const node = run.workflow.nodes.find((item) => item.id === selected);
  const done = run.progress.current ?? 0,
    total = run.progress.total ?? run.workflow.nodes.length;
  const action = async (name: "pause" | "resume" | "cancel") => {
    try {
      setRun(await api.action(runId, name));
    } catch (value) {
      setError((value as Error).message);
    }
  };
  const retryNode = async (
    nodeId: string,
    decision: "retry" | "mark_failed",
  ) => {
    setError("");
    try {
      setRun(await api.retryNode(runId, nodeId, decision));
    } catch (value) {
      setError((value as Error).message);
      throw value;
    }
  };
  return (
    <main className="run-page">
      <header className="run-header">
        <div>
          <Link to="/runs">返回执行中心</Link>
          <p className="overline">TICKET #{run.ticketId}</p>
          <h1>{run.knowledgeName}</h1>
          <div className="run-meta">
            <StatusTag status={run.status} />
            <span>{run.runId}</span>
            <span>发起人 {run.initiatedBy}</span>
          </div>
        </div>
        <div className="run-controls">
          <Button
            kind="ghost"
            disabled={!["RUNNING", "QUEUED"].includes(run.status)}
            onClick={() => action("pause")}
          >
            暂停
          </Button>
          <Button
            kind="secondary"
            disabled={run.status !== "PAUSED"}
            onClick={() => action("resume")}
          >
            继续
          </Button>
          <Button
            kind="danger--tertiary"
            disabled={["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.status)}
            onClick={() => action("cancel")}
          >
            取消运行
          </Button>
        </div>
      </header>
      <section className="progress-strip">
        <div>
          <strong>
            {done}/{total}
          </strong>
          <span>节点已结束</span>
        </div>
        <ProgressBar
          label="整体进度"
          value={total ? (done / total) * 100 : 0}
          hideLabel
        />
        <p>
          {run.waitingReason ||
            (run.status === "SUCCEEDED"
              ? "运行完成"
              : run.currentNodeId
                ? `当前节点 ${run.currentNodeId}`
                : "等待执行")}
        </p>
      </section>
      {run.status === "WAITING_PLAN_APPROVAL" && (
        <ApprovalBand run={run} onDone={setRun} />
      )}
      {run.status.startsWith("WAITING_") &&
        run.status !== "WAITING_PLAN_APPROVAL" && (
          <InterruptBand run={run} onDone={setRun} />
        )}
      <div className="run-workspace">
        <section className="canvas-panel">
          <WorkflowCanvas
            definition={run.workflow}
            statuses={run.nodeStatuses}
            onSelect={setSelected}
          />
          <div className="mobile-timeline">
            {run.workflow.nodes.map((item) => (
              <button key={item.id} onClick={() => setSelected(item.id)}>
                <StatusTag status={run.nodeStatuses[item.id]} />
                <span>
                  <b>{item.title}</b>
                  <small>{item.type}</small>
                </span>
              </button>
            ))}
          </div>
        </section>
        <aside className="node-drawer">
          {node ? (
            <NodeDetail
              key={node.id}
              node={node}
              status={run.nodeStatuses[node.id]}
              artifact={artifact}
              attempts={run.attempts.filter((item) => item.nodeId === node.id)}
              runId={runId}
              onRetry={(decision) => retryNode(node.id, decision)}
            />
          ) : (
            <p>选择节点查看详情</p>
          )}
        </aside>
      </div>
      <EventTimeline events={run.events || []} />
    </main>
  );
}

function ApprovalBand({
  run,
  onDone,
}: {
  run: Run;
  onDone: (run: Run) => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const approve = async () => {
    setBusy(true);
    try {
      onDone(await api.approve(run.runId, run.planHash));
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="decision-band">
      <div>
        <p className="overline">PLAN APPROVAL</p>
        <h2>确认完整执行计划</h2>
        <p>
          确认后进入执行队列。计划哈希：<code>{run.planHash}</code>
        </p>
        {error && <span className="error-text">{error}</span>}
      </div>
      <Button onClick={approve} disabled={busy}>
        {busy ? "正在提交" : "确认并开始"}
      </Button>
    </section>
  );
}

function InterruptBand({
  run,
  onDone,
}: {
  run: Run;
  onDone: (run: Run) => void;
}) {
  const open = (
    run as Run & {
      interrupts?: Array<{
        id?: string;
        interruptId?: string;
        kind: string;
        status: string;
        request: { fields?: string[] };
      }>;
    }
  ).interrupts?.find((item) => item.status === "OPEN");
  const [value, setValue] = useState("{}"),
    [error, setError] = useState("");
  if (!open)
    return (
      <section className="decision-band">
        <div>
          <h2>{run.waitingReason}</h2>
          <p>等待 Worker 写入可恢复的中断记录。</p>
        </div>
      </section>
    );
  const resume = async () => {
    try {
      if (open.kind === "CREDENTIAL") {
        onDone(await api.updateCredential(run.runId, value));
        setValue("");
        return;
      }
      const payload =
        open.kind === "NODE_APPROVAL"
          ? { decision: "approve" }
          : open.kind === "PAUSE"
            ? { action: "continue" }
            : { inputs: JSON.parse(value) };
      onDone(
        await api.resumeInterrupt(
          run.runId,
          open.interruptId || open.id || "",
          payload,
        ),
      );
    } catch (reason) {
      setError((reason as Error).message);
    }
  };
  return (
    <section className="decision-band">
      <div>
        <p className="overline">{open.kind}</p>
        <h2>{run.waitingReason}</h2>
        {open.kind === "HUMAN_INPUT" && (
          <TextArea
            id="resume-input"
            labelText="补充输入 JSON"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        )}
        {open.kind === "CREDENTIAL" && (
          <TextInput
            id="resume-credential"
            type="password"
            labelText="新的 AOPS API Key"
            value={value === "{}" ? "" : value}
            onChange={(event) => setValue(event.target.value)}
            autoComplete="off"
          />
        )}
        {error && <span className="error-text">{error}</span>}
      </div>
      <Button onClick={resume}>
        {open.kind === "NODE_APPROVAL"
          ? "批准节点"
          : open.kind === "CREDENTIAL"
            ? "更新凭据并继续"
            : "提交并继续"}
      </Button>
    </section>
  );
}

function NodeDetail({
  node,
  status,
  artifact,
  attempts,
  onRetry,
  runId,
}: {
  node: WorkflowNode;
  status: NodeStatus;
  artifact: Record<string, unknown> | null;
  attempts: Array<Record<string, unknown>>;
  onRetry?: (decision: "retry" | "mark_failed") => Promise<void>;
  runId?: string;
}) {
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState("");
  const [diagnostic, setDiagnostic] = useState<{ stdout: string; stderr: string; truncated: boolean } | null>(null);
  const [diagnosticError, setDiagnosticError] = useState("");
  const decide = async (decision: "retry" | "mark_failed") => {
    if (!onRetry) return;
    setRetrying(true);
    setRetryError("");
    try {
      await onRetry(decision);
    } catch (value) {
      setRetryError((value as Error).message);
    } finally {
      setRetrying(false);
    }
  };
  return (
    <div>
      <div className="drawer-head">
        <p className="overline">NODE DETAIL</p>
        <StatusTag status={status} />
      </div>
      <h2>{node.title}</h2>
      <dl>
        <div>
          <dt>节点 ID</dt>
          <dd>{node.id}</dd>
        </div>
        <div>
          <dt>类型</dt>
          <dd>{node.type}</dd>
        </div>
        <div>
          <dt>超时</dt>
          <dd>{node.timeoutSeconds}s</dd>
        </div>
        <div>
          <dt>审批</dt>
          <dd>{node.approvalPolicy}</dd>
        </div>
      </dl>
      {onRetry && (status === "FAILED" || status === "UNKNOWN") && (
        <section className="retry-panel">
          <b>{status === "UNKNOWN" ? "执行结果未知" : "节点执行失败"}</b>
          <p>
            {status === "UNKNOWN"
              ? "外部请求可能已经到达 AOPS，重新执行前请核对审计记录。"
              : "重新执行只增加本节点的一次尝试，已成功节点不会重复运行。"}
          </p>
          {retryError && <span className="error-text">{retryError}</span>}
          <div>
            <Button
              size="sm"
              disabled={retrying}
              onClick={() => decide("retry")}
            >
              {retrying ? "正在提交" : "重新执行此节点"}
            </Button>
            {status === "UNKNOWN" && (
              <Button
                size="sm"
                kind="danger--tertiary"
                disabled={retrying}
                onClick={() => decide("mark_failed")}
              >
                标记为失败
              </Button>
            )}
          </div>
        </section>
      )}
      {node.type === "sql_read" && (
        <>
          <h3>数据库</h3>
          <code className="code-block">{String(node.config.databaseRef)}</code>
          <h3>SQL 模板</h3>
          <pre>{String(node.config.sqlTemplate)}</pre>
        </>
      )}
      <h3>尝试记录</h3>
      {attempts.length ? (
        attempts.map((item, index) => (
          <div className="attempt" key={index}>
            <b>第 {String(item.attempt)} 次</b>
            <span>{String(item.status)}</span>
            <small>{String(item.errorMessage || item.errorCode || item.cliVersion || "")}</small>
            {item.exitCode !== undefined && item.exitCode !== null && <small>退出码 {String(item.exitCode)}</small>}
            {runId && Boolean(item.diagnosticAvailable) && <button type="button" className="diagnostic-link" onClick={() => { setDiagnosticError(""); api.diagnostic(runId, String(item.attemptId)).then((value) => setDiagnostic(value.data)).catch((value) => setDiagnosticError(value.message)); }}>查看诊断</button>}
          </div>
        ))
      ) : (
        <p className="muted">尚未执行</p>
      )}
      {diagnosticError && <p className="error-text">{diagnosticError}</p>}
      {diagnostic && <section className="diagnostic-panel"><header><b>脱敏诊断</b><button type="button" onClick={() => setDiagnostic(null)}>关闭</button></header>{diagnostic.truncated && <p>输出过长，已保留末尾内容。</p>}<h4>stderr</h4><pre>{diagnostic.stderr || "无 stderr 输出"}</pre><h4>stdout</h4><pre>{diagnostic.stdout || "无 stdout 输出"}</pre></section>}
      {artifact && (
        <>
          <h3>加密结果</h3>
          <pre>{JSON.stringify(artifact, null, 2)}</pre>
        </>
      )}
    </div>
  );
}

function EventTimeline({ events }: { events: WorkflowEvent[] }) {
  return (
    <section className="event-section">
      <header>
        <p className="overline">AUDIT TRAIL</p>
        <h2>事件时间线</h2>
      </header>
      {events.length ? (
        <ol>
          {[...events].reverse().map((event) => (
            <li key={event.eventId}>
              <time>{new Date(event.timestamp).toLocaleTimeString()}</time>
              <i />
              <div>
                <b>{event.type}</b>
                <span>{event.safeSummary}</span>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">暂无事件</p>
      )}
    </section>
  );
}

function KnowledgeList({ user }: { user: User }) {
  const query = useListQuery("status", "");
  const exactKey = JSON.stringify(query.exactFilters);
  const navigate = useNavigate();
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [data, setData] = useState<Paginated<KnowledgeSummary> | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date>();
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeSummary | null>(
    null,
  );
  const [deleting, setDeleting] = useState(false);
  const requestSequence = useRef(0);
  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setError("");
    try {
      const result = await api.knowledge({
        page: query.page,
        pageSize: query.pageSize,
        keyword: query.keyword,
          status: query.status || undefined,
          ...query.exactFilters,
      });
      if (sequence === requestSequence.current) {
        setData(result);
        setLastUpdated(new Date());
        if (result.page !== query.page) query.setPage(result.page);
      }
    } catch (value) {
      if (sequence === requestSequence.current)
        setError((value as Error).message);
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }, [query.page, query.pageSize, query.keyword, query.status, exactKey]);
  useEffect(() => {
    void load();
  }, [load]);
  const publish = async (id: string) => {
    try {
      await api.publishKnowledge(id);
      await load();
    } catch (value) {
      setError((value as Error).message);
    }
  };
  const submitReview = async (id: string) => {
    try {
      await api.submitKnowledgeReview(id);
      await load();
    } catch (value) {
      setError((value as Error).message);
    }
  };
  const remove = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.deleteKnowledge(deleteTarget.knowledgeId);
      setDeleteTarget(null);
      await load();
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setDeleting(false);
    }
  };
  return (
    <main className="page">
      <header className="page-title">
        <div>
          <p className="overline">VERSIONED KNOWLEDGE</p>
          <h1>知识与版本</h1>
          <p>每条经验都是可观察的节点流程；发布后生成不可变版本。</p>
        </div>
        <div className="header-actions">
          {user.isAdmin && selectedIds.length > 0 && <Button kind="tertiary" onClick={() => { sessionStorage.setItem("transferKnowledgeIds", JSON.stringify(selectedIds)); navigate("/transfers"); }}>导出所选 · {selectedIds.length}</Button>}
          {user.isOperator && (
            <Button kind="secondary" as={Link} to="/knowledge/extract">
              从工单提取
            </Button>
          )}
          {user.isAdmin && (
            <Button as={Link} to="/knowledge/new">
              手工编排
            </Button>
          )}
        </div>
      </header>
      <ListToolbar
        keyword={query.draftKeyword}
        placeholder="名称 / 摘要 / 工单 ID / 事件编号 / 创建人 UID"
        status={query.status}
        statusOptions={[
          { value: "", label: "全部状态" },
          { value: "DRAFT", label: "草稿" },
          { value: "PENDING_REVIEW", label: "待审核" },
          { value: "PUBLISHED", label: "已发布" },
        ]}
        loading={loading}
        lastUpdated={lastUpdated}
        onKeywordChange={query.setDraftKeyword}
        onKeywordCommit={query.commitKeyword}
        onStatusChange={query.setStatus}
        onRefresh={() => void load()}
        onClear={query.clear}
        exactValues={query.exactFilters}
        onExactChange={query.setExactFilter}
        exactFields={[
          { key: "knowledgeId", label: "知识 ID" },
          { key: "nameExact", label: "经验名称（精确）" },
          { key: "creatorUid", label: "创建人 UID" },
          ...(user.isAdmin ? [{ key: "authorizedUid", label: "授权 UID" }] : []),
          { key: "sourceTicketId", label: "来源工单 ID" },
          { key: "sourceTicketNo", label: "事件编号" },
          { key: "systemKey", label: "系统标识" },
          { key: "createdFrom", label: "创建时间起", type: "datetime-local" as const },
          { key: "createdTo", label: "创建时间止", type: "datetime-local" as const },
        ]}
      />
      {error && (
        <InlineNotification
          kind="error"
          title="知识操作失败"
          subtitle={error}
          hideCloseButton
        />
      )}
      {loading && data === null ? (
        <RunSkeleton />
      ) : data && data.items.length === 0 ? (
        <section className="empty-state">
          <span>K0</span>
          <h2>
            {query.keyword || query.status
              ? "当前条件没有匹配经验"
              : "还没有可执行经验"}
          </h2>
          <p>
            {query.keyword || query.status
              ? "调整关键词或状态，或者清空查询条件。"
              : "从已处理工单提取，或手工编排多个只读查询节点。"}
          </p>
          {query.keyword || query.status ? (
            <Button onClick={query.clear}>清空查询条件</Button>
          ) : (
            <Button as={Link} to="/knowledge/extract">
              从工单提取第一条经验
            </Button>
          )}
        </section>
      ) : data ? (
        <>
          <div className={`knowledge-grid ${loading ? "is-refreshing" : ""}`}>
            {data.items.map((item) => (
              <article key={item.knowledgeId}>
                <div>
                  {user.isAdmin && <input aria-label={`选择 ${item.name}`} type="checkbox" checked={selectedIds.includes(item.knowledgeId)} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...current, item.knowledgeId] : current.filter((id) => id !== item.knowledgeId))} />}
                  <StatusTag status={item.status} />
                  <span>
                    {item.sourceType === "TICKET_EXTRACTION"
                      ? `工单 #${item.sourceTicketId}`
                      : "手工编排"}
                  </span>
                </div>
                <Link
                  className="knowledge-link"
                  to={`/knowledge/${item.knowledgeId}`}
                >
                  <h2>{item.name}</h2>
                  <p>{item.summary}</p>
                </Link>
                <footer>
                  <span>
                    {item.nodeCount} 节点 · {item.visibility}
                    <br />创建 {new Date(item.createdAt).toLocaleString()}
                    <br />更新 {new Date(item.updatedAt).toLocaleString()}
                    {item.publishedAt && <><br />发布 {new Date(item.publishedAt).toLocaleString()}</>}
                  </span>
                  <div className="knowledge-actions">
                    {user.isAdmin && item.status === "PENDING_REVIEW" ? (
                      <Button
                        size="sm"
                        kind="tertiary"
                        onClick={() => publish(item.knowledgeId)}
                      >
                        审核并发布
                      </Button>
                    ) : item.status === "DRAFT" &&
                      (user.isAdmin || item.creatorUid === user.uid) ? (
                      <Button
                        size="sm"
                        kind="tertiary"
                        onClick={() => submitReview(item.knowledgeId)}
                      >
                        提交审核
                      </Button>
                    ) : item.status === "PENDING_REVIEW" ? (
                      <span>等待管理员审核</span>
                    ) : (
                      <code>{item.knowledgeId}</code>
                    )}
                    {(user.isAdmin ||
                      (item.creatorUid === user.uid &&
                        item.status === "DRAFT")) && (
                      <Button
                        size="sm"
                        kind="danger--ghost"
                        onClick={() => setDeleteTarget(item)}
                      >
                        删除
                      </Button>
                    )}
                  </div>
                </footer>
              </article>
            ))}
          </div>
          <ListPagination
            page={data.page}
            pageSize={data.pageSize}
            total={data.total}
            totalPages={data.totalPages}
            loading={loading}
            onPageChange={query.setPage}
            onPageSizeChange={query.setPageSize}
          />
        </>
      ) : null}
      <Modal
        open={Boolean(deleteTarget)}
        danger
        modalHeading="删除知识经验"
        primaryButtonText={deleting ? "正在删除" : "确认删除"}
        secondaryButtonText="取消"
        primaryButtonDisabled={deleting}
        onRequestClose={() => !deleting && setDeleteTarget(null)}
        onRequestSubmit={remove}
      >
        <p>
          将删除“{deleteTarget?.name}
          ”。它会立即从知识清单、审核队列和MCP匹配中消失；历史运行与不可变版本继续保留用于审计。
        </p>
      </Modal>
    </main>
  );
}

function NewKnowledge() {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    name: "",
    summary: "",
    phrases: "",
    negativePhrases: "",
    systems: "",
    uids: "",
  });
  const [workflow, setWorkflow] = useState<EditableWorkflow>(newWorkflow());
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const field =
    (key: keyof typeof form) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setForm({ ...form, [key]: event.target.value });
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await api.createKnowledge({
        name: form.name,
        summary: form.summary,
        matchPhrases: lines(form.phrases),
        negativePhrases: lines(form.negativePhrases),
        systemKeys: lines(form.systems),
        uids: lines(form.uids.replaceAll(",", "\n")),
        workflowDefinition: workflow,
      });
      navigate(`/knowledge/${created.knowledgeId}`);
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="page author-page">
      <header className="page-title">
        <div>
          <p className="overline">STRUCTURED DEFINITION</p>
          <h1>可视化编排经验</h1>
          <p>
            拖动节点、连接端口、点击卡片编辑；节点输入可绑定任意可达前置节点的结果。
          </p>
        </div>
      </header>
      <form onSubmit={submit}>
        {error && (
          <InlineNotification
            kind="error"
            title="无法创建经验"
            subtitle={error}
            hideCloseButton
          />
        )}
        <section className="definition-panel">
          <h2>适用范围</h2>
          <div className="definition-grid">
            <TextInput
              id="knowledge-name"
              labelText="经验名称"
              value={form.name}
              onChange={field("name")}
              required
            />
            <TextArea
              id="knowledge-summary"
              labelText="摘要"
              value={form.summary}
              onChange={field("summary")}
              required
            />
            <TextArea
              id="knowledge-phrases"
              labelText="匹配短语（每行一个）"
              value={form.phrases}
              onChange={field("phrases")}
              required
            />
            <TextArea
              id="knowledge-negative"
              labelText="排除短语（每行一个）"
              value={form.negativePhrases}
              onChange={field("negativePhrases")}
            />
            <TextArea
              id="knowledge-systems"
              labelText="系统标识（每行一个）"
              value={form.systems}
              onChange={field("systems")}
            />
            <TextArea
              id="knowledge-uids"
              labelText="授权 UID（逗号或换行；留空公开）"
              value={form.uids}
              onChange={field("uids")}
            />
          </div>
        </section>
        <WorkflowEditor definition={workflow} onChange={setWorkflow} />
        <div className="sticky-save">
          <span>
            {workflow.nodes.length} 个节点 · {workflow.edges.length}{" "}
            条连接，保存后进入草稿审核。
          </span>
          <div>
            <Button kind="secondary" as={Link} to="/knowledge">
              取消
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "正在保存" : "保存草稿"}
            </Button>
          </div>
        </div>
      </form>
    </main>
  );
}

function lines(value: string) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function StudioPage() {
  const [catalog, setCatalog] = useState<NodeCatalogItem[]>([]);
  const [digest, setDigest] = useState("");
  const [selected, setSelected] = useState<NodeCatalogItem | null>(null);
  const [mode, setMode] = useState("SIMULATION");
  const [nodeJson, setNodeJson] = useState("");
  const [inputsJson, setInputsJson] = useState("{}");
  const [fixtureJson, setFixtureJson] = useState("{}");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [manualValue, setManualValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selectNode = (item: NodeCatalogItem) => {
    const ui = item.uiSchema || {};
    const config = (ui.debugConfig as Record<string, unknown>) || {};
    const inputs = (ui.debugInputs as Record<string, unknown>) || {};
    const fixture = (ui.debugFixture as Record<string, unknown>) || {};
    setSelected(item);
    setNodeJson(JSON.stringify({ id: "debug-node", type: item.type, schemaVersion: item.schemaVersion, handlerVersion: item.handlerVersion, title: item.name, config, inputs: [], approvalPolicy: item.approvalPolicy, timeoutSeconds: 60 }, null, 2));
    setInputsJson(JSON.stringify(inputs, null, 2));
    setFixtureJson(JSON.stringify({ fixtureOutput: fixture, rule: ui.debugRule }, null, 2));
    setResult(null); setError("");
  };
  useEffect(() => { api.studioCatalog().then((value) => { setCatalog(value.nodes); setDigest(value.catalogDigest); const initial = value.nodes.find((item) => item.type === "sql_read") || value.nodes[0]; if (initial) selectNode(initial); }).catch((value) => setError(value.message)); }, []);
  const run = async () => {
    setBusy(true); setError("");
    try {
      const value = await api.studioDebugNode({ node: JSON.parse(nodeJson), inputs: JSON.parse(inputsJson), mode, simulation: JSON.parse(fixtureJson) });
      setResult(value);
    } catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const reply = async (response: Record<string, unknown>) => {
    if (!result?.debugRunId) return;
    setBusy(true); setError("");
    try { setResult(await api.studioDebugReply(String(result.debugRunId), response)); }
    catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const interrupt = result?.status === "WAITING_INPUT" ? result?.interrupt as Record<string, unknown> | undefined : undefined;
  const candidates = (interrupt?.candidates as Array<Record<string, unknown>> | undefined) || [];
  return <main className="page studio-page">
    <header className="page-title"><div><p className="overline">NODE REGISTRY / TEST ONLY</p><h1>节点Studio</h1><p>使用正式节点定义和Simulation Adapter进行单节点调试。结果仅写入临时SQLite，不影响生产运行。</p></div><code>{digest ? digest.slice(0, 12) : "loading"}</code></header>
    {error && <InlineNotification kind="error" title="Studio操作失败" subtitle={error} hideCloseButton />}
    <div className="studio-layout">
      <aside className="studio-catalog"><header><b>Node Catalog</b><span>{catalog.length} 类型</span></header>{catalog.map((item) => <button type="button" className={selected?.type === item.type ? "active" : ""} key={`${item.type}-${item.schemaVersion}`} onClick={() => selectNode(item)}><span>{item.category}</span><b>{item.name}</b><small>{item.type} · v{item.schemaVersion} · {item.handlerVersion}</small></button>)}</aside>
      <section className="studio-editor">
        {selected ? <><header><div><p className="overline">SINGLE NODE DEBUG</p><h2>{selected.name}</h2><p>{selected.description}</p></div><StatusTag status="READY" /></header>
          <label className="select-label">执行模式<select value={mode} onChange={(event) => setMode(event.target.value)}>{selected.supportedModes.filter((item) => item !== "PRODUCTION").map((item) => <option key={item}>{item}</option>)}</select></label>
          <div className="studio-json-grid"><TextArea id="studio-node" labelText="节点定义 JSON" rows={14} value={nodeJson} onChange={(event) => setNodeJson(event.target.value)} /><TextArea id="studio-inputs" labelText="节点输入 JSON" rows={14} value={inputsJson} onChange={(event) => setInputsJson(event.target.value)} /><TextArea id="studio-fixture" labelText="Simulation Adapter JSON" rows={14} value={fixtureJson} onChange={(event) => setFixtureJson(event.target.value)} /></div>
          <div className="studio-actions"><span>仅创建TEST_ONLY调试attempt</span><Button onClick={run} disabled={busy}>{busy ? "正在运行" : "运行单节点"}</Button></div>
        </> : <InlineLoading description="加载Node Catalog" />}
      </section>
      <aside className="studio-result"><header><b>调试结果</b>{result && <StatusTag status={String(result.status)} />}</header>{result ? <><dl><div><dt>Debug Run</dt><dd>{String(result.debugRunId)}</dd></div><div><dt>模式</dt><dd>{String(result.mode)}</dd></div><div><dt>Handler</dt><dd>{String(result.handlerVersion)}</dd></div></dl>{interrupt && <section className="studio-interrupt"><b>{String(interrupt.title || "请选择")}</b>{candidates.map((item) => <button type="button" key={String(item.id)} onClick={() => reply({ action: "SELECT", candidateId: item.id })}><strong>{String(item.label || item.id)}</strong><small>{String(item.value)}</small></button>)}<TextInput id="studio-manual" labelText="直接输入值" value={manualValue} onChange={(event) => setManualValue(event.target.value)} /><div><Button size="sm" onClick={() => reply({ action: "MANUAL_VALUE", value: manualValue })} disabled={!manualValue}>使用该值</Button><Button size="sm" kind="danger--ghost" onClick={() => reply({ action: "CANCEL" })}>取消</Button></div></section>}<h3>输出</h3><pre>{JSON.stringify(result.output, null, 2)}</pre></> : <div className="inspector-empty"><span>01</span><h3>等待调试</h3><p>选择节点并运行后，这里展示标准输出、HITL和诊断。</p></div>}</aside>
    </div>
  </main>;
}

type TransferPreview = {
  sourceEnvironment: string;
  packageId?: string;
  knowledgeCount?: number;
  itemCount?: number;
  exportable?: boolean;
  blockers?: string[];
  warnings?: string[];
  databasePaths: Array<{ sourceRef: string; targetRef: string; usageCount: number }>;
  items: Array<{ itemIndex?: number; knowledgeId?: string; name: string; summary: string; creatorUid?: string; uids?: string[]; action?: string }>;
};

function TransferCenter() {
  const initialIds = (() => { try { return JSON.parse(sessionStorage.getItem("transferKnowledgeIds") || "[]") as string[]; } catch { return []; } })();
  const [knowledgeIds, setKnowledgeIds] = useState(initialIds.join("\n"));
  const [exportData, setExportData] = useState<TransferPreview | null>(null);
  const [exportMappings, setExportMappings] = useState<Array<{ sourceRef: string; targetRef: string }>>([]);
  const [importPackageData, setImportPackageData] = useState<Record<string, unknown> | null>(null);
  const [importData, setImportData] = useState<TransferPreview | null>(null);
  const [importMappings, setImportMappings] = useState<Array<{ sourceRef: string; targetRef: string }>>([]);
  const [replaceData, setReplaceData] = useState<{ affectedCount: number } | null>(null);
  const [overrides, setOverrides] = useState<Array<{ itemIndex: number; creatorUid: string; uids: string[]; action: string }>>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const exportPreview = async () => {
    setBusy(true); setError(""); setMessage(""); setConfirmed(false);
    try {
      const value = await api.exportPreview(lines(knowledgeIds)) as unknown as TransferPreview;
      setExportData(value);
      setExportMappings(value.databasePaths.map((item) => ({ sourceRef: item.sourceRef, targetRef: item.targetRef })));
    } catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const download = async () => {
    if (!confirmed) return;
    setBusy(true); setError("");
    try {
      const value = await api.exportPackage({ knowledgeIds: lines(knowledgeIds), confirmed: true, databaseMappings: exportMappings });
      const filename = `itsm-workflow-${String(value.packageId || "export")}.json`;
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url);
      setMessage(`已生成 ${filename}`);
    } catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const previewReplace = async () => {
    setBusy(true); setError(""); setConfirmed(false);
    try { setReplaceData(await api.replacePreview({ knowledgeIds: lines(knowledgeIds), databaseMappings: exportMappings }) as unknown as { affectedCount: number }); }
    catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const replaceNow = async () => {
    if (!confirmed) return;
    setBusy(true); setError("");
    try { const value = await api.replacePaths({ knowledgeIds: lines(knowledgeIds), databaseMappings: exportMappings, confirmed: true }); setMessage(`已替换 ${(value.updatedKnowledgeIds as string[]).length} 条经验的数据库路径。`); setReplaceData(null); }
    catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  const readPackage = async (file?: File) => {
    if (!file) return;
    setBusy(true); setError(""); setMessage(""); setConfirmed(false);
    try {
      if (file.size > 10 * 1024 * 1024) throw new Error("导入包超过10 MiB");
      const packageValue = JSON.parse(await file.text()) as Record<string, unknown>;
      const value = await api.importPreview({ package: packageValue }) as unknown as TransferPreview;
      setImportPackageData(packageValue); setImportData(value);
      setImportMappings(value.databasePaths.map((item) => ({ sourceRef: item.sourceRef, targetRef: item.targetRef })));
      setOverrides(value.items.map((item, index) => ({ itemIndex: item.itemIndex ?? index, creatorUid: item.creatorUid || "", uids: item.uids || [], action: item.action || "CREATE" })));
    } catch (value) { setError(`导入预检失败：${(value as Error).message}`); } finally { setBusy(false); }
  };
  const importNow = async () => {
    if (!confirmed || !importPackageData) return;
    setBusy(true); setError("");
    try {
      const value = await api.importPackage({ package: importPackageData, confirmed: true, databaseMappings: importMappings, knowledgeOverrides: overrides });
      setMessage(`导入完成：创建 ${(value.createdKnowledgeIds as string[]).length} 条待审核经验，跳过 ${(value.skippedItemIndexes as number[]).length} 条。`);
    } catch (value) { setError((value as Error).message); } finally { setBusy(false); }
  };
  return <main className="page transfer-page">
    <header className="page-title"><div><p className="overline">PORTABLE WORKFLOW</p><h1>导入导出</h1><p>迁移结构化 DAG；路径核实和人工确认完成前不会写入生产知识。</p></div></header>
    {error && <InlineNotification kind="error" title="迁移操作失败" subtitle={error} hideCloseButton />}
    {message && <InlineNotification kind="success" title="迁移操作完成" subtitle={message} hideCloseButton />}
    <div className="transfer-grid">
      <section className="transfer-panel"><p className="overline">EXPORT</p><h2>导出经验</h2><p>每行一个知识 ID。已发布经验导出当前不可变版本。</p><TextArea id="transfer-ids" labelText="知识 ID" rows={6} value={knowledgeIds} onChange={(event) => setKnowledgeIds(event.target.value)} /><Button onClick={exportPreview} disabled={busy || !lines(knowledgeIds).length}>{busy ? "正在预检" : "预检并汇总路径"}</Button>
        {exportData && <div className="transfer-preview"><strong>{exportData.knowledgeCount} 条经验 · {exportData.databasePaths.length} 条去重路径</strong>{exportData.blockers?.map((item) => <p className="error-text" key={item}>{item}</p>)}{exportMappings.map((item, index) => <div className="mapping-row" key={item.sourceRef}><code>{item.sourceRef}</code><span>→</span><TextInput id={`export-map-${index}`} labelText="目标数据库路径" hideLabel value={item.targetRef} onChange={(event) => setExportMappings((current) => current.map((entry, position) => position === index ? { ...entry, targetRef: event.target.value } : entry))} /></div>)}<label className="confirm-row"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />我已核实目标环境数据库路径</label><div className="transfer-actions"><Button onClick={download} disabled={busy || !confirmed || exportData.exportable === false}>下载 JSON 包</Button><Button kind="danger--tertiary" onClick={previewReplace} disabled={busy}>预检源环境路径替换</Button></div>{replaceData && <div className="replace-confirm"><p>将直接修改 {replaceData.affectedCount} 条当前环境经验；已发布经验会退回待审核。</p><Button kind="danger" onClick={replaceNow} disabled={!confirmed || busy}>确认快速替换</Button></div>}</div>}
      </section>
      <section className="transfer-panel"><p className="overline">IMPORT</p><h2>导入经验</h2><p>支持原生 v2 DAG包和旧版 v1线性步骤包，导入后一律进入待审核。</p><label className="file-drop">选择 JSON 文件<input type="file" accept="application/json,.json" onChange={(event) => void readPackage(event.target.files?.[0])} /></label>
        {importData && <div className="transfer-preview"><strong>{importData.itemCount} 条经验 · 来源 {importData.sourceEnvironment}</strong>{importData.warnings?.map((item) => <p className="warning-text" key={item}>{item}</p>)}{importMappings.map((item, index) => <div className="mapping-row" key={item.sourceRef}><code>{item.sourceRef}</code><span>→</span><TextInput id={`import-map-${index}`} labelText="生产数据库路径" hideLabel value={item.targetRef} onChange={(event) => setImportMappings((current) => current.map((entry, position) => position === index ? { ...entry, targetRef: event.target.value } : entry))} /></div>)}<div className="override-list">{importData.items.map((item, index) => <article key={index}><b>{item.name}</b><TextInput id={`creator-${index}`} labelText="创建人 UID" value={overrides[index]?.creatorUid || ""} onChange={(event) => setOverrides((current) => current.map((entry, position) => position === index ? { ...entry, creatorUid: event.target.value } : entry))} /><TextInput id={`uids-${index}`} labelText="授权 UID（逗号分隔，留空公开）" value={(overrides[index]?.uids || []).join(",")} onChange={(event) => setOverrides((current) => current.map((entry, position) => position === index ? { ...entry, uids: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) } : entry))} /><label>处理方式<select value={overrides[index]?.action || "CREATE"} onChange={(event) => setOverrides((current) => current.map((entry, position) => position === index ? { ...entry, action: event.target.value } : entry))}><option value="CREATE">创建</option><option value="SKIP">跳过</option><option value="COPY">强制复制</option></select></label></article>)}</div><label className="confirm-row"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />确认以待审核状态写入当前环境</label><Button onClick={importNow} disabled={busy || !confirmed}>确认导入</Button></div>}
      </section>
    </div>
  </main>;
}

function ExtractKnowledge() {
  const [ticketId, setTicketId] = useState(""),
    [uids, setUids] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Awaited<
    ReturnType<typeof api.extractKnowledge>
  > | null>(null);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setResult(null);
    try {
      setResult(
        await api.extractKnowledge({
          ticketId: Number(ticketId),
          uids: lines(uids.replaceAll(",", "\n")),
        }),
      );
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="page narrow">
      <header className="page-title">
        <div>
          <p className="overline">TICKET TO WORKFLOW</p>
          <h1>从工单提取草稿</h1>
          <p>
            服务使用当前登录人的 AOPS API Key 调用系统
            aops-cli，读取工单详情与审计记录。程序先过滤失败操作，LLM
            再生成中文语义。
          </p>
        </div>
      </header>
      <form className="form-stack" onSubmit={submit}>
        {error && (
          <InlineNotification
            kind="error"
            title="提取失败"
            subtitle={error}
            hideCloseButton
          />
        )}
        <TextInput
          id="extract-ticket"
          labelText="工单 ID"
          type="number"
          min="1"
          value={ticketId}
          onChange={(event) => setTicketId(event.target.value)}
          required
        />
        <TextArea
          id="extract-uids"
          labelText="授权 UID（逗号或换行；留空表示公开）"
          value={uids}
          onChange={(event) => setUids(event.target.value)}
          helperText="创建人只用于审计，不自动获得私有经验权限。"
        />
        <div className="evidence-note">
          <b>固定提取边界</b>
          <span>
            仅接受 operation=sql_exec_read、result
            明确成功、数据库路径完整且通过只读 SQL 校验的记录。
          </span>
        </div>
        <div className="form-actions">
          <Button kind="secondary" as={Link} to="/knowledge">
            取消
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "正在读取与分析" : "生成经验草稿"}
          </Button>
        </div>
      </form>
      {result && (
        <section className="extraction-result">
          <p className="overline">DRAFT CREATED</p>
          <h2>{result.knowledge.name}</h2>
          <p>{result.knowledge.summary}</p>
          <div>
            <span>
              <b>{result.extraction.auditOperationCount}</b>审计操作
            </span>
            <span>
              <b>{result.extraction.acceptedOperationCount}</b>有效步骤
            </span>
            <span>
              <b>{result.extraction.ignoredOperationCount}</b>已过滤
            </span>
          </div>
          {result.extraction.ignoredOperations.length > 0 && (
            <details>
              <summary>查看过滤原因</summary>
              <pre>
                {JSON.stringify(result.extraction.ignoredOperations, null, 2)}
              </pre>
            </details>
          )}
          <Button as={Link} to={`/knowledge/${result.knowledge.knowledgeId}`}>
            审核流程草稿
          </Button>
        </section>
      )}
    </main>
  );
}

function KnowledgeDetail({ user }: { user: User }) {
  const { knowledgeId = "" } = useParams(),
    [knowledge, setKnowledge] = useState<Knowledge | null>(null),
    [selected, setSelected] = useState(""),
    [versions, setVersions] = useState<WorkflowVersionSummary[]>([]),
    [error, setError] = useState("");
  useEffect(() => {
    api
      .knowledgeById(knowledgeId)
      .then((value) => {
        setKnowledge(value);
        setSelected(value.workflowDefinition.entryNodeId);
      })
      .catch((value) => setError(value.message));
  }, [knowledgeId]);
  useEffect(() => { api.versions({ knowledgeId, pageSize: 100 }).then((value) => setVersions(value.items)).catch(() => setVersions([])); }, [knowledgeId]);
  if (error)
    return (
      <main className="page">
        <InlineNotification
          kind="error"
          title="无法加载经验"
          subtitle={error}
          hideCloseButton
        />
      </main>
    );
  if (!knowledge)
    return (
      <main className="page">
        <InlineLoading description="加载经验流程" />
      </main>
    );
  const node = knowledge.workflowDefinition.nodes.find(
    (item) => item.id === selected,
  );
  const authorable = knowledge.workflowDefinition.nodes.every((item) =>
    ["sql_read", "condition", "end"].includes(item.type),
  );
  const ownsDraft = knowledge.creatorUid === user.uid;
  const canEdit =
    authorable && (user.isAdmin || (ownsDraft && knowledge.status === "DRAFT"));
  const submitReview = async () => {
    try {
      setKnowledge(await api.submitKnowledgeReview(knowledge.knowledgeId));
    } catch (value) {
      setError((value as Error).message);
    }
  };
  const publish = async () => {
    try {
      await api.publishKnowledge(knowledge.knowledgeId);
      setKnowledge(await api.knowledgeById(knowledge.knowledgeId));
    } catch (value) {
      setError((value as Error).message);
    }
  };
  return (
    <main className="page knowledge-detail">
      <header className="page-title">
        <div>
          <Link to="/knowledge">返回知识清单</Link>
          <p className="overline">{knowledge.sourceType}</p>
          <h1>{knowledge.name}</h1>
          <p>{knowledge.summary}</p>
        </div>
        <div className="header-actions">
          <StatusTag status={knowledge.status} />
          {canEdit && (
            <Button
              kind="secondary"
              as={Link}
              to={`/knowledge/${knowledge.knowledgeId}/edit`}
            >
              {knowledge.status === "PUBLISHED" ? "创建新草稿版本" : "编辑草稿"}
            </Button>
          )}
        </div>
      </header>
      <section className="knowledge-facts">
        <span>
          <b>{knowledge.workflowDefinition.nodes.length}</b>节点
        </span>
        <span>
          <b>{knowledge.workflowDefinition.edges.length}</b>连接
        </span>
        <span>
          <b>{knowledge.visibility}</b>可见范围
        </span>
        <span>
          <b>
            {knowledge.sourceTicketId ? `#${knowledge.sourceTicketId}` : "—"}
          </b>
          来源工单
        </span>
      </section>
      <div className="run-workspace knowledge-graph">
        <section className="canvas-panel">
          <WorkflowCanvas
            definition={knowledge.workflowDefinition}
            statuses={Object.fromEntries(
              knowledge.workflowDefinition.nodes.map((item) => [
                item.id,
                "PENDING" as NodeStatus,
              ]),
            )}
            onSelect={setSelected}
          />
        </section>
        <aside className="node-drawer">
          {node && (
            <NodeDetail
              node={node}
              status="PENDING"
              artifact={null}
              attempts={[]}
            />
          )}
        </aside>
      </div>
      <section className="scope-panel">
        <div>
          <h2>匹配与权限</h2>
          <p>这些字段参与知识召回，不进入执行命令。</p>
        </div>
        <dl>
          <div>
            <dt>匹配短语</dt>
            <dd>{knowledge.matchPhrases.join("、")}</dd>
          </div>
          <div>
            <dt>排除短语</dt>
            <dd>{knowledge.negativePhrases.join("、") || "无"}</dd>
          </div>
          <div>
            <dt>系统范围</dt>
            <dd>{knowledge.systemKeys.join("、") || "不限"}</dd>
          </div>
          <div>
            <dt>授权 UID</dt>
            <dd>{knowledge.uids.join("、") || "所有已认证用户"}</dd>
          </div>
        </dl>
      </section>
      <section className="lifecycle-panel">
        <header><div><p className="overline">LIFECYCLE</p><h2>知识生命周期</h2></div><span>{knowledge.lifecycle?.length || 0} 条记录</span></header>
        <ol>{knowledge.lifecycle?.map((item) => <li key={item.eventId}><time>{new Date(item.createdAt).toLocaleString()}</time><div><b>{item.eventType}</b><span>{item.summary}</span><small>{item.actorUid || "系统迁移"}{item.workflowVersionId ? ` · ${item.workflowVersionId}` : ""}</small></div></li>)}</ol>
        {!knowledge.lifecycle?.length && <p className="muted">暂无生命周期记录</p>}
      </section>
      <section className="version-panel"><header><div><p className="overline">IMMUTABLE VERSIONS</p><h2>发布版本</h2></div><span>{versions.length} 个版本</span></header>{versions.length ? <div className="version-table">{versions.map((item) => <article key={item.workflowVersionId}><b>v{item.versionNumber}{item.current ? " · 当前" : ""}</b><code>{item.workflowVersionId}</code><span>{item.publishedBy}</span><time>{new Date(item.publishedAt).toLocaleString()}</time><small>{item.contentHash}</small></article>)}</div> : <p className="muted">尚未发布版本</p>}</section>
      {knowledge.status === "DRAFT" && (user.isAdmin || ownsDraft) && (
        <div className="sticky-save">
          <span>提交后草稿进入管理员审核队列，发布前不会参与匹配。</span>
          <Button onClick={submitReview}>提交管理员审核</Button>
        </div>
      )}
      {user.isAdmin && knowledge.status === "PENDING_REVIEW" && (
        <div className="sticky-save">
          <span>审核通过后将创建不可变版本并重建检索向量。</span>
          <Button onClick={publish}>审核通过并发布</Button>
        </div>
      )}
    </main>
  );
}

function EditKnowledge() {
  const { knowledgeId = "" } = useParams(),
    navigate = useNavigate(),
    [knowledge, setKnowledge] = useState<Knowledge | null>(null),
    [workflow, setWorkflow] = useState<EditableWorkflow>(newWorkflow()),
    [form, setForm] = useState({
      name: "",
      summary: "",
      phrases: "",
      negativePhrases: "",
      systems: "",
      uids: "",
    }),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    api
      .knowledgeById(knowledgeId)
      .then((value) => {
        setWorkflow(value.workflowDefinition);
        setKnowledge(value);
        setForm({
          name: value.name,
          summary: value.summary,
          phrases: value.matchPhrases.join("\n"),
          negativePhrases: value.negativePhrases.join("\n"),
          systems: value.systemKeys.join("\n"),
          uids: value.uids.join("\n"),
        });
      })
      .catch((value) => setError(value.message));
  }, [knowledgeId]);
  if (!knowledge)
    return (
      <main className="page">
        {error ? (
          <InlineNotification
            kind="error"
            title="无法加载经验"
            subtitle={error}
            hideCloseButton
          />
        ) : (
          <InlineLoading description="加载草稿" />
        )}
      </main>
    );
  const field =
    (key: keyof typeof form) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setForm({ ...form, [key]: event.target.value });
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.updateKnowledge(knowledgeId, {
        name: form.name,
        summary: form.summary,
        matchPhrases: lines(form.phrases),
        negativePhrases: lines(form.negativePhrases),
        systemKeys: lines(form.systems),
        uids: lines(form.uids.replaceAll(",", "\n")),
        workflowDefinition: workflow,
      });
      navigate(`/knowledge/${knowledgeId}`);
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="page author-page">
      <header className="page-title">
        <div>
          <p className="overline">DRAFT REVIEW</p>
          <h1>审核并修正流程</h1>
          <p>
            直接在画布中调整节点、条件和连线；保存已发布经验时会创建新的草稿版本，历史版本和已有运行不受影响。
          </p>
        </div>
      </header>
      <form onSubmit={save}>
        {error && (
          <InlineNotification
            kind="error"
            title="无法保存草稿"
            subtitle={error}
            hideCloseButton
          />
        )}
        <section className="definition-panel">
          <h2>适用范围</h2>
          <div className="definition-grid">
            <TextInput
              id="edit-name"
              labelText="经验名称"
              value={form.name}
              onChange={field("name")}
              required
            />
            <TextArea
              id="edit-summary"
              labelText="摘要"
              value={form.summary}
              onChange={field("summary")}
              required
            />
            <TextArea
              id="edit-phrases"
              labelText="匹配短语（每行一个）"
              value={form.phrases}
              onChange={field("phrases")}
              required
            />
            <TextArea
              id="edit-negative"
              labelText="排除短语（每行一个）"
              value={form.negativePhrases}
              onChange={field("negativePhrases")}
            />
            <TextArea
              id="edit-systems"
              labelText="系统标识（每行一个）"
              value={form.systems}
              onChange={field("systems")}
            />
            <TextArea
              id="edit-uids"
              labelText="授权 UID（留空公开）"
              value={form.uids}
              onChange={field("uids")}
            />
          </div>
        </section>
        <WorkflowEditor definition={workflow} onChange={setWorkflow} />
        <div className="sticky-save">
          <span>保存后请在详情页再次核对并发布。</span>
          <div>
            <Button kind="secondary" as={Link} to={`/knowledge/${knowledgeId}`}>
              取消
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "正在保存" : "保存修改"}
            </Button>
          </div>
        </div>
      </form>
    </main>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null));
  }, []);
  if (user === undefined)
    return (
      <main className="boot">
        <InlineLoading description="正在恢复会话" />
      </main>
    );
  if (!user) return <Login onLogin={setUser} />;
  return (
    <Shell
      user={user}
      onLogout={async () => {
        await api.logout();
        setUser(null);
      }}
    />
  );
}
