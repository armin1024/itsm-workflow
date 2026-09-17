# 架构与恢复语义

## 编排模型

流程画布保存节点位置、控制边和结构化配置，但不允许携带 Python代码或任意 shell命令。`sql_read`、`condition`、`human_input`、`approval`、`end` 均来自节点注册表；新增操作类型通过 Handler、配置 Schema、输入 Schema、输出 Schema和风险级别扩展，不修改调度核心。

控制依赖和数据依赖分离：边决定节点何时可运行；`NODE_OUTPUT` 输入通过来源节点 ID和 JSON Pointer取值。发布校验会拒绝环、不可达节点、非法条件、无默认分支，以及引用非前置节点的数据绑定。

`aops-cli db read` 的 stdout 是 SSE事件流。执行适配器以 `event:done` 和 `data:!ok` 判定成功，将 `title` 与每个 `message` 数组合并为对象行。例如标题 `user_id` 和消息 `000244` 会形成 `data[0].user_id`，后续节点可以使用 `/data/0/user_id` 绑定。

## 组件

```text
Browser / REST client
        │ AOPS identity + service token
        ▼
itsm-workflow-api ───── SSE ─────► run cards
        │
        ▼
PostgreSQL
  knowledge / lifecycle / immutable versions / runs / events
  transfer provenance / transfer audits
  encrypted credentials / encrypted artifacts
  LangGraph encrypted checkpoints
        ▲
        │ lease + checkpoint
itsm-workflow-worker
        │ argv + minimal environment
        ▼
system aops-cli ─────► AOPS
```

API之前已有独立无状态 MCP Adapter。Adapter只转换 MCP参数和 REST响应，不直接访问 checkpoint、不领取队列任务，也不执行 `aops-cli`。详细约定见 [MCP 与 Agent 接入指南](mcp-agent-integration.md)。

API和 Worker是独立 systemd进程。Worker通过 PostgreSQL领取 `QUEUED` 运行并维护租约；Worker重启后从 LangGraph checkpoint继续。

## 外部操作边界

LangGraph能恢复图状态，但不能保证外部 CLI 调用 exactly-once。Worker在启动 CLI 前写入 `STARTED` attempt；如果租约过期时仍有 STARTED attempt，运行进入 `UNKNOWN`，不会自动重放。操作员必须明确选择重新执行或标记失败。

## 可观测与控制通道

- Web页面通过持久化 SSE事件流展示节点开始、成功、失败、跳过、中断和恢复。
- `Last-Event-ID` 用于断线续传，页面刷新不会丢失进度。
- 暂停在节点安全边界生效；取消可以终止当前 CLI进程组，但不承诺撤销已经到达 AOPS 的请求。
- Agent接入应使用短事件等待而非永久阻塞的 MCP调用，使用户指令能及时进入下一轮。
- Web控制与 Agent控制使用相同 REST状态机，因此用户可在 Agent等待期间直接从执行详情页暂停或取消。
- CLI失败会从 SSE错误事件、JSON错误字段或 stderr中提炼安全摘要；脱敏后的 stdout/stderr作为独立加密诊断 artifact保存，不进入普通列表或 MCP状态快照。

## Checkpoint和敏感数据

- 运行状态不包含明文 API Key，只保存 credential引用。
- 节点结果单独 AES-GCM加密并通过 artifact引用进入图状态。
- PostgreSQL checkpointer使用 `EncryptedSerializer`。
- 启用 `LANGGRAPH_STRICT_MSGPACK=true`。
- 完整运行输入、artifact和 checkpoint默认保留30天；审计元数据长期保留。

## 知识生命周期与跨环境迁移

- `knowledge`保存当前工作草稿和最近发布信息，`workflow_versions`保存不可变发布快照，`knowledge_lifecycle_events`保存创建、修改、提交、退回、发布、导入、路径替换和删除事件。
- 原生迁移包只包含结构化知识定义和 DAG，不包含向量、凭据、运行结果或 checkpoint。
- 导入始终创建待审核知识，不覆盖生产现有知识；规范化后的 `effectiveContentHash`用于重复检测，包内来源哈希只承担变更提示，不是数字签名。
- 数据库路径在导出和导入预检中按完整字符串去重映射；修改已发布经验路径会退回待审核并生成生命周期和迁移审计。

## 扩展节点

节点注册表位于 `app/node_types.py`。增加操作类型时必须新增 Manifest和 Handler，实现输入输出 Schema、风险级别、校验、准备、执行、摘要和未知结果协调。数据库定义不能包含 Python代码或任意命令模板。

通用节点、控制台模板、Capability安全边界和未来节点市场设计见 [通用节点与卡片扩展平台设计](node-extension-platform.md)。
