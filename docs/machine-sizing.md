# 机器容量与参数配置（0.13.0）

以下是保守起始配置，适用Linux x86_64、LLM远程部署、VM资源主要供本服务使用。不表示已承诺固定吞吐量。共享VM按可用资源选择较低档，1核2GiB仅建议开发验证。

## 1. 四档参数

| 环境变量 | 2核4GiB | 4核8GiB，默认 | 8核16GiB | 16核32GiB |
|---|---:|---:|---:|---:|
| EXECUTOR_MAX_ACTIVE_RUNS | 4 | 8 | 16 | 32 |
| EXECUTOR_MAX_ACTIVE_CLI | 1 | 2 | 4 | 8 |
| COMPILER_MAX_ACTIVE_JOBS | 1 | 1 | 2 | 4 |
| CPU_MAX_WORKERS | 1 | 1 | 2 | 2 |
| RUNTIME_DATA_BUDGET_MB | 1024 | 2048 | 4096 | 8192 |
| COMPILER_CACHE_MAX_MB | 32 | 64 | 128 | 256 |
| TEC01_HTTP_MAX_CONNECTIONS | 8 | 16 | 32 | 64 |
| TEC01_HTTP_MAX_KEEPALIVE | 4 | 8 | 16 | 32 |

文件位于源码`packaging/profiles/`，安装包中的`profiles/`，安装后为`/opt/itsm-workflow/profiles/`。名称分别是2c4g.env、4c8g.env、8c16g.env、16c32g.env；只含性能参数，不含地址、口令和用户Key。

单Uvicorn worker保持不变。CPU_MAX_WORKERS是数据处理线程数，不是Uvicorn worker数。不要用多worker或随机跨实例转发绕过内存运行归属。

## 2. 通用限制

```dotenv
REQUEST_MAX_BODY_MB=64
COMPILER_EVIDENCE_MAX_MB=8
COMPILER_MAX_AUDIT_OPERATIONS=5000
COMPILER_MODEL_INPUT_MAX_CHARS=24000
SQL_TEMPLATE_MAX_CHARS=65536
EXECUTOR_RUN_MEMORY_MB=256
```

MiB按1024×1024字节计算。模型输入限制为字符数，不是token数。提取最多499个SQL候选加end，最终图仍最多500节点。

CLI stdout20MiB、stderr1MiB不因机器档位增加。超限失败，不截断成功。每个实际CPU线程额外规划512MiB工作空间，并给操作系统、CLI子进程、网络缓冲和分配器保留余量；这不是程序硬RSS隔离。

提取完成缓存最多64条、1小时，另受缓存字节预算约束。Studio临时缓存和提取缓存也消耗全局预算；CPU/并发槽空闲不代表一定可以受理，先到任一限制即拒绝。

保持以下规则不随机器档位变化：

```dotenv
TEC01_TIMEOUT_SECONDS=20
TEC01_CALLBACK_MAX_ATTEMPTS=6
TEC01_CALLBACK_WAIT_SECONDS=70
LOG_RETENTION_DAYS=7
LOG_MAX_TOTAL_MB=512
LOG_MAX_FILE_MB=16
LOG_CLEANUP_INTERVAL_SECONDS=60
```

浏览器结果软预算为256MiB，保存在各自浏览器，与服务器内存档位无关；清理由用户确认，不会自动删除知识或历史计划。

## 3. 如何应用

先备份`/etc/itsm-workflow/service.env`，将对应profile文件中的八个键**按键替换**到原文件，再核对通用限制。不要整文件覆盖，不要重复追加同名变量，以免误删认证、AOPS地址和模型配置。

修改并重启前先停止新的下发，核实当前执行器没有活动流程；执行计划只在内存中，重启不会迁移进行中的任务。已有活动或未知外部结果应先通过tec01核实处理，不能直接以新计划覆盖。

```bash
sudo vi /etc/itsm-workflow/service.env
sudo systemctl restart itsm-workflow-api
sudo systemctl status itsm-workflow-api --full --no-pager
curl http://127.0.0.1:8089/api/v1/health
```

登录后在“运行配置”查看并发、字节预算及HTTP池的实际生效值。健康接口dataBudget展示的是应用预留计费，不是物理RSS。

升级保留已有显式值。旧环境如果写有EXECUTOR_MAX_ACTIVE_RUNS=32、EXECUTOR_MAX_ACTIVE_CLI=8，不会自动降到新默认8/2；请按机器规格手工调整。新键仅在缺失时补默认值。

## 4. 怎样调优

1. 实际CLI并发不得超过AOPS及目标数据库许可，提取并发不得超过LLM网关额度；机器更大不等于对端承载更大。
2. 先用对应profile测正常、恢复、大审计及大结果。目标为健康/活动查询p95≤250ms、暂停取消受理p95≤500ms；不是对SQL或模型完成时间的承诺。
3. 观察整个服务进程组内存（含CLI），目标不超过有效内存70%。cgroup限额低于物理内存时以较低值为准。
4. 如果超过内存目标，先把CLI和运行并发各减半，复测后再考虑预算；不要把对象计费字节当作真实RSS，也不要通过加大结果上限掩盖问题。
5. 如仅CPU延迟偏高，检查大SQL/审计/结果及请求体，再评估CPU_MAX_WORKERS；有GIL，默认最多2个CPU线程，不随核心数线性增加。
6. 如429多而资源仍空闲，分别查看提取名额、大请求名额、运行名额和数据预算；等待ACK的运行虽不占CLI槽，仍占活动及内存额度。
7. tec01持续故障仍沿原等待规则，提取可能等待很久；不能通过删掉ACK或跳过结果回报提升吞吐。

服务不保存生产结果文件；部署磁盘需另计systemd journal及主机其他服务。应用日志512MiB不限制journal副本，必须由主机运维单独设置journald保留策略。

基准脚本与环境说明见[性能验证报告](performance-report-0.13.0.md)。
