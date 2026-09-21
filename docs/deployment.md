# Linux内网部署

## 前置条件

- Linux x86_64，glibc 2.17或更高。
- 系统已安装可执行的`aops-cli`。
- 能访问AOPS和内网LLM。
- 不需要PostgreSQL、Redis、向量库或MCP端口。

## 安装

```bash
tar -xzf itsm-workflow-0.8.1-linux-x86_64.tar.gz
cd itsm-workflow-0.8.1-linux-x86_64
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
STUDIO_DATABASE_PATH=/var/lib/itsm-workflow/studio.db
```

不要在`service.env`配置`AOPS_API_KEY`。真实SQL测试时由使用者在页面临时输入。

## 服务

```bash
systemctl status itsm-workflow-api --no-pager
journalctl -u itsm-workflow-api -n 200 --no-pager
curl http://127.0.0.1:8089/api/v1/health
```

tec01通过同步内部API传递`ticketInfo`和`auditTimeline`并取得DraftProposal，不需要启动独立Compiler Worker。为tec01配置Runtime服务认证：

```dotenv
RUNTIME_SERVICE_TOKEN=<tec01调用itsm-workflow的服务令牌>
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

Studio使用`STUDIO_ADMIN_TOKEN`建立HttpOnly会话；仍建议叠加内网ACL、VPN或Nginx统一认证。TEST模式会真实调用外部系统并产生AOPS审计记录。SQLite可能包含测试SQL结果，应将`/var/lib/itsm-workflow`限制为服务用户可读，并按TTL清理。
