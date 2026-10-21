# Chat2API Auto Accounts

把已登录的 ChatGPT 网页账号接入本地服务，供支持 OpenAI Chat Completions 格式的客户端调用。项目基于 Python / FastAPI，按大模型和生图剩余额度选择账号，并提供管理员登录、聊天测试台、用量统计和调度状态页面。

```text
兼容客户端 / 本地测试台
          ↓
POST /v1/chat/completions
          ↓
按请求类型、额度与并发预留选择可用账号
          ↓
ChatGPT 网页接口 → 转换为 JSON / SSE 聊天响应
```

这是基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 修改的非官方项目，不是 OpenAI 官方 API 服务。它使用网页账号的权限和限额，稳定性取决于账号状态、网络及上游网页接口变化。

## 已实现的功能

- **聊天接口**：`POST /v1/chat/completions`，支持流式 SSE 和非流式 JSON 响应。
- **额度调度**：使用本地 API Key 调用账号池时，优先选择额度充足的账号；区分大模型和生图额度，耗尽后切换，重置后经查询确认再加入可用池。
- **Token 池**：从文件或管理页面导入凭据，合并可识别的同账号 Token，并排除已记录的异常或过期凭据。
- **本地管理登录**：使用用户名和 PBKDF2 密码哈希登录，保护测试台和 Token 管理入口。
- **浏览器测试台**：发送消息、临时多轮对话、切换流式响应、查看 HTTP 状态、耗时和首字时间。页面会显示本地 Token 数量与异常标记数量。
- **账号与额度**：查看 ChatGPT 网页返回的大模型 `reason`、生图 `image_gen` 剩余量、重置时间，以及其他功能和模型限制；支持单账号或全部账号刷新。
- **调度状态**：查看各能力是否可选、执行中的请求、等待刷新或冷却的原因，以及最近的账号选择、切换、耗尽和恢复事件。
- **本地用量记录**：按账号记录调用尝试、成功、失败、取消、中断，以及上游返回的图片资产数量，支持近期统计和模型标签对照。
- **本地运行管理**：提供启动、状态查询、停止脚本，以及从本仓库源码构建的 Docker Compose 配置。

名称中的 Auto Accounts 指已导入 Token 的自动选择，不包含自动注册账号或自动完成网页登录。

## 使用边界

- 建议先使用 `model: "auto"`。代码保留了一些模型别名与回退逻辑；响应里的 `model` 标签不能证明实际使用了同名底层模型，也不代表该模型当前可用。
- 默认配置的兼容范围以现有聊天接口为准。**没有实现标准 `/v1/images/generations`、`/v1/images/edits` 或 `/v1/responses` 路由**，也没有供客户端自动发现模型的 `/v1/models` 路由。上游聊天可能返回图片内容，这不等于提供标准 Images API。
- 额度页面展示查询时的上游快照，调度器另行记录本地预留。未返回的额度保持未知，不能用本地调用次数推算官方余额，也不提供“无限使用”能力。
- 测试台的聊天记录只存在于当前页面，刷新或退出后清空。`max_tokens` 等参数不保证被网页上游严格执行。
- 当前部署要求**单进程、单 worker**，不要添加 `--workers` 或多个服务副本：登录会话、刷新限频和并发控制保存在进程内，本地账本也按单进程重启恢复未完成请求。管理页面固定使用 `127.0.0.1:5005` 或 `localhost:5005`；公网或多实例部署需要另行设计访问控制和协调机制。

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

