# Chat2API Auto Accounts

把已登录的 ChatGPT 网页账号接入服务，供支持 OpenAI 格式的客户端查询模型、聊天和生成图片。同一 Base URL 与 API Key 可调用 Chat Completions 和 Images Generations。项目基于 Python / FastAPI，把原版 Chat2API 的聊天、图文和凭据能力，与账号额度、自动调度、API 密钥管理整合到一个管理工作区。

```text
兼容客户端 / 本地测试台
          ↓
GET /v1/models · POST /v1/chat/completions · POST /v1/images/generations
          ↓
按请求类型、额度与并发预留选择可用账号
          ↓
ChatGPT 网页接口 → JSON / SSE 聊天响应或标准图片响应
```

这是基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 修改的非官方项目，不是 OpenAI 官方 API 服务。它使用网页账号的权限和限额，稳定性取决于账号状态、网络及上游网页接口变化。

## 已实现的功能

- **聊天接口**：`POST /v1/chat/completions`，支持流式 SSE 和非流式 JSON 响应。
- **图片接口**：`POST /v1/images/generations`，默认用 `chatgpt-image-latest` 将网页生图转换为标准图片响应，按网页生图额度选账号。另保留原生 `gpt-image-2` 独立通道，默认禁用，需另行具备 Codex 授权。
- **额度调度**：使用本地 API Key 调用账号池时，优先选择额度充足的账号；区分大模型和生图额度，耗尽后切换，重置后经查询确认再加入可用池。
- **Token 池**：从文件或管理页面导入凭据，合并可识别的同账号 Token，并排除已记录的异常或过期凭据。
- **本地管理登录**：使用用户名和 PBKDF2 密码哈希登录，保护测试台和 Token 管理入口。
- **浏览器测试台**：发送消息、临时多轮对话、切换流式响应、查看 HTTP 状态、耗时和首字时间。页面会显示本地 Token 数量与异常标记数量。
- **图片测试**：测试台支持图片 URL 输入和原有聊天生图模式；API 接入页提供标准图片接口示例。网页生图启用会话历史，实际输出取决于上游账号能力。
- **API 密钥管理**：按用途创建独立应用密钥、停用、恢复和撤销，查看最近使用时间与累计受理次数，以及近 30 天 API 等价估值、未估值条数、图片请求和生成张数；密钥明文只在创建时返回一次，数据库只存摘要。
- **API 接入与模型目录**：复制本机或临时公网 Base URL，查询真实聊天模型标识和两种图片模型，查看模型查询、GPT 聊天、图片生成三类 cURL 示例；目录标明类型、来源与对应接口。
- **原功能入口**：统一展示原网页网关、GPTs、Seed 管理、Token 刷新与运行配置；保留继承路由，明确显示是否启用和调用条件。
- **账号与额度**：查看 ChatGPT 网页返回的大模型 `reason`、生图 `image_gen` 剩余量、重置时间，以及其他功能和模型限制；支持单账号或全部账号刷新。
- **调度状态**：查看各能力是否可选、执行中的请求、等待刷新或冷却的原因，以及最近的账号选择、切换、耗尽和恢复事件。
- **账号并发配置**：大模型默认 3、生图默认 1，可按账号独立覆盖、在账号列表批量修改，或在导入时设置；同账号的多份凭据共享上限。
- **数据总览**：按日期、API Key、模型及调用类型筛选独立请求账本，查看每日请求与估值、模型与密钥排行、24 小时分布、请求状态、Token 和耗时；图片请求次数与生成张数分开统计，支持配置估算价格。
- **本地用量记录**：按账号记录调用尝试、成功、失败、取消、中断，以及上游返回的图片资产数量，支持近期统计和模型标签对照。
- **本地运行管理**：提供启动、状态查询、停止脚本，以及从本仓库源码构建的 Docker Compose 配置。

名称中的 Auto Accounts 指已导入 Token 的自动选择，不包含自动注册账号或自动完成网页登录。

## 使用边界

- 聊天建议先使用 `model: "auto"`，或从 `GET /v1/models` 读取当前聊天模型标识。目录中的原生聊天标识直接发送上游；响应里的 `model` 标签不能独立证明底层模型身份，也不保证所有账号均有权限。
- 已提供模型查询、Chat Completions 和 Images Generations；**没有实现 `/v1/images/edits` 或 `/v1/responses` 路由**。网页图片模型 `chatgpt-image-latest` 不保证底层原生 Image2 身份。`gpt-image-2` 默认禁用，需要独立 Codex 授权，当前没有宣称已验证原生调用成功；也不会失败后自动改用网页生图。
- 额度页面展示查询时的上游快照，调度器另行记录本地预留。未返回的额度保持未知，不能用本地调用次数推算官方余额，也不提供“无限使用”能力。
- 测试台的聊天记录只存在于当前页面，刷新或退出后清空。`max_tokens` / `max_completion_tokens` 在本地限制返回文本长度，不代表能控制网页上游的计算量或额度消耗。
- 当前部署要求主服务和可选公网网关分别使用**单进程、单 worker**，不要添加 `--workers` 或多个主服务副本：登录会话、刷新限频和并发控制保存在进程内，本地账本也按单进程重启恢复未完成请求。管理页面固定使用 `127.0.0.1:5005` 或 `localhost:5005`；临时公网只开放独立的 API 网关。

