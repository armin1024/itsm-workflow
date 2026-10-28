# 调用日志与清理手册（0.12.0）

## 0.12.2：友好检索与后台原文

日志检索统一填写后点击“查询”应用，提供中文级别、事件与操作选择，以及不限时间、最近1小时/24小时/7天、自定义时间。日历输入使用浏览器时区，自动转换为带时区ISO；不要手工拼接时区。最近范围在刷新最新日志时重新计算，读取更早日志时冻结本次边界，避免游标失效。错误字段就地提示；清空条件可以移除旧URL条件。接口空时间字符串视为没有时间筛选。

新增精确event、operation参数，技术码保持英文，例如CALL_FAILED、analyze_evidence。字段校验错误保留details.fields并用中文说明具体参数，不回显输入内容。API文档的摘要、说明、分组和参数注释已中文化，但路径、JSON字段及稳定错误码不改名。

在“日志”标题区进入“后台原始日志”：

- 应用日志文件原文：GET /api/v1/studio/logs/raw，按原JSONL事件行读取，不合并调用。支持与日志列表相同的筛选、游标和8MiB扫描上限，返回rawLines（fileName/text/logId/timestamp）、nextCursor等。敏感字段会隐藏，不支持任意文件路径。
- 服务控制台：GET /api/v1/studio/logs/journal，仅读取itsm-workflow-api.service，支持limit（1～200，默认100）、keyword、fromTime、toTime。包含主机保存的启动和异常信息，关键词只过滤所取最新一批；缩小时间范围可查看更早记录。无法通过接口选择其他unit或执行shell。

两者均需Studio管理会话，不记录/开放请求正文、SQL、凭据；展示经过脱敏的原文，而不是保证逐字节不变的磁盘镜像。journal读取最长5秒、stdout最多2MiB，超时、输出超限、命令缺失或权限不足都返回中文状态，不让页面白屏。非Linux环境提示使用应用日志。

本安装器**不自动扩大服务账号权限**。Linux管理员如决定开放完整journal读取，先核实发行版存在systemd-journal组，再按主机安全策略操作：

```bash
getent group systemd-journal
sudo usermod -aG systemd-journal itsmworkflow
sudo systemctl restart itsm-workflow-api
```

该组一般意味着进程拥有更广的主机journal读取能力，请先评估；页面自身仍固定只读本服务。如果发行版没有该组，请管理员核实本机方案，不自动创建组、不开放任意sudo。部分账号只能看到自己的记录，可能缺少PID1写入的启动错误，页面会在可识别时提示权限不完整。

API完全启动不了时，网页无法通过已停止的API获取journal，应直接在主机运行`sudo journalctl -u itsm-workflow-api.service -n 100 --no-pager -o cat`。journal保留仍由主机管理，不计入应用7天/512MiB限额。

## 0.12.1：调用配对与中文说明

列表默认按调用合并开始/结束，使用spanId识别一次尝试；同一回报的重试共用callId但各有独立spanId。旧入站日志可按traceId配对，不能按共享requestId或nodeId猜配。可切换“原始事件”，详情显示开始、结束、重试时间线，并按spanId/callId/traceId查询关联记录，不受列表级别和时间筛选限制。

操作提供operationLabel和operationDescription：例如获取节点目录、校验定义、回报参数化进度、回报节点结果、等待保存确认、执行SQL只读查询。原operation技术码保留。开始记录没有最终耗时或响应码是正常现象；网络未响应时不制造HTTP状态，CLI退出码也不转换成HTTP码。仅见开始记录表示当前扫描批次未见结束，可能仍在处理、被筛选、在其他页或已清理，不能据此确认仍在运行。

有真实durationMs时使用原值；两端都在但旧结束记录没有耗时时，可以按日志时间差显示“约”，不冒充精确测量。回报总等待到期是整体结论，显示“保存确认总等待到期”，不是一条新HTTP响应。

