# 精简后架构

## 边界

```mermaid
flowchart LR
    U[开发或测试人员] --> S[独立Studio]
    S --> R[Node Registry]
    S --> C[Workflow Compiler]
    S --> E[Node Executor]
    E --> CLI[aops-cli]
    CLI --> A[AOPS]
    C --> L[内网LLM]
    E --> L
    S --> DB[(TEST_ONLY SQLite)]

    T[tec01控制面] -->|Catalog/Validate/Plan| API[Runtime API]
    CW[Compiler Worker] -->|主动claim| T
    CW --> C
```

itsm-workflow拥有节点定义、草稿编译、DAG校验、计划渲染、节点Handler和外部系统适配器。tec01拥有生产知识、MCP、权限、状态机、队列、Artifact、Checkpoint和审计。

## 保留模块

| 模块 | 路径 | 职责 |
|---|---|---|
| Node Registry | `app/runtime/registry.py`、`builtin_nodes.py` | 节点Manifest、Schema、版本与能力目录 |
| Node Executor | `app/runtime/executor.py` | 单节点统一执行入口，Studio和后续Remote Executor共用 |
| Compiler | `app/extraction.py` | 审计过滤、SQL参数化、LLM中文提炼、DAG生成 |
| Planner | `app/runtime/planner.py` | DAG归一化、校验、计划材料和内容哈希 |
| CLI Adapter | `app/cli.py` | 安全argv执行、SSE解析、超时、限流和诊断脱敏 |
| tec01 Adapter | `app/tec01_client.py`、`compiler_worker.py` | 主动claim、租约和编译结果回写 |
| Studio | `app/studio`、`frontend` | TEST_ONLY编排和调试 |

## 已移除模块

本仓库不再包含本地生产知识、全文/向量检索、MCP Server、用户登录、管理员权限、生产运行中心、PostgreSQL模型、Alembic迁移、旧生产Worker、导入导出和生产Artifact存储。

SQLite仅保存临时workspace与调试输出，默认24小时清理；它不是tec01的副本，也不能承载生产恢复。