## 本地安装

以下命令适用于 macOS / Linux，使用 Python 3.11。Windows 可使用对应的虚拟环境激活命令，或采用下方 Docker 方式。

```bash
git clone https://github.com/d100000/Chat2API-Auto-accounts.git
cd Chat2API-Auto-accounts
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
mkdir -p data
touch data/token.txt
chmod 700 data
chmod 600 .env data/token.txt
```

### 1. 配置本地 API Key 和管理员密码

生成一个随机本地 API Key：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

将输出填入 `.env` 的 `AUTHORIZATION`。它是客户端调用本地 Token 池使用的密钥，与 ChatGPT 网页 Access Token、管理员密码分别配置。

运行下面的命令，交互输入管理员密码，生成密码哈希配置行：

```bash
python - <<'PY'
import getpass
import hashlib
import secrets

password = getpass.getpass("Console password: ")
if not password:
    raise SystemExit("Password must not be empty")
salt = secrets.token_bytes(16)
rounds = 600000
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds).hex()
print(f"LOCAL_CONSOLE_PASSWORD_HASH='pbkdf2_sha256${rounds}${salt.hex()}${digest}'")
PY
```

用输出的完整配置行替换 `.env` 中的 `LOCAL_CONSOLE_PASSWORD_HASH=''`。**保留哈希外侧的单引号**，避免 Docker Compose 将其中的 `$` 当成变量插值。只保存哈希，不把登录密码写入 `.env`。

`.env.example` 已提供其余配置；首次运行保留以下设置：

```dotenv
AUTHORIZATION=YOUR_LOCAL_API_KEY
API_PREFIX=
LOCAL_CONSOLE_ENABLED=true
LOCAL_CONSOLE_USERNAME=admin
LOCAL_QUOTA_AUTO_REFRESH_SECONDS=300
AUTO_SEED=true
ACCOUNT_QUOTA_SCHEDULING=true
ACCOUNT_MAX_ATTEMPTS=3
RANDOM_TOKEN=false
ENABLE_GATEWAY=false
```

示例中的 `YOUR_LOCAL_API_KEY` 必须替换。当前配置加载器使用 ASCII 读取 `.env`，不要在该文件中加入中文注释或中文配置值。密码本身通过交互输入后转成哈希，不受此限制。

### 2. 导入网页账号凭据

把自己有权使用的 ChatGPT 网页 **Access Token** 写入 `data/token.txt`，每行一个纯 Token，不加引号或 `Bearer` 前缀：

```text
YOUR_CHATGPT_ACCESS_TOKEN
```

这里不能直接填管理员密码、官方 API Key 或浏览器 Session Cookie。项目不提供从 Session Cookie 提取 Access Token 的管理界面。凭据过期后需要更新；直接修改文件后重启服务。

也可以先启动服务并登录，在账号管理页面粘贴导入，页面导入会同步更新运行中的 Token 池。`.env` 和 `data/` 中的凭据与缓存保存在本地，未做静态加密，应保持私有，不要上传或分享这些文件。

### 3. 启动并使用