HTTP200、acknowledged=true只说明回报保存确认有效。businessStatus=FAILED表示被回报的业务失败；提取终态的安全业务错误码通过businessErrorCode记录，可搜索LLM_ANALYSIS_FAILED、NO_VALID_OPERATIONS、COMPILER_FAILED，不写入模型原文或错误正文。历史日志未记录的业务错误码无法补造。

日志查询新增精确参数spanId、callId、traceId，认证、8MiB扫描上限、保留和清理策略不变；过滤器或升级后旧游标失效时请重新查询最新。

## 作用与安全

“日志”菜单读取服务端JSON Lines滚动文件，与浏览器IndexedDB业务记录分开。记录tec01下发/控制、执行与提取回报、LLM请求、AOPS CLI调用的开始、完成、失败、耗时和重试。日志不参与恢复，不替代tec01的生产事实。

HTTP 200配合TEC01_ACK_NOT_SAVED表示请求已到达，但保存确认不合法。CLI仅记录退出码，不冒充AOPS HTTP状态。关联字段包括traceId、requestId、runId、dispatchId、knowledgeId、nodeId、attemptId。

回报日志还包含stage和businessStatus（若有），用于区分“回报请求成功”和“节点/提取本身成功”；例如HTTP 200、acknowledged=true、businessStatus=FAILED表示失败事实已被tec01保存，不代表节点执行成功。

不记录正文、请求头、Token、Cookie、SQL、参数、提示词、查询结果或原始stdout/stderr；字段采用白名单，错误为受控代码，URL去掉凭据、查询串和片段。DEBUG也不会开启原始报文。Uvicorn原始access日志关闭，HTTP客户端调试日志受限。成功GET轮询、健康检查、静态资源和日志查询不逐条记录，失败请求仍记录安全摘要。

## 配置与部署

Linux `/etc/itsm-workflow/service.env`：

```dotenv
LOG_LEVEL=INFO
LOG_DIR=/var/lib/itsm-workflow/logs
LOG_RETENTION_DAYS=7
LOG_MAX_TOTAL_MB=512
LOG_MAX_FILE_MB=16
LOG_CLEANUP_INTERVAL_SECONDS=60
```

MB按MiB计算。源码调试默认LOG_DIR=logs，目录已忽略且不打包。级别支持DEBUG/INFO/WARNING/ERROR；设置ERROR会过滤正常调用记录。修改后重启，在“运行配置”只读核对策略，在“日志”查看实际占用和错误计数。

安装器创建默认目录，目录0700、文件0600，归属itsmworkflow；升级不删除日志、不覆盖显式配置。自定义目录建议位于 `/var/lib/itsm-workflow/` 下，其他位置需自行授权并调整systemd ReadWritePaths，否则ProtectSystem=strict阻止写入。

```bash
sudo systemctl restart itsm-workflow-api
sudo journalctl -u itsm-workflow-api -f -o cat
sudo journalctl -u itsm-workflow-api --since '1 hour ago' --no-pager -o cat
```

终端输出同一份结构化日志。应用文件7天/512MiB规则**不限制systemd journal副本**，journal保留和容量由主机管理员管理，安装器不修改全局journald配置。

## 自动清理与故障

- 文件名 `calls-<UTC时间>-<随机ID>.jsonl`，UTF-8一行一事件，单文件16MiB或跨UTC日期轮转；不压缩、不建数据库。
- 启动、轮转及每60秒检查过期归档，写前检查总量；超限删除最旧归档，活动文件与归档合计最多512MiB。
- 保留按段创建时间计算，已关闭段超过7×24小时整体删除，不承诺每条记录完整保留7天；容量先到可能提前删除旧日志。
- 空闲时仍维护；跨日活动段关闭后可清理，重启后旧段均视作归档。
- 仅清理专用目录中本服务命名的常规文件，不跟随符号链接、不删除其他文件。不要使用日志命名保存业务文件。
- 清理无权限或外部人工写入超额文件时，无法保证删除成功；停止继续文件写入并告警，不影响执行、提取和回报。
- 后台队列最多2048条，满后丢弃新记录并计数；磁盘失败仍尽可能输出终端。停止服务最多等待3秒刷盘，强制退出可能丢失未写记录。
- 页面显示占用、文件数、最近清理、删除数、待写数和自本次启动以来的写入失败、清理失败、丢弃数。告警通过独立终端通道，避免递归写日志。

