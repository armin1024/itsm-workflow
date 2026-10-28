"""Chinese documentation and safe validation text; wire names stay stable."""
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

FIELDS = {
    "limit": ("每批条数", "每次返回1～200条，默认100；受8MiB扫描上限约束。"),
    "cursor": ("分页游标", "使用上一批nextCursor；修改筛选条件后必须清空。"),
    "keyword": ("关键词", "最多200字符，在安全日志字段中包含匹配。"),
    "level": ("日志级别", "INFO信息、WARNING警告、ERROR错误、DEBUG调试；空值表示全部。"),
    "peer": ("调用对端", "TEC01、STUDIO本地页面、LLM模型网关、AOPS_CLI命令行、COMPILER提取器。"),
    "direction": ("调用方向", "INBOUND入站、OUTBOUND出站、INTERNAL服务内部；空值表示全部。"),
    "errorCode": ("错误码", "精确匹配通信errorCode或业务businessErrorCode，例如COMPILER_FAILED、LLM_ANALYSIS_FAILED。"),
    "reasonCode": ("具体原因码", "精确匹配具体原因，例如LLM_RESPONSE_SCHEMA_INVALID、SQL_INPUT_MISMATCH。"),
    "errorField": ("失败字段", "精确匹配安全字段路径，例如/steps/0/parameters，不包含字段实际值。"),
    "errorId": ("错误编号", "按响应中的errorId定位对应独立失败日志。"),
    "stage": ("失败阶段", "精确匹配ANALYZING_EVIDENCE、BUILDING_WORKFLOW等阶段码。"),
    "event": ("事件类型", "按技术事件码精确匹配，例如CALL_FAILED或RETRY_SCHEDULED。"),
    "operation": ("操作类型", "按技术操作码精确匹配，例如sql_read、NODE_COMPLETED。"),
    "fromTime": ("开始时间", "ISO 8601且需包含时区，包含起始边界；空值视为未筛选。"),
    "toTime": ("结束时间", "ISO 8601且需包含时区，不包含结束边界；应晚于开始时间。"),
    "runId": ("运行ID", "tec01或本地运行的唯一标识，精确匹配。"),
    "knowledgeId": ("知识ID", "知识或提取任务的标识。"),
    "nodeId": ("节点ID", "流程中的节点实例标识，不是节点类型。"),
    "requestId": ("请求ID", "本次请求或提取尝试的幂等标识。"),
    "spanId": ("调用尝试ID", "关联一次调用的开始和结束，不合并不同重试。"),
    "callId": ("回报调用ID", "关联同一回报的全部尝试与退避事件。"),
    "traceId": ("调用链路ID", "关联入站请求与其派生调用。"),
    "dispatchId": ("调度ID", "每次重新下发需生成新值，同一ID不能提交不同内容。"),
    "ticketId": ("工单ID", "必填正整数，用于执行SQL时生成工单注释。"),
    "workflowDefinition": ("流程定义", "节点、连线及数据来源的完整Workflow对象。"),
    "workflowSnapshot": ("固定流程快照", "与内容哈希对应的完整规范化定义，不是增量。"),
    "workflowContentHash": ("流程内容哈希", "使用校验接口返回值，不要手工构造。"),
    "workflowVersionId": ("流程版本ID", "由tec01保存的不可变版本标识。"),
    "targetCatalogDigest": ("预期节点目录摘要", "与当前目录不一致时返回409，需刷新目录再校验。"),
    "validationMode": ("校验模式", "DRAFT草稿、PUBLISH发布、EXECUTE执行、TEST本地测试。"),
    "parameters": ("计划运行参数", "仅填写运行时输入，保留真实JSON类型。"),
    "runInputs": ("运行输入", "本次用户确认的运行参数，不包含凭据。"),
    "nodeStates": ("已确认节点状态", "tec01保存的节点事实；未知结果不能直接重放。"),
    "nodeOutputs": ("已确认节点输出", "供恢复时的前置结果绑定使用，不能伪造。"),
    "nodeResults": ("节点调试记录", "可选的交互与调试留痕，不作为凭据容器。"),
    "selectedRoutes": ("已选条件分支", "条件节点ID到连线ID的映射，恢复时保留。"),
    "currentNodeId": ("当前节点ID", "恢复起点，不允许直接重放未核实节点。"),
    "resumePayload": ("恢复或人工回复", "人工表单提交包含nodeId、action=SUBMIT和values。"),
    "actorUid": ("操作者UID", "用于计划材料，不代表已通过生产授权。"),
    "ticketInfo": ("工单详情", "填写AOPS info响应中的data对象，不是完整响应信封。"),
    "auditTimeline": ("操作记录", "填写audit_timeline响应中的data对象数组。"),
    "token": ("管理令牌", "仅用于创建Studio会话，不是AOPS执行凭据。"),
    "syncTec01": ("同步至tec01", "本地调试可选；开启前需配置回报并创建对应记录。"),
    "commandId": ("控制请求ID", "暂停或取消请求的标识，受理不等于已经生效。"),
    "type": ("类型", "使用接口或节点约束允许的类型值。"),
    "after": ("事件游标", "只读取此事件序号之后的本地临时事件。"),
    "planMaterialHash": ("计划确认材料哈希", "用于防止确认后定义、工单或参数发生变化。"),
    "x-aops-api-key": ("AOPS执行凭据", "真实CLI操作时临时传入，不落库或日志。"),
    "authorization": ("服务认证", "内部调用填写Bearer及Runtime服务令牌。"),
    "itsm_runtime_studio": ("Studio会话Cookie", "通过管理登录建立的HttpOnly会话。"),
    "if_none_match": ("缓存目录指纹", "命中当前目录时返回304，无正文。"),
    "schema_name": ("约束名称", "workflowDefinition、dispatch或extraction。"),
    "run_id": ("运行ID", "要查询或控制的运行标识。"),
    "knowledge_id": ("知识ID", "tec01预先创建的知识或提取记录标识。"),
    "stream_id": ("临时事件流ID", "用于本地调试事件查询，非生产持久化记录。"),
}