在仓库根目录、已激活虚拟环境的终端中运行：

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 5005 --no-access-log
```

打开 [本地登录页](http://127.0.0.1:5005/login)，使用 `.env` 中的用户名和刚才设置的密码登录。

- [数据总览](http://127.0.0.1:5005/dashboard)：查看按客户端请求统计的用量、金额估值、图片数量和近期明细。
- [聊天测试台](http://127.0.0.1:5005/playground)：可先点“运行最小测试”，再继续对话。
- [账号与额度](http://127.0.0.1:5005/accounts)：查看剩余额度、调度状态、切换动态和本地调用记录。
- [Token 管理](http://127.0.0.1:5005/tokens)：导入或清空 Token 池。
- [API 接入与密钥](http://127.0.0.1:5005/connection)：创建应用密钥，管理启停，复制调用配置。
- [功能与设置](http://127.0.0.1:5005/features)：查看原项目能力、入口和当前运行配置。

管理员登录状态保存在服务进程内，重启后需要重新登录。测试台显示“已连接”仅表示本地服务可访问；上游是否可用以聊天请求结果为准。

macOS / Linux 也可选择后台运行脚本，要求虚拟环境位于 `.venv`，并安装了 `ps`、`lsof`：

```bash
python scripts/local_service.py start
python scripts/local_service.py status
python scripts/local_service.py stop
```

后台日志位于 `data/server.log`。前台命令和后台脚本选一种启动即可。

## API 密钥与外部调用

管理员密码用于登录后台；网页账号凭据用于访问 ChatGPT；API 密钥用于让客户端调用本服务，三者不同。

在 `/connection` 的「密钥管理」页签中，按客户端用途创建应用密钥，例如“云服务器”或“本地客户端”。新密钥只在创建弹窗出现一次，请复制保存。之后列表只显示掩码，可立即停用或恢复；撤销不可恢复。密钥保存在 `data/api-keys.sqlite3`，权限为 `0600`，只存随机密钥的 SHA-256 摘要和管理元数据。

每个应用密钥使用同一账号池；网页聊天与网页生图共用额度调度。列表的「受理次数」统计通过鉴权、格式及通道检查后受理的聊天或图片生成请求，包括后续失败的调用，不等于成功次数或官方计费次数；模型查询不计数，默认禁用的原生图片请求也不计生成。已有 `.env` 的 `AUTHORIZATION` 系统密钥继续兼容，在页面单独显示，须编辑环境配置后重启才能更换。应用密钥的启停、撤销无需重启，已在执行中的请求不会被强制中断。

```bash
export CHAT2API_KEY='YOUR_APPLICATION_KEY'
curl http://127.0.0.1:5005/v1/chat/completions \
  -H "Authorization: Bearer $CHAT2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

### 临时 HTTPS 联调

安装 [Cloudflare cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)，先启动主服务，再运行：

```bash
python scripts/public_service.py start
python scripts/public_service.py status
python scripts/public_service.py stop
```

脚本启动仅监听 `127.0.0.1:5006` 的独立 API 网关，再创建随机 `https://*.trycloudflare.com` 地址。实际 Base URL 会显示在 `/connection`；每次重新创建隧道可能变化。Mac、主服务和隧道都需在线，停止隧道后地址失效。此方式用于联调，不提供生产可用性保证。

公网仅开放带 API Key 的 `GET /v1/models`、`GET /v1/models/{id}`、`POST /v1/chat/completions` 和 `POST /v1/images/generations`，拒绝直接网页账号凭据；后台、账号、凭据、Seed 与文档路由不会通过此网关公开。网关每次读取应用密钥状态，撤销立即影响后续请求。单请求正文上限 2 MiB，同时最多受理 10 个请求，不自动重放生成请求。公网图文输入只接受内联 data:image（PNG/JPEG/WebP/GIF），每图解码后不超过 1 MiB；HTTP(S) 图片 URL 输入保留在私有本机服务。公网启动要求 UPLOAD_BY_URL=false，防止外部请求让服务读取本机或内网文件。

[Cloudflare Quick Tunnel 不支持 SSE](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)，公网联调必须使用 `stream:false`；`stream:true` 会明确返回 400。本机流式聊天仍可正常使用。不要直接把完整的 5005 服务转发到公网。

## 数据总览与金额估值

`/dashboard` 支持近 7 / 30 / 90 天，或最长 366 天的自定义日期范围；可按 API Key、请求模型和聊天 / 图片类型筛选。日期包含首尾，按页面显示的时区划分自然日；24 小时图是所选日期范围内各时段请求量的合计。密钥管理列表另展示近 30 天的 API 等价估值，以及图片请求次数与成功生成张数，可直接进入该密钥明细。

数据总览使用 `chatgpt/analytics.py` 的独立客户端请求账本，存于 `data/request_usage.sqlite3`。一个客户端生成请求只记一次，内部换账号重试不会重复计入总请求；账号页仍按执行尝试统计。模型查询不计生成请求。账本仅保存时间、匿名密钥标识、模型标识、状态、数量、耗时与估价元数据，不保存提示词、回复、图片 URL 或凭据。

页面显示实际采集起点。启用前的账号执行记录缺少 API Key、Token 或价格信息，**不回填历史金额**；密钥原有的累计受理次数因此可能高于新账本请求数。失败、取消、中断和执行中请求分别展示；成功率以全部已记录请求为分母。

金额均为 **API 等价估值（USD），不是 OpenAI 账单、网页账号可用余额或真实支出**。已知估值单独求和，缺失价格或数量的请求标记“未估值”，同时显示估价覆盖率。Token 为可得部分，其中网页聊天主要使用本地分词估算；缺失值保留未知。图片请求次数与成功生成张数独立统计，聊天内有图片证据的请求也会计入。