## 页面与REST

页面支持关键词、级别、对端，精确筛选支持方向、运行/知识/节点/请求ID及时间。时间为带时区ISO 8601，起始包含、结束不含。点击行查看安全字段，无删除、配置修改或重放入口。

自动更新默认关闭，开启后每5秒更新最新一批，页面隐藏后停止。读取历史时暂停追加，点击“返回最新”恢复；筛选保存在URL，日志不复制到IndexedDB。

接口需要Studio管理会话，不能仅用Runtime服务令牌查询：

```bash
curl -c studio-cookie.txt -X POST 'http://127.0.0.1:8089/api/v1/studio/session' \
  -H 'Content-Type: application/json' -d '{"token":"<STUDIO_ADMIN_TOKEN>"}'
curl -b studio-cookie.txt --get 'http://127.0.0.1:8089/api/v1/studio/logs' \
  --data-urlencode 'peer=TEC01' --data-urlencode 'runId=run_100173' \
  --data-urlencode 'fromTime=2026-10-22T00:00:00+08:00' --data-urlencode 'limit=100'
curl -b studio-cookie.txt 'http://127.0.0.1:8089/api/v1/studio/logs/status'
```

Cookie文件是凭据，使用后自行安全删除，勿提交或传入日志查询参数；部署上下文前缀时需在URL补齐前缀。

`GET /api/v1/studio/logs`支持 `keyword/level/peer/direction/runId/knowledgeId/nodeId/requestId/fromTime/toTime/limit/cursor`。不同字段AND，关键词对安全字段包含匹配；游标默认100条、最多200条，每次分块倒序扫描最多8MiB，不统计总数、不加载全量文件。响应：

```json
{"items":[{"timestamp":"2026-10-22T02:00:00+00:00","logId":"<id>","level":"WARNING","event":"CALL_FAILED","peer":"TEC01","direction":"OUTBOUND","operation":"NODE_COMPLETED","runId":"run_100173","nodeId":"sql-1","attempt":1,"maxAttempts":6,"httpStatus":200,"errorCode":"TEC01_ACK_NOT_SAVED","durationMs":12.3}],"nextCursor":"<下一批游标或null>","scannedBytes":65536}
```

筛选稀疏时本批可能无匹配但仍有nextCursor，可继续读取。文件清理或筛选改变后旧游标失效。未登录401，参数非法422，游标失效409，存储不可读返回空列表及unavailable:true。接口不接受文件路径，不提供写入或清理方法。

`GET /api/v1/studio/logs/status`返回目录、级别、保留与容量、清理间隔、占用、文件数、最近清理、删除数、写入/清理错误、丢弃、待写、最后错误及available。日志为空可能是级别过滤、清理、丢弃或磁盘失败，不能据此认定未执行；生产状态以tec01为准。
# 提取失败检索补充（0.13.2）

提取器在失败发生时写独立COMPILER_FAILED/ERROR，peer=COMPILER、direction=INTERNAL；不是HTTP调用，不补造响应码。记录受控errorMessage、具体reasonCode、errorField、errorId、stage、异常类别及安全代码位置。关闭tec01同步也能查询；如果回报通信成功则仍可能另有INFO/HTTP200，两者不矛盾。

日志页关键词可以搜索中文原因、字段和错误编号，高级筛选可组合errorCode、reasonCode、errorField、errorId、stage及既有knowledgeId/requestId；不同条件AND组合。提取页提供按错误编号跳转。详情见[失败诊断说明](compiler-failure-diagnostics.md)。不记录原始异常文字、SQL、参数、模型原文或locals，不补造历史缺失诊断。