def validation_message(error: dict) -> str:
    kind = error.get("type", "")
    name = str(error.get("loc", [""])[-1])
    if name in {"fromTime", "toTime"}:
        return "请选择有效时间；接口调用需使用带时区的ISO 8601格式"
    if name in {"level", "peer", "direction"} and kind == "string_pattern_mismatch":
        return "选项不正确，请重新选择或清空该条件"
    return {
        "missing": "缺少必填字段", "int_parsing": "请输入整数", "int_type": "字段必须为整数",
        "float_parsing": "请输入有效数字", "bool_parsing": "请输入true或false布尔值",
        "greater_than": "数值必须大于允许的下限", "greater_than_equal": "数值小于允许的最小值",
        "less_than": "数值必须小于允许的上限", "less_than_equal": "数值超过允许的最大值",
        "string_too_long": "字符数超过限制，请缩短输入", "string_too_short": "字符数不足，请补充输入",
        "string_pattern_mismatch": "字段格式不符合约束", "string_type": "字段必须为字符串",
        "list_type": "字段必须为数组", "dict_type": "字段必须为JSON对象",
        "model_attributes_type": "请求内容必须为JSON对象", "json_invalid": "请求正文不是合法JSON",
    }.get(kind, "字段类型或格式不正确，请核对接口约束")