展开「估价设置」可查看有效参考价，按准确模型标识添加文本每百万 Token 价或图片每张价。`auto` 和网页模型不会自动套用相似名称的官方 API 价格；没有可信映射时保持未估值。自定义项按“模型 + 类型”覆盖参考价，删除后恢复参考价；空值不会被当作零。配置存于 `data/pricing.json`，请求开始时固定价格版本，**保存新价格只影响之后开始的请求，历史记录不重新计算**。这些运行数据应保持私有，不提交到 Git。

## 原版功能与统一后台

本仓库是在 `lanqian528/chat2api` 上增加管理功能，不是 Electron 多供应商桌面项目。原有聊天协议转换、模型映射、图文消息、网页工具结果解析、网页会话参数、Token 管理、Refresh Token 刷新、Seed 和网页网关代码均保留；`/features` 按当前配置列出入口和限制。

- 普通聊天和原有对话生图可在 `/playground` 直接测试；图文输入支持 Chat Completions 的 `text` / `image_url` 消息。原有生图模式使用 `history_disabled:false`，同时新增独立 Images Generations 路由；两种入口都保留。
- `/tokens` 提供批量导入、错误凭据查看、清空账号池及清空 Seed 会话映射。清空操作需要在页面确认。
- 账号详情分为「概览、调度、更多额度、模型记录」，原有信息仍保留，默认只展示常用概览。
- 继承的网页网关默认 `ENABLE_GATEWAY=false`。启用并重启后，`/gateway`、`/gateway/login` 为原网页入口，`/gpts`、`/seedtoken`、`/auth/refresh` 等继承路由生效；管理员仍使用 `/login`，无需覆盖后台入口。网关使用继承的网页资源，上游兼容性需按实际账号验证。
- 共享账号池使用 messages 重建多轮上下文，不接受显式 `conversation_id`、`parent_message_id` 或工作区覆盖，以免把会话切换到错误账号；需要这些原参数时使用原有直接账号凭据调用方式，仅限私有本机服务。

## 账号额度与本地用量

登录后打开 `/accounts`，可以按账号标签或邮箱搜索、筛选状态、展开详情，并刷新单个或全部账号。列表使用紧凑的横向表格，每格最多两行，分别展示邮箱、套餐、授权状态、大模型与生图余额、各自的重置时间、并发和近期调用。桌面固定账号与操作列，窄屏只固定账号列并支持横向滚动；「显示列」可以选择所需列并恢复默认，设置仅在当前浏览器保存列标识，不保存邮箱或账号资料。

邮箱、Free / Plus / Pro 等套餐和 Token 签发、到期时间来自当前选中凭据的 JWT 声明，不额外请求上游个人资料。详情会显示来源；无法解析、缺失或工作区不匹配的套餐保持未知，不按额度推算等级。JWT 签名未在本地验证，声明不等于当前订阅或实时授权证明；Token 到期也不等于订阅到期。最近验证时间仅来自对同一份凭据、同一工作区成功查询额度的记录，更换凭据后需重新验证；旧缓存无法关联时显示未验证。

账号仍使用匿名稳定标签；能够识别的工作区与用户组合用于合并同一账号的多个 Token，同一工作区内的不同用户分别统计。邮箱与套餐仅通过已登录的本机管理接口返回，不进入公开模型目录、调度状态或用量账本，也不额外写入额度缓存。

这部分由四个模块组成：

| 层次 | 实现与存储 | 数据含义 |
| --- | --- | --- |
| 上游额度快照 | `chatgpt/account_quota.py` → `data/account_quota.json` | 只读查询 ChatGPT 网页 `POST /backend-api/conversation/init`，保留允许的额度字段；不发送聊天或生图消息 |
| 本地请求账本 | `chatgpt/account_usage.py` → `data/account_usage.sqlite3` | 记录经过本机服务的每次账号执行尝试；重试单独计数，不回填启用前的历史 |
| 账号调度 | `chatgpt/account_scheduler.py`、`chatgpt/scheduling.py` → `data/account_scheduler.json` | 选择账号、预留并发容量、记录临时扣减与阻断状态；在安全条件下切换账号 |
| 管理页面 | `/accounts`、`api/local_console.py` | 合并快照、账本和调度状态；“更新本地视图”读取本地记录，“刷新额度”请求上游元数据 |

这些数据文件均保存在私有 `data/` 目录，文件权限为 `0600`。额度缓存与调度状态只使用匿名账号标识；调度状态和用量账本不保存 Token、提示词、回复正文、图片 URL 或原始错误正文。不要把运行时 `data/` 提交到仓库。

模型目录由 `chatgpt/model_catalog.py` 维护，私密缓存位于 `data/model_catalog.json`，按匿名账号与通道分别保存经过校验的模型标识、查询状态及时间。公开 `/v1/models` 汇总可路由条目，不暴露账号身份；查询直接读取缓存，后台最多并发 2、每账号两次尝试至少间隔 60 秒。正常目录缓存 5 分钟，失败保留最长 24 小时的上次成功结果；过期或错误凭据不会放行现代模型请求。

