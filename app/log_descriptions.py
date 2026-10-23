"""Static, safe operation descriptions: never copy user payload into prose."""

STAGES = {
    "FILTERING_OPERATIONS": "过滤有效SQL操作",
    "PARAMETERIZING_SQL": "去重并参数化SQL",
    "ANALYZING_EVIDENCE": "分析工单与操作证据",
    "BUILDING_WORKFLOW": "组装Workflow草稿",
    "VALIDATING_WORKFLOW": "校验Workflow定义",
}
BUSINESS_ERRORS = {"LLM_ANALYSIS_FAILED", "NO_VALID_OPERATIONS", "COMPILER_FAILED"}


def describe(record: dict) -> tuple[str, str]:
    operation = record.get("operation")
    if operation == "http_request":
        path = record.get("url", "")
        method = record.get("method")
        if path.endswith("/catalog"):
            return "获取节点目录", "读取支持的节点类型、配置约束与目录版本。"
        if "/runtime/schemas" in path:
            return "获取Workflow JSON约束", "读取流程、下发或提取请求的JSON Schema，不执行节点。"
        if path.endswith("/workflows/validate"):
            return "校验Workflow定义", "检查节点、连线、参数绑定和SQL规则，返回规范化定义。"
        if path.endswith("/workflows/plan"):
            return "生成执行计划预览", "整理节点、输入来源与缺失参数，不执行SQL。"
        if path.endswith("/executions/dispatch"):
            return "接收Workflow执行下发", "受理固定流程和恢复输入；HTTP成功不代表整个流程执行成功。"
        if path.endswith("/commands"):
            return "接收运行控制请求", "接收暂停或取消意图，是否生效以运行事实为准。"
        if "/extractions" in path or "/compiler/" in path:
            return "接收工单知识提取请求", "接收工单证据或测试取证请求；后台提取结果另行回报。"
        if path.endswith("/session"):
            return ("退出Studio会话" if method == "DELETE" else "验证Studio登录"), "处理管理会话，不记录登录令牌。"
        if path.endswith("/config"):
            return "查看运行配置", "读取安全的只读配置，不展示凭据值。"
        return "处理服务接口请求", "记录接口的受理和响应，不记录请求正文。"
    if operation == "PROGRESS":
        stage = STAGES.get(record.get("stage"), "提取阶段")
        return "回报提取进度：" + stage, "向tec01同步实际阶段及处理进度；分析阶段仅进入时回报，不发送定时心跳。"
    if operation == "FINISHED":
        failed = record.get("businessStatus") == "FAILED"
        return ("回报知识提取失败" if failed else "回报知识提取终态"), "发送草稿或提取失败事实；回报成功与提取成功是两个不同结论。"
    if operation == "NODE_STARTED":
        return "回报节点开始执行", "通知tec01当前节点及尝试ID，收到有效保存确认后再执行节点。"
    if operation == "NODE_COMPLETED":
        return "回报节点执行结果", "发送节点输出、诊断或人工待办，等待tec01确认保存后才能继续。"
    if operation == "RUN_RELEASED":
        return "回报运行结束或释放", "同步完成、暂停、人工等待、失败或取消后的释放事实，不删除历史记录。"
    return {
        "callback_ack_wait": ("等待tec01保存确认超时", "整条回报含请求和退避的总等待已到期；这是整体结论，不是一条新的HTTP请求。"),
        "pause_run": ("请求暂停Workflow运行", "记录暂停意图，在当前操作安全边界生效；受理不代表已经暂停。"),
        "cancel_run": ("请求取消Workflow运行", "记录取消意图并尽力终止CLI，不保证撤销已经到达AOPS的请求。"),
        "analyze_evidence": ("调用LLM提炼经验草稿", "请求内网模型分析工单和参数化步骤，并解析结构化结果。"),
        "sql_read": ("执行AOPS SQL只读查询", "运行真实aops-cli并解析结果；这里只记录CLI退出码，不记录SQL或数据。"),
        "ticket_info": ("获取AOPS工单详情", "通过aops-cli读取工单详情，用于独立调试取证。"),
        "audit_timeline": ("获取AOPS工单操作记录", "通过aops-cli读取审计时间线，用于独立调试取证。"),
        "aops_command": ("调用AOPS CLI", "执行受控CLI操作，不记录命令参数或凭据。"),
        "dispatch": ("提交Workflow执行任务", "准备受理执行，最终受理结果应结合对应HTTP响应。"),
        "extraction": ("提交工单证据提取任务", "准备受理提取，后续进度和终态由后台任务回报。"),
    }.get(operation, ("服务处理事件", "查看技术操作码和关联ID定位此事件。"))
