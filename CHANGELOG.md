# 版本说明

## 0.8.0

- 将服务收敛为Node Registry、Compiler、Runtime Adapter、tec01 Client和独立Studio。
- 删除本地生产知识/检索/运行数据库、MCP、登录权限、Alembic和旧Worker。
- Studio无需管理员登录，提供节点单步调试、可视化DAG校验和工单草稿提取。
- SQL读TEST模式按请求接收`X-AOPS-Api-Key`，凭据不落盘、不进入调试记录。
- 安装包只保留API与可选Compiler Worker，并在升级时停用旧MCP/Worker/Migrate unit。

## 0.7.0

- 增加统一Node Registry、完整Manifest、节点Schema、执行模式和Catalog摘要。
- 增加Runtime内部Catalog、Workflow校验、计划渲染和Compiler预览API。
- 增加独立Studio页面、TEST_ONLY SQLite、TTL临时数据和单节点调试。
- 增加`llm_extract`与`hitl_select`节点、结构化输出、候选选择和受控REFINEMENT回边。
- 增加tec01版本化HTTP客户端和无存储Compiler Worker。
- 保留Monolith兼容模式；未启用tec01时现有知识、MCP和Worker行为不变。

## 0.6.3

- 发布流程显式排除 `._*`、`.DS_Store`、`.AppleDouble` 和 `__MACOSX`，关闭 tar xattr、ACL和 SELinux扩展元数据。
- 安装器在 `/opt/itsm-workflow` 再次清理 AppleDouble文件，防止 Alembic误加载 `._*.py`。
- 发布验收会扫描 macOS元数据；发现污染时直接阻止产包。
- 导入包使用规范化后的 `effectiveContentHash`做重复检测；来源哈希不一致只产生可见告警，不再阻止经人工核实的 JSON包导入。

## 0.6.2

- 修复导出 JSON包重新导入时报 `items[0]内容哈希校验失败`的问题。
- 预检响应增加 `sourceContentHash`、`effectiveContentHash` 和 `contentHashMismatch`。
- 保留人工修改路径、授权UID和通用描述的能力。

## 0.6.1

- 将 Alembic revision缩短为 `0007_diagnostics_transfer`，适配默认 `alembic_version.version_num VARCHAR(32)`。
- 增加 revision长度自动测试，并通过 PostgreSQL 17真实迁移验证。

## 0.6.0

- 增加节点失败诊断、字段级精确检索、知识生命周期、版本清单和原生 DAG跨环境导入导出。
- 增加数据库路径去重映射、快速替换、导入重复识别和迁移审计。
- 此版本的 `0007` revision长度不兼容 PostgreSQL默认 Alembic版本表，不应继续部署，请直接升级到 `0.6.3`。

## 兼容性

- 目标系统：Linux x86_64、glibc 2.17及以上、systemd。
- 持久化：itsm-workflow只使用TEST_ONLY SQLite；生产数据和运行状态由tec01持有。
- `aops-cli`由目标机提供，不包含在离线包中。
- 旧 `aops-workflow-knowledge-export/schemaVersion=1` 知识包仍可导入；新包使用 `itsm-workflow-export/schemaVersion=2`。