账号页分别展示目录发现与 `gpt-6-luna` 调用核查证据。内部管理员核查只向指定账号发送一次固定短提示词，受账号额度和并发限制，每账号至少间隔 60 秒，不重试、不改写模型、不自动切换账号。只有实际发送与上游报告都精确为 `gpt-6-luna` 才记为调用一致；HTTP 200 但报告其他模型会显示不一致，不能当作 Luna 可用。核查记录不包含回复正文，超过 24 小时显示过期。上游报告仍不构成底层模型身份的独立证明。

### 刷新与时间口径

启用本地控制台后，服务启动时开始监测额度。后台每 15 秒检查刷新条件，正常快照默认每 5 分钟更新；额度到期、调度预算用完或认证状态待确认时可提前查询。无论手动、后台还是按需刷新，同一账号两次尝试至少间隔 60 秒，全局最多同时查询 2 个账号。

设置 `LOCAL_QUOTA_AUTO_REFRESH_SECONDS=0` 停用后台额度请求；手动刷新仍可使用，调用时没有可选账号也会尝试一次受限的按需刷新。其他非零值最小为 60 秒。本地页面刷新只读取缓存，不会每次都请求上游。

- **最新快照**：上次成功查询距今不足 5 分钟，且没有已到重置时间的功能或后续查询失败。
- **待刷新 / 查询失败**：快照超过 5 分钟、功能已到重置时间，或后续请求失败。页面保留旧值并标记，不把失败改成剩余 0。
- **凭据过期**：本地解析的 JWT 有效期已结束；“声明有效期内”也不保证上游一定接受凭据。没有有效期字段时显示未知。
- **重置时间**：来自上游明确返回的时间，页面按浏览器本地时区显示。倒计时结束只表示需要重新查询，不会自动补满余额。

`reason` 和 `image_gen` 是上游功能额度标识，不是对底层模型身份的确认。总量与已用量只有在上游明确提供时才展示；缺失字段显示未知。这里查询的是 **ChatGPT 网页额度，不是 Codex / Work 额度**，不会套用固定“5 小时 / 每周”额度或重置规则。

### 本地统计与模型标签

页面中的近 5 小时、24 小时、7 天和累计数据，都是**本地调用统计范围**，不是上游配额周期。它们不包含绕过本服务的网页、手机或其他客户端用量，也不等于官方额度消耗。生图调用来自观察到的工具事件；图片数量按每次请求内已完成的图片资产标识去重，不按文字承诺或预览计数，也不代表已下载验证的图片文件数量。记录失败时页面会提示统计不完整。

模型使用详情区分三个标签：

| 标签 | 字段 | 含义 |
| --- | --- | --- |
| 请求模型（requested） | `requested_model` | 客户端填写的模型名称 |
| 发送上游模型（sent） | `sent_model` | 经本地别名或回退逻辑处理后发送的名称 |
| 上游声明模型（observed） | `upstream_model` | 从上游消息元数据观察到的名称；未返回时保持未知 |

三种标签都不能独立证明真实底层模型身份。成功、失败、取消和中断按本次请求过程记录，也不用于推断上游是否扣减了额度。

## 自动账号调度

默认启用 `ACCOUNT_QUOTA_SCHEDULING=true`。只有同时满足 `AUTO_SEED=true`、使用 `AUTHORIZATION` 中的本地共享 Key 调用 `/v1/chat/completions` 时，才进入额度调度。直接传入上游凭据或使用固定 Seed 时保持原有账号绑定，不自动跨账号重放生成请求。关闭额度调度后，账号池使用原有轮换／随机选择逻辑。

### 账号并发设置

系统默认每账号大模型并发 **3**、生图并发 **1**，两类分别允许设置为 1–10 的整数。账号页可修改系统默认、设置单账号，或勾选当前筛选范围批量修改；每类独立支持保持不变、继承默认、自定义。修改默认只影响继承该项的账号。配置存于私密 `data/account_concurrency.json`，包含匿名账号、覆盖值、凭据摘要关联和修订号，不保存 Token。

导入页面默认继承系统默认，可为本批新账号指定覆盖值。重复凭据会去重，同账号新增凭据默认保留已有配置，只有明确选择覆盖才修改。导入使用临时私密文件和仅含元数据的恢复日志，在设置与凭据都可提交后才发布新账号。Refresh Token 从临时身份归并到可识别账号时，配置与执行中计数一起归并；冲突先采用较小上限并提示。

并发槽按提交时的请求类型分别计算：普通聊天占大模型槽，标准生图及显式生图占生图槽；网页生图仍检查和预留两种额度。普通聊天中由上游自动触发的生图保留原有能力，无法仅凭提示词保证事先识别所有工具调用。固定账号直连、Seed 和原网页对话入口也执行容量限制，不切换已绑定账号；原生 Codex 图片使用实际授权身份与独立容量租约，不扣网页额度。