- [聊天测试台](http://127.0.0.1:5005/playground)：可先点“运行最小测试”，再继续对话。
- [账号与额度](http://127.0.0.1:5005/accounts)：查看剩余额度、调度状态、切换动态和本地调用记录。
- [Token 管理](http://127.0.0.1:5005/tokens)：导入或清空 Token 池。

管理员登录状态保存在服务进程内，重启后需要重新登录。测试台显示“已连接”仅表示本地服务可访问；上游是否可用以聊天请求结果为准。

macOS / Linux 也可选择后台运行脚本，要求虚拟环境位于 `.venv`，并安装了 `ps`、`lsof`：

```bash
python scripts/local_service.py start
python scripts/local_service.py status
python scripts/local_service.py stop
```

后台日志位于 `data/server.log`。前台命令和后台脚本选一种启动即可。

## 账号额度与本地用量

登录后打开 `/accounts`，可以搜索账号、筛选状态、展开详情，并刷新单个或全部账号。账号以匿名标签显示；能够识别的工作区与用户组合用于合并同一账号的多个 Token，同一工作区内的不同用户分别统计。JWT 声明只用于标签、去重和有效期提示，不作为认证成功的证明。

这部分由四个模块组成：

| 层次 | 实现与存储 | 数据含义 |
| --- | --- | --- |
| 上游额度快照 | `chatgpt/account_quota.py` → `data/account_quota.json` | 只读查询 ChatGPT 网页 `POST /backend-api/conversation/init`，保留允许的额度字段；不发送聊天或生图消息 |
| 本地请求账本 | `chatgpt/account_usage.py` → `data/account_usage.sqlite3` | 记录经过本机服务的每次账号执行尝试；重试单独计数，不回填启用前的历史 |
| 账号调度 | `chatgpt/account_scheduler.py`、`chatgpt/scheduling.py` → `data/account_scheduler.json` | 选择账号、预留并发容量、记录临时扣减与阻断状态；在安全条件下切换账号 |
| 管理页面 | `/accounts`、`api/local_console.py` | 合并快照、账本和调度状态；“重新载入”读取本地记录，“刷新额度”请求上游元数据 |

这些数据文件均保存在私有 `data/` 目录，文件权限为 `0600`。额度缓存与调度状态只使用匿名账号标识；调度状态和用量账本不保存 Token、提示词、回复正文、图片 URL 或原始错误正文。不要把运行时 `data/` 提交到仓库。

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
| API 类型 | OpenAI Chat Completions |
| Base URL | `http://127.0.0.1:5005/v1` |
| API Key | `.env` 中设置的 `AUTHORIZATION` |
| 模型 | 手动填写 `auto` |

调用示例，将占位符替换成本地 API Key：

```bash
export CHAT2API_KEY='YOUR_LOCAL_API_KEY'
curl -N http://127.0.0.1:5005/v1/chat/completions \
  -H "Authorization: Bearer ${CHAT2API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

需要非流式响应时，将 `stream` 改为 `false`。本地 Key 用于选择 Token 池，继承的接口还允许直接传入上游凭据，因此不要把它当作完整的公网调用者权限系统。保持本机监听，并保持 `LOCAL_CONSOLE_ENABLED=true`；关闭本地控制台也会关闭它对旧 Token 管理路由的登录保护。

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
| `LOCAL_CONSOLE_ENABLED` | `true` | 启用本地登录、测试台及 Token 管理路由保护 |
| `LOCAL_QUOTA_AUTO_REFRESH_SECONDS` | `300` | 正常额度快照的刷新间隔秒数；到期或待确认状态可提前刷新，`0` 停用后台请求，非零最小 60 秒 |
| `LOCAL_CONSOLE_USERNAME` | `admin` | 管理员用户名 |
| `LOCAL_CONSOLE_PASSWORD_HASH` | 生成的带单引号哈希 | 管理员密码校验；留空时无法登录 |

遇到上游认证失败、限额或超时，应结合响应和服务日志检查凭据、账号状态及网络。更换模型标签或反复重试不能保证解决上游限制。

继承的 `RETRY_TIMES` 配置不再控制 `/v1/chat/completions`；当前源码没有调用旧重试辅助函数，建议使用上述 `ACCOUNT_MAX_ATTEMPTS` 配置。

## 开发验证

安装依赖后，可运行全部离线测试：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

测试使用合成账号、临时缓存与数据库；HTTP 测试在临时目录启动独立服务并拦截上游请求，不读取真实账号文件。覆盖登录与访问控制、输入校验、额度去重与限频、旧快照保留、本地请求及图片资产统计、流式请求终态，以及账号切换、并发预留、重置确认、失败分类和重启恢复。GitHub Actions 会运行语法编译检查和全部测试；这些检查不验证真实账号的上游可用性。

## 来源与许可证

本项目基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 的 [a63a90c](https://github.com/lanqian528/chat2api/commit/a63a90cec90dea3fea3e272d22f8c5799de14e30) 源码，保留其 ChatGPT 网页接口适配与 Token 管理代码，并加入本地管理员登录、聊天测试台、额度快照、账号调度、本地用量记录和运行辅助功能。本仓库以当前源码快照作为初始版本。

项目遵循 [MIT License](LICENSE)，保留原许可证中的 `Copyright (c) 2024 aurora-develop` 声明。使用时请遵守相关服务条款，并仅使用有权访问的账号和凭据。
