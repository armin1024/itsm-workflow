# Linux内网部署

## 前置条件

- Linux x86_64，glibc 2.17或更高。
- 系统已安装可执行的`aops-cli`。
- 能访问AOPS和内网LLM。
- 不需要PostgreSQL、Redis、向量库或MCP端口。

## 安装

```bash
tar -xzf itsm-workflow-0.10.0-linux-x86_64.tar.gz
cd itsm-workflow-0.10.0-linux-x86_64
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
EXECUTOR_ID=executor-01
EXECUTOR_MAX_ACTIVE_RUNS=32
EXECUTOR_MAX_ACTIVE_CLI=8
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

Studio使用`STUDIO_ADMIN_TOKEN`建立HttpOnly会话。调试会真实调用外部系统并产生AOPS审计记录。知识、计划和结果保存在浏览器IndexedDB，服务端不建立数据库；不再需要`STUDIO_DATABASE_PATH`或本地数据库备份。更换浏览器、访问域名或清除站点数据前，应通过页面导出需要保留的记录。

默认本地调试不依赖tec01；启用状态同步前配置回调地址和令牌，并在tec01预先创建相应经验或运行。接入细节见[API文档](api.md)及[独立调试手册](standalone-studio-guide.md)。
