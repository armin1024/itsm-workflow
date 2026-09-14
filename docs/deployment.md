# Linux x86_64 非 Docker部署

## 前置条件

- Linux x86_64和 systemd。
- 可访问 PostgreSQL。
- 系统已安装 `aops-cli`，服务账号具有执行权限。
- Node.js只在构建机需要，目标机不需要。

## 工单草稿提取配置

在 `/etc/itsm-workflow/service.env` 配置 OpenAI-compatible 内网 LLM：

```dotenv
LLM_BASE_URL=http://llm.internal/v1
LLM_API_KEY=
LLM_MODEL=<内网模型名>
LLM_TIMEOUT_SECONDS=30
LLM_PATH=/chat/completions
LLM_RESPONSE_FORMAT=auto
```

服务会向 `${LLM_BASE_URL}${LLM_PATH}` 发送分析请求。`LLM_RESPONSE_FORMAT=auto` 会先尝试 JSON Object模式，推理网关不支持时自动改用普通对话响应，并解析纯 JSON或 Markdown JSON代码块。AOPS凭据来自当前登录会话，只通过子进程环境变量传给 `aops-cli`，不会写入知识、日志或命令行参数。

如需三方平台审核知识，在 `/etc/itsm-workflow/service.env` 配置独立审核凭据：

```dotenv
WORKFLOW_REVIEW_TOKEN=<与WORKFLOW_API_TOKEN不同的长随机Token>
WORKFLOW_REVIEW_ACTOR_UID=workflow-review-service
```

审核 Token不要写入 Hermes MCP配置；它只交给受信任的审核平台。

## 安装

```bash
tar -xzf itsm-workflow-0.4.1-linux-x86_64.tar.gz
cd itsm-workflow-0.4.1-linux-x86_64
sudo ./install.sh --no-start
sudo vi /etc/itsm-workflow/service.env
sudo systemctl start itsm-workflow-migrate
sudo systemctl start itsm-workflow-api itsm-workflow-worker itsm-workflow-mcp
```

MCP配置单独保存在 `/etc/itsm-workflow/mcp.env`，避免 MCP进程获得数据库连接和主加密密钥。首次安装或从旧版升级时，安装器会从 `service.env` 复制 `WORKFLOW_API_TOKEN` 并补齐安全默认值：

```dotenv
WORKFLOW_API_TOKEN=<必须与service.env一致>
WORKFLOW_PUBLIC_URL=http://workflow.internal:8089
MCP_INTERNAL_API_URL=http://127.0.0.1:8089/api/v1
MCP_BIND_HOST=127.0.0.1
MCP_PORT=8090
MCP_PATH=/mcp
MCP_ALLOWED_HOSTS=workflow.internal:*,127.0.0.1:*,localhost:*
MCP_WAIT_MAX_SECONDS=15
```

如果 Hermes直接通过服务器 IP连接且不经过反向代理，将 `MCP_BIND_HOST` 改成 `0.0.0.0`，并把实际 IP加入 `MCP_ALLOWED_HOSTS`，例如 `91.0.14.5:*`。

## 检查

```bash
systemctl status itsm-workflow-api itsm-workflow-worker itsm-workflow-mcp --no-pager
curl http://127.0.0.1:8089/api/v1/health
curl http://127.0.0.1:8089/metrics
curl http://127.0.0.1:8090/health
journalctl -u itsm-workflow-worker -n 100 --no-pager
journalctl -u itsm-workflow-mcp -n 100 --no-pager
```

健康检查中的 `cli.status` 必须为 `ok`。`CLI_NOT_FOUND`、`CLI_VERSION_MISMATCH` 会阻止操作节点成功执行。

离线包所有原生扩展均按 `manylinux_2_17 x86_64` 选择并在构建时扫描 GLIBC符号，可运行于 glibc 2.17及以上系统。

## 反向代理与 SSE

执行详情依赖长连接事件流。使用 Nginx时必须关闭 `/api/v1/runs/*/events` 的响应缓冲：

```nginx
location / {
    proxy_pass http://127.0.0.1:8089;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location ~ ^/api/v1/runs/.+/events$ {
    proxy_pass http://127.0.0.1:8089;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    add_header X-Accel-Buffering no;
}

location = /mcp {
    proxy_pass http://127.0.0.1:8090/mcp;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 30s;
    proxy_set_header Host $host;
}
```

Hermes使用最终地址 `/mcp`，不要依赖尾斜杠重定向。MCP单次工具超时建议30秒；事件等待工具本身最多等待10–15秒。

## 部署在域名子路径

例如页面地址为：

```text
https://test.mg.tf.cn/aops/itsm-workflow/
```

先在 `/etc/itsm-workflow/service.env` 配置：

```dotenv
WORKFLOW_BASE_PATH=/aops/itsm-workflow
```

在 `/etc/itsm-workflow/mcp.env` 配置：

```dotenv
WORKFLOW_PUBLIC_URL=https://test.mg.tf.cn/aops/itsm-workflow
MCP_ALLOWED_HOSTS=test.mg.tf.cn,test.mg.tf.cn:*,127.0.0.1:*,localhost:*
```

Nginx配置如下。`proxy_pass` 末尾的 `/` 不能省略，它负责把 `/aops/itsm-workflow/` 前缀从上游请求中移除：

```nginx
location = /aops/itsm-workflow {
    return 301 /aops/itsm-workflow/;
}

location = /aops/itsm-workflow/mcp {
    proxy_pass http://91.0.14.5:8090/mcp;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 30s;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location ~ ^/aops/itsm-workflow/api/v1/runs/[^/]+/events$ {
    rewrite ^/aops/itsm-workflow/(.*)$ /$1 break;
    proxy_pass http://91.0.14.5:8089;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    add_header X-Accel-Buffering no;
}

location /aops/itsm-workflow/ {
    proxy_pass http://91.0.14.5:8089/;
    proxy_http_version 1.1;
    proxy_read_timeout 600s;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /aops/itsm-workflow;
    proxy_hide_header Content-Security-Policy;
    proxy_hide_header X-Frame-Options;
    add_header Content-Security-Policy "frame-ancestors 'self' https://test.mg.tf.cn" always;
}
```

修改后执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl restart itsm-workflow-api itsm-workflow-mcp
```

Hermes地址相应改为：

```text
https://test.mg.tf.cn/aops/itsm-workflow/mcp
```

## 升级

安装器会停止 API、Worker、MCP和迁移单元，替换程序文件但保留 `/etc/itsm-workflow/service.env`。启动迁移单元成功后才能启动 API、Worker和MCP。