满载账号会被跳过；全部满载返回 `429 account_concurrency_exhausted` 和短 `Retry-After`，不触发额度刷新。降低上限只阻止新请求，当前请求继续结束，页面可能暂时显示 `5 / 2 · 等待现有请求结束`。流式完成、异常或断连会关闭本地上游连接后释放槽。并发不是 RPM 或剩余额度，公网网关的全局 **10** 并发上限仍独立生效。

### 额度选择规则

调度规则如下：

1. 大模型请求检查 `reason`；生图请求同时检查 `reason` 和 `image_gen`。
2. 优先选择新鲜、已知正余额的账号，按扣除并发预留和临时扣减后的可用容量排序；生图以两种能力中较小的容量为准。
3. 没有这类账号时，才尝试余额未知或快照过期的账号，每个账号最多一个此类请求同时执行。已经确认耗尽或被上游阻断的能力不会进入这个后备池。
4. 已知余额不足时切换账号；明确的额度拒绝会阻断对应能力。普通限频进入短暂冷却，不直接改写为剩余 0。认证失败需要后续成功查询确认恢复。
5. 重置时间到达后先刷新；只有查询开始时间晚于阻断事件、不早于重置时间，且返回新的正余额，才重新加入该能力的可用池。

预留用于避免并发请求同时占用最后一份容量。每次已发送到上游的尝试，暂记一份大模型容量；请求生图时再暂记一份生图容量，**不按生成的图片资产数扣减**。这些临时记录会保留到一次在请求结束后才开始的额度查询返回数值余额。页面的“调度估算”与上游“剩余量”分开展示：前者是本地保守调度依据，不是官方计费或额度消耗记录。

`ACCOUNT_MAX_ATTEMPTS` 默认是 `3`，有效范围为 `1`–`10`，表示一次账号池调用最多尝试几个不同账号。只有尚未发送生成请求，或上游明确拒绝且没有输出等可安全重试的情况，才允许切换。响应流已经开始、已有部分输出或发送后的连接状态不明时，不重新生成；后续请求会按更新后的调度状态选账号。没有可选账号时返回带 `Retry-After` 的 `429`，不会循环发送。

后台展示各账号的大模型／生图可选状态、预留数、执行中请求和冷却／重置等待原因。调度文件最多保留最近 100 条事件，页面显示最近 20 条。重启保留阻断与临时扣减；未完成的预留按可能已经发送处理，等待新额度查询确认。调度仍要求单进程、单 worker。

### 指定请求类型

请求体支持本项目扩展参数 `quota_feature`：`auto`（默认）、`reason` 或 `image_gen`。自动模式根据模型名、工具选择和最后一条用户提示进行尽力识别，不能保证识别所有表达。需要生图时建议明确填写 `"quota_feature":"image_gen"`，让账号选择同时检查生图额度；这个参数不会把聊天接口转换为标准 Images API，也不保证上游一定执行生图。

```json
{
  "model": "auto",
  "quota_feature": "image_gen",
  "messages": [{"role": "user", "content": "请生成一张蓝色圆形图案。"}],
  "stream": true
}
```

账号池请求应传完整 `messages`。携带已有 `conversation_id`、`parent_message_id` 或 `Chatgpt-Account-Id` 工作区覆盖的共享 Key 请求会返回 `400`，避免把已有会话发给另一个账号；需要继续这类上游会话时，请使用对应账号的直接凭据。

如果上游提示临时聊天无法生图，可为该次请求显式设置 `"history_disabled": false` 使用普通聊天。此设置可能让上游保留会话历史；调度器不会自动修改这个选项。

## 接入客户端

| 配置项 | 值 |
| --- | --- |
| API 类型 | OpenAI Chat Completions / Images Generations |
| Base URL | `http://127.0.0.1:5005/v1` |
| API Key | 在「API 接入与密钥」创建应用密钥，或使用 `AUTHORIZATION` 系统密钥 |
| 聊天模型 | 默认 `auto`；动态列表见 `GET /v1/models` |
| 图片模型 | 默认 `chatgpt-image-latest`；原生 `gpt-image-2` 默认禁用、需独立 Codex 授权 |

### 模型查询

以下三个示例共用一组连接参数。运行在云服务器时，把 Base URL 换为后台显示的实际 HTTPS 公网地址；不要填写云服务器自己的 `127.0.0.1`。

```bash
export CHAT2API_BASE_URL='http://127.0.0.1:5005/v1'
export CHAT2API_API_KEY='YOUR_APPLICATION_KEY'
curl "${CHAT2API_BASE_URL}/models" \
  -H "Authorization: Bearer ${CHAT2API_API_KEY}"
```