def localize_openapi(app):
    summaries = {
        "health": "检查服务健康状态", "studio_login": "登录调试工作台", "studio_session": "查询登录会话", "studio_logout": "退出调试工作台",
        "studio_catalog": "获取调试节点目录", "studio_configuration": "查看只读运行配置", "log_status": "查询日志存储与清理状态", "log_query": "检索调用日志",
        "raw_log_query": "读取应用日志文件原文", "journal_logs": "读取本服务控制台日志", "compiler_preview": "同步预览工单提取草稿",
        "compiler_ticket": "按工单ID预览提取草稿", "studio_extract": "提交本地JSON提取任务", "studio_extract_ticket": "按工单ID提交本地提取",
        "studio_validate": "校验本地流程定义", "studio_plan": "生成本地执行计划", "studio_dispatch": "下发本地调试运行",
        "studio_command": "暂停或取消本地运行", "studio_activity": "查看本地活动运行", "events": "读取本地提取或运行事件",
        "runtime_catalog": "获取生产节点目录", "runtime_validate": "校验并规范化Workflow", "runtime_schemas": "获取全部JSON约束",
        "runtime_schema": "获取单份JSON约束", "runtime_plan": "预览生产流程计划材料", "extraction": "提交生产知识提取任务",
        "execution_dispatch": "下发生产Workflow执行", "execution_command": "暂停或取消生产运行", "execution_activity": "核实执行器活动状态",
    }
    descriptions = {
        "log_query": "按事件、操作、对端、时间和关联ID检索安全元数据，使用游标分批读取，不返回全量总数。",
        "raw_log_query": "读取应用JSONL文件行，不合并事件，不允许传入任意文件路径；敏感字段会隐藏。",
        "journal_logs": "仅读取itsm-workflow-api.service控制台日志，包含启动与异常。需要主机journal权限，不执行sudo；关键词只过滤所取最新一批。",
        "log_status": "查看应用日志目录、容量、清理时间、写入失败和丢弃计数；不包含主机journal占用。",
        "runtime_validate": "校验DAG、节点配置、只读SQL和参数绑定，返回完整规范化定义与哈希；不会执行SQL。",
        "runtime_plan": "检查运行参数并生成计划材料，只供联调预览；正式计划与用户确认由tec01保存。",
        "execution_dispatch": "异步受理完整固定流程，HTTP 200仅代表受理。含SQL节点时需当前用户AOPS凭据；逐节点回报获保存确认后继续。",
        "extraction": "异步受理工单详情data对象与审计data数组，HTTP 202不是提取成功。每阶段和终态回报至tec01。",
        "execution_command": "发送PAUSE暂停或CANCEL取消意图。读取accepted判断是否受理，最终结果以回报事实为准。",
        "execution_activity": "只查询本实例活动情况。active=false不表示从未执行，未知结果不得自动重放。",
        "runtime_catalog": "读取支持的节点类型及版本，If-None-Match命中时返回304。",
        "runtime_schemas": "获取Workflow、完整下发与提取请求的JSON Schema及各类型节点约束。",
        "runtime_schema": "读取指定名称的Schema，未知名称返回404，不提供已取消的单节点执行约束。",
    }
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        route.summary = summaries.get(route.name, "服务接口")
        if "/logs" in route.path:
            group = "日志与诊断"
        elif "/session" in route.path:
            group = "调试登录"
        elif "/extractions" in route.path or "/compiler" in route.path:
            group = "知识提取"
        elif "/executions" in route.path or "/events" in route.path:
            group = "运行与控制"
        else:
            group = "流程定义与服务配置"
        route.tags = [group]
        auth = "提交管理Token建立HttpOnly会话。" if route.name == "studio_login" else "查询浏览器会话是否有效，不要求已登录。" if route.name == "studio_session" else "通过Studio管理会话访问。" if route.path.startswith("/api/v1/studio/") else "内部调用需Runtime服务令牌。" if route.path.startswith("/internal/") else "服务状态接口。"
        route.description = descriptions.get(route.name, route.description or route.summary + "。") + auth + "路径、字段名和稳定错误码保持英文，说明与校验提示使用中文。"

    def schema():
        if app.openapi_schema:
            return app.openapi_schema
        document = get_openapi(title="ITSM工作流执行器接口", version=app.version, description="节点定义、知识提取、生产执行、运行控制和安全日志接口。", routes=app.routes)
        for path in document["paths"].values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for parameter in operation.get("parameters", []):
                    name = parameter["name"]
                    info = FIELDS.get(name) or FIELDS.get(name.lower()) or FIELDS.get(name.replace("-", "_"))
                    if info:
                        parameter["description"] = info[1]
                        parameter.setdefault("schema", {})["title"] = info[0]
                for code, response in operation.get("responses", {}).items():
                    response["description"] = {"200": "请求处理成功，请继续检查响应中的业务字段", "202": "任务已受理，最终结果通过回报返回", "204": "处理完成，无响应正文", "422": "请求参数类型、格式或约束不正确"}.get(code, "接口响应")
        def translate(value):
            if isinstance(value, dict):
                for name, field in value.get("properties", {}).items():
                    if name in FIELDS:
                        field["title"], field["description"] = FIELDS[name]
                for child in value.values():
                    translate(child)
            elif isinstance(value, list):
                for child in value:
                    translate(child)
        translate(document.get("components", {}))
        command = document.get("components", {}).get("schemas", {}).get("ExecutionCommandRequest", {}).get("properties", {}).get("type")
        if command:
            command["title"] = "控制动作"
            command["description"] = "PAUSE表示请求暂停，CANCEL表示请求取消；受理后仍需等待最终运行事实。"
        app.openapi_schema = document
        return document
    app.openapi = schema
