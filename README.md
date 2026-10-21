# Chat2API Auto Accounts

把已登录的 ChatGPT 网页账号接入本地服务，供支持 OpenAI Chat Completions 格式的客户端调用。项目基于 Python / FastAPI，提供 Token 池、管理员登录和浏览器聊天测试台。

```text
兼容客户端 / 本地测试台
          ↓
POST /v1/chat/completions
          ↓
Token 池选择已导入的账号
          ↓
ChatGPT 网页接口 → 转换为 JSON / SSE 聊天响应
```

这是基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 修改的非官方项目，不是 OpenAI 官方 API 服务。它使用网页账号的权限和限额，稳定性取决于账号状态、网络及上游网页接口变化。

## 已实现的功能

- **聊天接口**：`POST /v1/chat/completions`，支持流式 SSE 和非流式 JSON 响应。
- **Token 池**：从文件或管理页面导入凭据；使用本地 API Key 调用时，按配置轮换或随机选择 Token，并排除已记录的异常 Token。
- **本地管理登录**：使用用户名和 PBKDF2 密码哈希登录，保护测试台和 Token 管理入口。
- **浏览器测试台**：发送消息、临时多轮对话、切换流式响应、查看 HTTP 状态、耗时和首字时间。页面会显示本地 Token 数量与异常标记数量。
- **本地运行管理**：提供启动、状态查询、停止脚本，以及从本仓库源码构建的 Docker Compose 配置。

名称中的 Auto Accounts 指已导入 Token 的自动选择，不包含自动注册账号或自动完成网页登录。

## 使用边界

- 建议先使用 `model: "auto"`。代码保留了一些模型别名与回退逻辑；响应里的 `model` 标签不能证明实际使用了同名底层模型，也不代表该模型当前可用。
- 默认配置的兼容范围以现有聊天接口为准。**没有实现标准 `/v1/images/generations`、`/v1/images/edits` 或 `/v1/responses` 路由**，也没有供客户端自动发现模型的 `/v1/models` 路由。上游聊天可能返回图片内容，这不等于提供标准 Images API。
- 没有账号额度仪表盘、按剩余额度调度或“无限使用”能力。本地数量和异常标记不能表示账号实时可用性、剩余次数或重置时间。
- 测试台的聊天记录只存在于当前页面，刷新或退出后清空。`max_tokens` 等参数不保证被网页上游严格执行。
- 默认配置面向本机单进程运行。管理页面固定使用 `127.0.0.1:5005` 或 `localhost:5005`；需要公网服务或多实例部署时，须另行设计访问控制和会话存储。

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
AUTO_SEED=true
RANDOM_TOKEN=false
RETRY_TIMES=0
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
- [Token 管理](http://127.0.0.1:5005/tokens)：导入或清空 Token 池。

管理员登录状态保存在服务进程内，重启后需要重新登录。测试台显示“已连接”仅表示本地服务可访问；上游是否可用以聊天请求结果为准。

macOS / Linux 也可选择后台运行脚本，要求虚拟环境位于 `.venv`，并安装了 `ps`、`lsof`：

```bash
python scripts/local_service.py start
python scripts/local_service.py status
python scripts/local_service.py stop
```

后台日志位于 `data/server.log`。前台命令和后台脚本选一种启动即可。

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
| `RANDOM_TOKEN` | `false` | `false` 轮换选择，`true` 随机选择；轮换顺序不保证与文件行序一致 |
| `RETRY_TIMES` | `0` | 关闭自动重试，便于观察原始失败；增加重试可能再次发送请求 |
| `HISTORY_DISABLED` | `true` | 向网页上游请求禁用历史记录，不代表本地文件自动清理 |
| `SCHEDULED_REFRESH` | `false` | 关闭继承的 Refresh Token 定时刷新流程；不会自动更新导入的 Access Token |
| `API_PREFIX` | 留空 | API 路径前缀；本地测试台建议保持留空 |
| `ENABLE_GATEWAY` | `false` | 关闭继承的网页网关功能 |
| `LOCAL_CONSOLE_ENABLED` | `true` | 启用本地登录、测试台及 Token 管理路由保护 |
| `LOCAL_CONSOLE_USERNAME` | `admin` | 管理员用户名 |
| `LOCAL_CONSOLE_PASSWORD_HASH` | 生成的带单引号哈希 | 管理员密码校验；留空时无法登录 |

遇到上游认证失败、限额或超时，应结合响应和服务日志检查凭据、账号状态及网络。更换模型标签或反复重试不能保证解决上游限制。

## 开发验证

安装依赖后，可运行本地控制台的 HTTP 测试：

```bash
python -m unittest discover -s tests -p 'test_local_console_http.py' -v
```

测试在临时目录启动独立服务，使用临时登录凭据和空 Token 池，并拦截向上游发起的请求，不读取真实账号文件。覆盖登录、退出、管理页面访问控制、来源校验、空账号池和输入校验。GitHub Actions 会运行语法编译检查和这些测试；它们不验证真实账号的上游可用性。

## 来源与许可证

本项目基于 [lanqian528/chat2api](https://github.com/lanqian528/chat2api) 的 [a63a90c](https://github.com/lanqian528/chat2api/commit/a63a90cec90dea3fea3e272d22f8c5799de14e30) 源码，保留其 ChatGPT 网页接口适配与 Token 管理代码，并加入本地管理员登录、聊天测试台和运行辅助功能。本仓库以当前源码快照作为初始版本。

项目遵循 [MIT License](LICENSE)，保留原许可证中的 `Copyright (c) 2024 aurora-develop` 声明。使用时请遵守相关服务条款，并仅使用有权访问的账号和凭据。