返回 OpenAI `{"object":"list","data":[...]}` 结构。每个条目附带 `type`、`source`、`supported_endpoints`、`availability` 和说明；还可通过 `GET /v1/models/{id}` 查询单个条目。聊天目录来自一个可用账号的只读上游查询；没有账号或查询失败时仍返回 `auto` 和两个图片入口，不代表这些入口已获授权。不要在客户端写死某一次查询得到的 GPT 列表。

### GPT 聊天

```bash
curl "${CHAT2API_BASE_URL}/chat/completions" \
  -H "Authorization: Bearer ${CHAT2API_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

本机调用可将 `stream` 改为 `true` 并为 cURL 加上 `-N`；临时公网入口保持 `stream:false`。聊天模型可替换为目录中 `type:chat` 的标识，图片模型应调用下方图片接口。

聊天支持 `max_completion_tokens` 或 `max_tokens`（二者不能同时传入，正整数 1–1,000,000），用于限制本地返回的文本 Token；达到上限时返回 `finish_reason:"length"`，不承诺减少上游计算或账号额度消耗。仅支持 `n:1`。

本机流式调用可传 `"stream_options":{"include_usage":true}`：成功完成时，在 `[DONE]` 前返回 `choices:[]` 的用量块；普通内容块包含 `usage:null`。这些用量主要是本地分词估算，不是官方计费量。非流式请求不接受非空 `stream_options`。

当前桥接不支持客户端工具调用参数（如 `tools` / `tool_choice`）或 JSON / JSON Schema 结构化输出；请求这些能力会返回 `400 unsupported_parameter`。聊天 `response_format` 只接受省略或 `{"type":"text"}`。这不影响下方 Images 接口独立的 `response_format:"b64_json"` 参数，也不代表支持完整 OpenAI 或 Sub2API 协议。

### 图片生成

图片接口未指定 `model` 时默认使用网页桥接模型 `chatgpt-image-latest`；示例中显式写出名称，方便客户端区分通道。接口接收标准图片请求，内部使用普通 ChatGPT 会话并按 `image_gen` 额度调度；响应仅提供 `data[].b64_json`，由服务端下载完成的图片后编码返回，不把需要账号鉴权的上游图片地址交给外部客户端。普通会话可能在上游保留历史，网页生图结果不保证底层原生 Image2 身份。

```bash
curl "${CHAT2API_BASE_URL}/images/generations" \
  -H "Authorization: Bearer ${CHAT2API_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "chatgpt-image-latest",
    "prompt": "生成一张简洁的蓝色圆形图案，白色背景。",
    "n": 1,
    "size": "auto",
    "quality": "auto",
    "output_format": "png",
    "response_format": "b64_json"
  }'
