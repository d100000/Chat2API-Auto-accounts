"""Authenticated, read-only catalogue of the features present in this checkout."""

import re
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import app, templates
from api.local_console import current_session, require_session
from chatgpt.model_catalog import DEFAULT_CHAT_MODEL
import utils.configs as configs


def feature_catalog(settings):
    """Expose an explicit allowlist of switches/counts; never serialize settings."""
    prefix = getattr(settings, "api_prefix", "") or ""
    prefix = "/" + prefix.strip("/") if re.fullmatch(r"[A-Za-z0-9_/-]*", prefix) and prefix.strip("/") else ""
    chat_path = f"{prefix}/v1/chat/completions"
    gateway = getattr(settings, "enable_gateway", False) is True
    token_route = f"{prefix}/tokens"
    gateway_status = "enabled" if gateway else "disabled"
    capabilities = [
        {"id": "chat", "title": "文本对话与流式响应", "state": "implemented", "description": "Chat Completions 支持 SSE 逐段输出和完整 JSON 响应。", "href": "/playground", "action": "打开聊天测试", "condition": "需要可用账号和 API Key；上游可用性以请求结果为准。"},
        {"id": "multimodal", "title": "图片与文件输入", "state": "implemented", "description": "messages 支持 text / image_url 内容数组；图片和文件由服务上传到 ChatGPT。", "href": "/playground", "action": "测试图文输入", "condition": "测试台提供 HTTP(S) 图片 URL；API 还支持 data URL。文件类型和处理能力取决于上游。"},
        {"id": "images", "title": "标准图片生成接口", "state": "implemented", "description": "Images API 支持网页生图桥接与原生 Image2 两条路径，与聊天共用 API Key。", "href": "/connection#examples", "action": "查看图片调用", "condition": "chatgpt-image-latest 使用网页会话并启用历史，不保证原生 Image2 身份；gpt-image-2 默认未启用，需要独立 Codex 授权，当前尚未验证成功。"},
        {"id": "models", "title": "默认模型目录", "state": "implemented", "description": "默认展示 gpt-6-luna、auto、gpt-image-2，以及类型、来源与对应接口。", "href": "/connection", "action": "查看模型目录", "condition": "后台保留完整账号能力发现结果，缓存 5 分钟，失败最多保留 24 小时旧值。默认列出不授予账号权限，原生 Image2 需要独立 Codex 授权。"},
        {"id": "context", "title": "多轮上下文", "state": "implemented", "description": "账号池通过完整 messages 传递上下文；本地测试台自动携带已成功的轮次。", "href": "/playground", "action": "测试多轮对话", "condition": "已有 conversation_id / parent_message_id 仅可配合对应账号直接凭据使用，不能跨账号池调度。"},
        {"id": "gpts-model", "title": "GPTs 自定义助手", "state": "implemented", "description": "model 可填写 g- 开头的 GPT ID，进入源码中的 gizmo_interaction 模式。", "href": "/playground", "action": "输入 GPT ID 测试", "condition": "对应账号必须具有该 GPT 的访问权限；此功能不要求打开网页网关。"},
        {"id": "tools", "title": "网页工具与附件结果", "state": "conditional", "description": "解析上游产生的图片、文件、代码执行和引用结果，作为聊天内容返回。", "href": "#api-examples", "action": "查看接口边界", "condition": "具体工具由上游选择；没有实现 OpenAI tools / tool_calls 函数调用协议。"},
    ]
    inherited = [
        {"title": "原版 ChatGPT 网页网关", "state": gateway_status, "path": "/gateway", "href": "/gateway" if gateway else None, "description": "独立网页入口；登录入口为 /gateway/login。原有网页路由继续保留。"},
        {"title": "GPTs 网页与发现", "state": gateway_status, "path": "/gpts · /g/g-{id}", "href": "/gpts" if gateway else None, "description": "继承 GPTs 列表、详情及相关 backend-api 路由，需要网页登录态。"},
        {"title": "Seed 账号绑定", "state": gateway_status, "path": "GET / POST / DELETE /seedtoken", "href": "#api-examples", "description": "通过 .env 系统 Key 管理 Seed 映射；AUTO_SEED=false 时使用 Seed 作为 Bearer 凭据。返回值可能包含账号凭据，只在可信环境调用。"},
        {"title": "RefreshToken 换取访问凭据", "state": gateway_status, "path": "POST /auth/refresh", "href": "#api-examples", "description": "继承刷新接口；是否成功取决于上游认证和 RefreshToken 的有效性。"},
        {"title": "网页后端转发", "state": gateway_status, "path": "/backend-api/* · /public-api/*", "href": "/gateway" if gateway else None, "description": "包含会话列表、会话详情、分享及其他网页请求的反代，保留原实现。"},
        {"title": "原 admin 模块", "state": "not_implemented", "path": "gateway/admin.py", "href": "/accounts", "description": "当前源码文件为空，没有旧版 admin 路由；账号与额度、凭据、密钥由本工作区统一管理。"},
    ]
    config_specs = [
        ("ENABLE_GATEWAY", "enable_gateway", "注册原网页网关、GPTs 页面、Seed 与刷新接口"),
        ("AUTO_SEED", "auto_seed", "开启时共享 Key 使用账号池；关闭时使用已绑定的 Seed"),
        ("HISTORY_DISABLED", "history_disabled", "聊天请求未单独指定时使用临时会话"),
        ("RANDOM_TOKEN", "random_token", "未进入额度调度时，随机或轮换选择账号"),
        ("ENABLE_LIMIT", "enable_limit", "启用原有模型限流检查"),
        ("UPLOAD_BY_URL", "upload_by_url", "自动解析文本中的文件 URL；结构化 image_url 不依赖此开关"),
        ("CONVERSATION_ONLY", "conversation_only", "跳过独立 chat-requirements 流程"),
        ("FORCE_NO_HISTORY", "force_no_history", "原网页网关强制无历史模式"),
        ("SCHEDULED_REFRESH", "scheduled_refresh", "原有 RefreshToken 定时刷新配置"),
        ("CHECK_MODEL", "check_model", "保留的上游模型检查配置"),
    ]
    configuration = [{"name": name, "enabled": getattr(settings, attr, False) is True, "description": description} for name, attr, description in config_specs]
    examples = [
        {"title": "流式 / 完整聊天", "method": "POST", "path": chat_path, "auth": "YOUR_API_KEY", "body": {"model": DEFAULT_CHAT_MODEL, "messages": [{"role": "user", "content": "请只回复 OK。"}], "stream": True}, "note": "stream=true 返回 SSE；改为 false 返回完整 JSON。临时公网入口不支持流式时应设为 false。"},
        {"title": "原有对话生图", "method": "POST", "path": chat_path, "auth": "YOUR_API_KEY", "body": {"model": "auto", "quota_feature": "image_gen", "history_disabled": False, "messages": [{"role": "user", "content": "请生成一张蓝色圆形图案。"}], "stream": False}, "note": "原有 Chat Completions 生图仍保留；需要标准图片响应结构时使用 /v1/images/generations。普通会话可能在上游保留历史。"},
        {"title": "图文输入", "method": "POST", "path": chat_path, "auth": "YOUR_API_KEY", "body": {"model": DEFAULT_CHAT_MODEL, "messages": [{"role": "user", "content": [{"type": "text", "text": "描述这张图片。"}, {"type": "image_url", "image_url": {"url": "https://example.com/your-image.png", "detail": "auto"}}]}], "stream": False}, "note": "将示例 URL 替换为你有权发送且服务可访问的图片地址。"},
        {"title": "继续指定账号的上游会话", "method": "POST", "path": chat_path, "auth": "ACCOUNT_ACCESS_TOKEN", "body": {"model": "auto", "history_disabled": False, "conversation_id": "YOUR_CONVERSATION_ID", "parent_message_id": "YOUR_PARENT_MESSAGE_ID", "messages": [{"role": "user", "content": "继续。"}], "stream": True}, "note": "必须使用原账号的直接凭据与真实会话 ID。共享 Key 账号池会拒绝这组参数。"},
        {"title": "注册固定 Seed", "method": "POST", "path": "/seedtoken", "auth": "SYSTEM_API_KEY", "body": {"seed": "YOUR_SEED", "token": "ACCOUNT_ACCESS_TOKEN"}, "note": "需要 ENABLE_GATEWAY=true，仅接受 .env 系统密钥，不接受应用密钥。切换 AUTO_SEED=false 后，聊天接口以 YOUR_SEED 作为 Bearer；seed 不是聊天请求体参数。"},
        {"title": "刷新账号访问凭据", "method": "POST", "path": "/auth/refresh", "auth": None, "encoding": "form", "body": {"refresh_token": "YOUR_REFRESH_TOKEN"}, "note": "需要 ENABLE_GATEWAY=true；原接口的请求格式由 gateway/share.py 保留。不要在不可信网络传输 RefreshToken。"},
        {"title": "查询模型目录", "method": "GET", "path": f"{prefix}/v1/models", "auth": "YOUR_API_KEY", "note": "返回 OpenAI list 结构，附 type、source、supported_endpoints。单个模型可用 GET /v1/models/{id} 查询；目录不是每个账号的能力验证。"},
        {"title": "Images API · 网页生图", "method": "POST", "path": f"{prefix}/v1/images/generations", "auth": "YOUR_API_KEY", "body": {"model": "chatgpt-image-latest", "prompt": "生成一张蓝色圆形图案，白色背景。", "n": 1, "size": "auto", "quality": "auto", "output_format": "png", "response_format": "b64_json"}, "note": "网页桥接固定 n=1、PNG，size / quality / background 只接受 auto；response_format 仅支持 b64_json。自动使用生图额度调度并启用会话历史，不保证原生 Image2 身份。"},
        {"title": "Images API · 原生 Image2", "method": "POST", "path": f"{prefix}/v1/images/generations", "auth": "YOUR_API_KEY", "body": {"model": "gpt-image-2", "prompt": "生成一张蓝色圆形图案，白色背景。", "n": 1, "size": "auto", "quality": "auto", "output_format": "png", "response_format": "b64_json"}, "note": "默认未启用，需要独立 Codex 授权，当前尚未验证成功。仅支持 n=1 和 b64_json；不会失败后静默切换为网页生图。网页额度看板不代表 Codex 额度。"},
    ]
    return {"gateway_enabled": gateway, "capabilities": capabilities, "inherited": inherited,
            "configuration": configuration, "examples": examples,
            "management": [{"title": "账号与额度", "href": "/accounts", "description": "额度快照、重置时间、自动调度和本地用量"},
                           {"title": "凭据管理", "href": token_route, "description": "导入、错误凭据、清空账号池及 Seed 映射"},
                           {"title": "API 密钥与接入", "href": "/connection", "description": "接入地址、密钥管理、公网状态与客户端示例"}],
            "boundaries": ["模型目录中的原生聊天标识直接转发上游，auto 由网页选择模型；响应名称不独立证明底层模型身份。", "temperature、top_p、tools、tool_calls、请求体 seed 没有对应的网页聊天上游参数映射。", "已提供模型查询、Chat Completions 和 Images Generations；未实现 /v1/images/edits、/v1/responses。两种图片模型权限和参数不同，不能互相冒充。"],
            "configuration_note": "这里只展示当前进程已读取的开关，不保存修改。修改本机 .env 后需重启服务；页面不会自动开启网关。"}


@app.get("/features", response_class=HTMLResponse, include_in_schema=False)
async def features_page(request: Request):
    if not current_session(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="local_features.html", context={})


@app.get("/local-console/features", include_in_schema=False)
async def features_info(request: Request):
    require_session(request)
    return feature_catalog(configs)
