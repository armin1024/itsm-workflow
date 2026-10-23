# Linux内网部署

## 调用日志（0.12.0）

“日志”菜单只读展示调用情况。默认文件目录`/var/lib/itsm-workflow/logs`，7天、最多512MiB、单文件16MiB，每60秒清理；升级保留显式配置和日志。终端同时输出结构化日志，日志不保存正文或凭据，不引入业务数据库。配置、journalctl、API及故障处理见[日志运维手册](logging.md)。应用限额不约束systemd journal副本。

## 前置条件

- Linux x86_64，glibc 2.17或更高。
- 系统已安装可执行的`aops-cli`。
- 能访问AOPS和内网LLM。
- 不需要PostgreSQL、Redis、向量库或MCP端口。

## 安装

```bash
tar -xzf itsm-workflow-0.12.2-linux-x86_64.tar.gz
cd itsm-workflow-0.12.2-linux-x86_64
sudo ./install.sh --no-start
sudo vi /etc/itsm-workflow/service.env
sudo systemctl start itsm-workflow-api
sudo systemctl status itsm-workflow-api --no-pager
```

必须核对：

```dotenv
AOPS_BASE_URL=https://aops.internal/aops/api
AOPS_CLI_PATH=/usr/local/bin/aops-cli
LLM_BASE_URL=http://llm.internal/v1
LLM_MODEL=<模型名>
STUDIO_ADMIN_TOKEN=<随机长管理Token>
```

不要在`service.env`配置`AOPS_API_KEY`。真实SQL测试时由使用者在页面临时输入。

## 服务

```bash
systemctl status itsm-workflow-api --no-pager
journalctl -u itsm-workflow-api -n 200 --no-pager
curl http://127.0.0.1:8089/api/v1/health
```

tec01通过内部API传递`ticketInfo`和`auditTimeline`，Compiler在后台处理并回调进度和DraftProposal，不需要启动旧版轮询Compiler Worker。为tec01配置Runtime服务认证：

```dotenv
RUNTIME_SERVICE_TOKEN=<tec01调用itsm-workflow的服务令牌>
TEC01_BASE_URL=https://tec01.internal
TEC01_SERVICE_TOKEN=<itsm-workflow回调tec01的服务令牌>
TEC01_RUN_CALLBACK_PATH=/internal/v1/runs/{runId}/callbacks
TEC01_EXTRACTION_CALLBACK_PATH=/internal/v1/extractions/{knowledgeId}/callbacks
EXECUTOR_ID=executor-01
EXECUTOR_MAX_ACTIVE_RUNS=32
EXECUTOR_MAX_ACTIVE_CLI=8
TEC01_TIMEOUT_SECONDS=20
TEC01_CALLBACK_MAX_ATTEMPTS=6
TEC01_CALLBACK_WAIT_SECONDS=70
```

```bash
# 旧版Compiler Worker不属于新部署拓扑
sudo systemctl disable --now itsm-workflow-compiler
```

## Nginx子路径

service.env：

```dotenv
WORKFLOW_BASE_PATH=/aops/itsm-workflow
```

Nginx：

```nginx
location /aops/itsm-workflow/ {
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 600s;
    proxy_pass http://127.0.0.1:8089/;
}
```

重启：

```bash
sudo systemctl restart itsm-workflow-api
```

## 安全边界

运行回调要求tec01已实现HTTP200、整数业务status=200及data.runId匹配的AopsResult；不再要求saved或节点身份回显，200只能在事务提交并采纳后返回。先拒绝旧调度409，再确认当前调度已保存的完全相同重试，最后拒绝新终态冲突。空data仍会停止执行。运行回报对5xx、连接异常和超时默认最多尝试6次，退避1/2/4/8/16秒，仍受70秒总等待约束，不保证6次全部发生。4xx和无效确认不重试。提取五阶段各一次、不发心跳，原requestId仍用于隔离；终止回报统一TERMINATED。请先让tec01配套再开启生产同步。

已有service.env显式配置的TEC01_CALLBACK_MAX_ATTEMPTS（例如3）会保留；如需采用新的默认6次策略，请自行调整该值并重启服务。提取回报仍固定最多6次且不套用运行回报总等待上限。

服务按单进程运行（systemd固定`--workers 1`，`KillMode=control-group`）。不要在不具备运行归属控制时改为多进程或跨实例随机负载均衡。恢复前查询同一执行器活动；active=false不证明远端AOPS未执行。

Studio使用`STUDIO_ADMIN_TOKEN`建立HttpOnly会话。调试会真实调用外部系统并产生AOPS审计记录。知识、计划和结果保存在浏览器IndexedDB，服务端不建立数据库，不需要数据库配置或数据库备份。更换浏览器、访问域名或清除站点数据前，应通过页面导出需要保留的记录。升级安装会清理旧数据库路径配置，不删除历史数据库文件。

## 回报接口配置与只读查看

登录后进入“运行配置”，或访问 `/config`（部署上下文前缀仍适用），查看当前生效配置。令牌只显示是否配置；页面不提供修改、连接探测或自动请求tec01功能。

- `TEC01_RUN_CALLBACK_PATH`：节点开始、节点完成、流程释放统一使用，且必须包含一次 `{runId}`。
- `TEC01_EXTRACTION_CALLBACK_PATH`：提取进度、最终结果统一使用，且必须包含一次 `{knowledgeId}`。
- 完整地址为 `TEC01_BASE_URL + 路径模板`，保留基础地址的上下文。例如基础地址 `https://tec01.internal/aops`、路径 `/custom/runs/{runId}/report`，实际地址是 `https://tec01.internal/aops/custom/runs/run_1/report`。
- 路径只接受以 `/` 开头的相对路径，不接受完整URL、查询参数、路径跳转或其他占位符；ID由执行器编码替换。
- 未配置新字段时沿用上述默认接口；升级脚本会把默认配置补入现有 `service.env`，已配置值不会覆盖。
- 修改 `/etc/itsm-workflow/service.env` 后执行 `sudo systemctl restart itsm-workflow-api`。刷新配置页面即可核对。
- 地址配置不改变回报结构：运行回报仍需严格保存确认。运行回报最多尝试次数和总等待分别由现有配置控制，5xx、连接异常和超时有限重试；提取回报沿用最多6次策略。单次HTTP请求实际超时上限30秒。

默认本地调试不依赖tec01；启用状态同步前配置回调地址和令牌，并在tec01预先创建相应经验或运行。接入细节见[API文档](api.md)及[独立调试手册](standalone-studio-guide.md)。