```

| 参数 / 行为 | `chatgpt-image-latest` 网页生图 | `gpt-image-2` 原生通道 |
| --- | --- | --- |
| 当前入口 | 网页桥接；实际能力取决于账号 | 默认禁用，需独立 Codex 授权；未宣称已验证成功 |
| `n` | 仅 `1` | 仅 `1` |
| `size` | 仅 `auto` | `auto` 或接口校验通过的分辨率 |
| `quality` | 仅 `auto` | `auto` / `low` / `medium` / `high` |
| `background` | 仅 `auto` | `auto` / `opaque` / `transparent` |
| `output_format` | 仅 `png` | `png` / `jpeg` / `webp` |
| `response_format` | 仅 `b64_json` | 仅 `b64_json` |
| 额度 | 网页 `reason` + `image_gen` | Codex 权限与额度独立，不读取或扣减网页额度 |

原生通道需要显式请求 `model:"gpt-image-2"`，并明确配置 `CODEX_NATIVE_IMAGES_ENABLED=true` 后才尝试；开关本身不会授予账号权限。未启用时返回 `403 unsupported_credentials`，不占用网页额度预留，也不计生成请求。原生失败不会静默切换成网页生图。不要为了让网页生图可用而打开原生开关。

客户端可使用同一 API Key 分别发送聊天与图片请求。不要自动重放超时的生成请求，以免重复执行。图片响应需在调用方解码 `b64_json` 后显示或保存；标准 Images 接口不支持 `response_format:"url"`。

主服务保持本机监听，并保持 `LOCAL_CONSOLE_ENABLED=true`；关闭本地控制台也会关闭它对旧 Token 管理路由的登录保护。外部客户端使用上面的独立公网网关。

## Docker 部署

先按上面的说明准备 `.env`、管理员密码哈希和 `data/token.txt`，再运行：

```bash
docker compose up -d --build
docker compose logs -f chat2api
```

停止服务：

```bash
docker compose down
```

仓库中的 `docker-compose.yml` 从当前源码构建 `chat2api-auto-accounts:local`，将 `data/` 挂载到容器，并且仅在宿主机 `127.0.0.1:5005` 暴露服务。访问地址与本地安装一致；使用上游预构建镜像不会包含本仓库新增的登录页和测试台。

## 常用配置

下表为 `.env.example` 的建议值，修改后需要重启服务。

| 变量 | 示例值 | 作用 |
| --- | --- | --- |
| `AUTHORIZATION` | 自行生成的随机值 | 本地 Token 池调用密钥；多个值用英文逗号分隔 |
| `CHATGPT_BASE_URL` | `https://chatgpt.com` | 网页上游地址 |
| `PROXY_URL` | 留空 | 上游请求使用的代理地址，按网络需要设置 |
| `IMPERSONATE` | `["chrome123"]` | `curl_cffi` 使用的浏览器指纹配置 |
| `AUTO_SEED` | `true` | 使用已导入的 Token 池选择逻辑 |
| `ACCOUNT_QUOTA_SCHEDULING` | `true` | 为使用共享本地 Key 的账号池聊天请求启用额度调度 |
| `ACCOUNT_MAX_ATTEMPTS` | `3` | 一次账号池请求最多尝试的不同账号数，限制在 1–10；仅在安全条件下切换 |
| `RANDOM_TOKEN` | `false` | 额度调度未启用时，`false` 轮换选择，`true` 随机选择；轮换顺序不保证与文件行序一致 |
| `HISTORY_DISABLED` | `true` | 向网页上游请求禁用历史记录，不代表本地文件自动清理 |
| `SCHEDULED_REFRESH` | `false` | 关闭继承的 Refresh Token 定时刷新流程；不会自动更新导入的 Access Token |
| `API_PREFIX` | 留空 | API 路径前缀；本地测试台建议保持留空 |
| `ENABLE_GATEWAY` | `false` | 关闭继承的网页网关功能 |
| `CODEX_NATIVE_IMAGES_ENABLED` | `false` | 默认关闭独立原生 Image2 通道；网页生图不依赖此开关，开启也不授予 Codex 权限 |
| `LOCAL_CONSOLE_ENABLED` | `true` | 启用本地登录、测试台及 Token 管理路由保护 |
| `LOCAL_QUOTA_AUTO_REFRESH_SECONDS` | `300` | 正常额度快照的刷新间隔秒数；到期或待确认状态可提前刷新，`0` 停用后台请求，非零最小 60 秒 |
| `LOCAL_CONSOLE_USERNAME` | `admin` | 管理员用户名 |
| `LOCAL_CONSOLE_PASSWORD_HASH` | 生成的带单引号哈希 | 管理员密码校验；留空时无法登录 |

遇到上游认证失败、限额或超时，应结合响应和服务日志检查凭据、账号状态及网络。更换模型标签或反复重试不能保证解决上游限制。

继承的 `RETRY_TIMES` 配置不再控制 `/v1/chat/completions`；当前源码没有调用旧重试辅助函数，建议使用上述 `ACCOUNT_MAX_ATTEMPTS` 配置。

## 开发验证

界面参考 Animate UI 的中性色与交互规范，提供桌面侧栏、移动抽屉导航、浅色/深色主题、滑动页签和统一控件。由 Jinja 模板和本地 JavaScript / CSS 构成，六个工作区页面共用导航、控件、颜色和动效；Outfit 字体随项目自托管。交互与适配约定见 [界面设计规范](DESIGN.md)。无需安装前端构建工具，也不依赖外部样式 CDN。

安装依赖后，可运行全部离线测试：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_dashboard_ui.cjs
node tests/test_connection_ui.cjs
node tests/test_account_concurrency_ui.cjs
node tests/test_account_columns_ui.cjs
node tests/test_tokens_concurrency_ui.cjs
for file in static/local-console/*.js; do node --check "$file"; done
```

测试使用合成账号、临时缓存与数据库；HTTP 测试在临时目录启动独立服务并拦截上游请求，不读取真实账号文件。覆盖登录与访问控制、输入校验、额度去重与限频、旧快照保留、本地请求及图片资产统计、流式请求终态，以及账号切换、并发预留、重置确认、失败分类和重启恢复。GitHub Actions 会运行语法编译检查和全部测试；这些检查不验证真实账号的上游可用性。

## 来源与许可证

本项目基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 的 [a63a90c](https://github.com/lanqian528/chat2api/commit/a63a90cec90dea3fea3e272d22f8c5799de14e30) 源码，保留其 ChatGPT 网页接口适配与 Token 管理代码，并加入本地管理员登录、聊天测试台、额度快照、账号调度、本地用量记录和运行辅助功能。本仓库以当前源码快照作为初始版本。

项目遵循 [MIT License](LICENSE)，保留原许可证中的 `Copyright (c) 2024 aurora-develop` 声明。使用时请遵守相关服务条款，并仅使用有权访问的账号和凭据。
