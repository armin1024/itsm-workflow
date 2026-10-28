# SQL读返回异常排查（0.13.1）

执行器从aops-cli的stdout读取JSON或SSE。CLI退出码为0仅说明进程正常退出，不能证明SQL成功；它可能打印错误页或提前结束的流。

## 识别顺序

1. 非零CLI退出码：保留NON_ZERO_EXIT，提取安全错误摘要。
2. JSON响应：检查整数status=0；业务失败提取message/msg/error/detail/reason，不把完整错误信封复制进摘要。
3. SSE：先检查显式error/failed/failure事件，即使没有done也报告AOPS_BUSINESS_ERROR；即使还有done=!ok也不能忽略错误。
4. HTML/XML、普通文本等非JSON非SSE：NON_SSE_RESPONSE。提示核查鉴权、地址、代理及CLI版本，不从HTML标题伪造真实HTTP状态。
5. 确实是SSE但缺少done：INVALID_SSE，结果可能不完整。保留严格失败，不自动重试、不执行下一节点。
6. done存在但值不是!ok：AOPS_BUSINESS_ERROR。空结果仍应以done=!ok正常结束。

正常结束示例：

```text
event:done
data:!ok
```

错误事件示例（即使没有done，也应显示“数据库无访问权限”）：

```text
event:error
data:{"message":"数据库无访问权限"}
```

## 如何检查

在失败节点的诊断中核对errorCode、exitCode、脱敏stdout/stderr和CLI版本。普通调用日志只记录安全元数据，不记录完整返回正文。

| 错误码 | 下一步 |
|---|---|
| AOPS_BUSINESS_ERROR | 根据实际错误摘要检查权限、数据库、SQL或AOPS服务 |
| NON_SSE_RESPONSE | 核查AOPS_BASE_URL、代理/鉴权返回、内网CLI版本；stdout可能是HTML错误页或文本 |
| INVALID_SSE | 核查AOPS/代理是否断流、服务端是否遗漏终止事件；有部分行不代表完整成功 |
| NON_ZERO_EXIT | 检查CLI退出码和脱敏诊断 |

本修复没有更改内网aops-cli。部分CLI版本未将HTTP错误转为非零退出，因此执行器仍需验证返回内容。CLI不提供HTTP状态时，不能从退出码推断HTTP200。

不要开启--debug或将Token贴到日志/工单。不要以反复执行生产SQL诊断回报格式；外部请求可能已经发生，先核对AOPS审计，再由用户决定是否重试。

本版本修复错误识别和诊断，不承诺消除上游网络断流或补齐实际缺失的done。正常失败回报、人工重试及tec01保存确认语义保持不变。
