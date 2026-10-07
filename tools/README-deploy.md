# 一键部署说明（Dockerfile / docker-compose.yml / run.sh / run.ps1）

面向已经 clone 了仓库、想最快看到页面的人。AK 申请步骤、`.env` 变量清单与合规声明归
根目录 `README.md`（FR-407 / FR-413），这里只讲**部署这件事本身**，不重复抄写以免两处失同步。

## 1. 三条启动路径

| 路径 | 命令 | 用在哪 |
| --- | --- | --- |
| A 容器（交付要求） | `docker compose up --build` 或 `./run.sh` / `.\run.ps1` | 有 Docker 的机器，评审验收用的就是这条 |
| B 本机 venv（退路） | `./run.sh local` 或 `.\run.ps1 local` | 没装 Docker / Docker 守护进程没起来的机器 |
| C 手工 | `.venv/.../python -m uvicorn api.app:app --host 127.0.0.1 --port 8000` | 调试，想看清每一步在干什么 |

零配置断言（PRD §6.2）：全新 clone、**不创建 `.env`**、执行 `docker compose up` → 服务照常起，
`data_mode` 由启动探测落 `mock`，报告完整，只是数据来源标为 `sample`。这条断言是本文件里所有
设计取舍的出发点。

## 2. 为什么 `env_file` 指的是 `.env.example` 而不是 `.env`

`env_file: .env` 是直觉写法，但它和零配置断言直接冲突：Compose 遇到缺失的 env_file 是
**硬失败**（`Couldn't find env file: .env`），而断言要求「不填任何 `.env`」也能 `up`。
规范里的可选写法

```yaml
env_file:
  - path: .env
    required: false
```

要 Compose **v2.24（2024-01）以上**才认；旧版本看到 `path/required` 这种映射是解析错误而不是忽略，
于是升级语法反而把断言砸了。本机没有 Docker，无法实测评审机的 Compose 版本，所以取一个
在 v1 / v2 都成立的等价方案，两层叠加：

1. `env_file: .env.example` —— 模板在全新 clone 里必然存在，内容本身就是一套可用的零配置默认值
   （AK 空、`DATA_MODE=auto`），等于把 `infra/config.py` 的保守默认搬进容器。
2. `environment: KEY: ${KEY:-默认}` —— Compose 做插值时**永远**会读项目目录的 `.env`（不存在也只是
   用默认值，不报错），并且 `:-` 默认值顺带压掉
   `WARNING: The XXX variable is not set. Defaulting to a blank string.` 这行红字。
   同名键 `environment` 覆盖 `env_file`，所以用户在 `.env` 里写的值一定生效。

代价：`environment` 只列了演示与验收会现场改的 31 个旋钮（AK、模式、限流阈值、三栏预算、缓存、
以及两份可替换的配置文件路径）。
**新增配置项若需要现场覆盖，要同步这里**，否则只能靠 `.env.example` 的模板值。
在 Compose ≥ v2.24 的机器上，可以改用上面的 `required: false` 写法整份透传 `.env`。

另外两处与直觉不同的取值，都是刻意的：

- `APP_HOST` 在容器里固定 `0.0.0.0`。模板里的 `127.0.0.1` 是给本机 uvicorn 用的，容器内监听回环
  会让宿主端口映射直接 connection refused（「镜像起得来但打不开」的根因）。
- `CACHE_DB_PATH` / `SAMPLES_DIR` / `WEB_DIR` 在容器里钉成绝对路径。模板写的是相对 cwd 的值，
  cwd 一变就会在别处悄悄建第二个库，表现为「命中率 0」这种查不出来的问题。
- 端口：容器内固定 8000（`Dockerfile` 的 CMD）。改宿主端口只动 `CIRCLEDOC_HOST_PORT`，
  `APP_PORT` 不透传进容器，避免出现「`.env` 写了 8080、容器还在听 8000」的错位。
  默认只发布到 `127.0.0.1`；手机演示要局域网访问就在 `.env` 里加 `CIRCLEDOC_HOST_IP=0.0.0.0`。

## 3. 镜像里为什么一定没有 `.db`

`.dockerignore` 是这条链上唯一的守门人（FR-402：缓存是同进程运行时性能措施，不是数据资产库）。
两个要点：

- 模式**锚定在上下文根目录**：`*.db` 只匹配根下那一份，`data/cache.db` 一条都排不掉。
  所以库里全部写成 `**/*.db`、`data/**/*.db` 这种带层级的形式，`.db-shm` / `.db-wal` /
  `.db-journal` 这些 SQLite 伴生文件也一起排除。
- `Dockerfile` 不用 `COPY . .`，而是逐个目录 `COPY api/ ./api/ …`。排除规则 + 显式清单双保险：
  前者挡住脏数据，后者让「哪几份内容进镜像」在 review 里一眼可见。

`docker-compose.yml` 里 `./data:/app/data` 挂载后出现的 `cache.db` 属于**运行期产物**，与
「镜像不得预置 `.db`」不冲突：前者是用户自己机器上跑出来的，后者约束分发内容。

## 4. 两个平台的差异（为什么脚本要写两份）

| 事项 | run.sh（Linux / macOS / Git Bash） | run.ps1（Windows PowerShell 5.1） |
| --- | --- | --- |
| 命令链接符 | 可用 `&&` | **5.1 不支持**，一律拆成 `;` + 判退出码 |
| 环境变量 | `$NAME` / `export` | 必须写 `$env:NAME`；动态名字要用 `[Environment]::GetEnvironmentVariable()` |
| venv 里的 python | `.venv/bin/python` | `.venv\Scripts\python.exe`（两份脚本都同时探测两种布局） |
| 文件编码 | UTF-8 + **LF**（CRLF 会让 bash 报 `\r` 找不到命令） | UTF-8 + **BOM** + CRLF：5.1 不认无 BOM 的 UTF-8，中文注释与提示会按 GBK 解码成乱码 |
| 原生命令的 stderr | 直接透传 | 5.1 在 `$ErrorActionPreference='Stop'` 下会把 native stderr 升级成 `NativeCommandError` 终止脚本 → 调 Docker 的分支临时降到 `Continue` |
| 内嵌给 python 的代码 | 单引号包整段 | PS 5.1 不给内层双引号转义，`-c` 的代码里**一个双引号都不能有** |
| 执行策略 | `chmod +x` 即可 | 未签名脚本会被拦：`powershell -ExecutionPolicy Bypass -File .\run.ps1` |
| 守护进程没启动 | `docker info` 失败即提示 | 同，另提示「等右下角变成 Engine running」；WSL2 后端与 Hyper-V 后端二选一 |
| Docker 缺失的退路 | 打印 A/B/C 三条路后自动 `local` 起服务 | 同 |

两份脚本行为一致：`up`（默认，Docker 优先，缺 Docker 时解释原因并自动走本机模式）、`local`、
`build`、`down`、`logs`、`status`、`check`（只自检环境不启动进程）、`setup`（建 venv 装依赖，
会真的执行 pip，所以放在显式命令里而不是背后自动跑）、`help`。

## 5. 健康检查的当前语义

`Dockerfile` 的 `HEALTHCHECK` 打 `/api/health`，判定条件是 **HTTP 状态 < 500 即健康**，不是「等于 200」。

这条**现在仍然成立，而且理由比当初更强**：`/api/health` 已经实装（返回 200 + 逐服务探测结果），
但零配置（无 AK）时它的 `data_mode` 是 `mock` —— 那是一个**合法且预期**的状态，
服务照常能出完整离线报告。真正要判成「不健康」的只有「探测自身炸了」，而那会走 5xx。
所以 `status < 500` 表达的是「进程活着、能应答」，数据层面的结论由响应体表达，两者不是一回事。

⚠️ **字段名要读准**（这里踩过一次，CI 的冒烟脚本因此带着一个必然 `KeyError` 的断言进了仓库）：
顶层是 **`data_mode`**（本次生效的模式，值域 `live|cache|mock`），
而 `data_mode_in_effect` **只存在于 `config` 子对象**里（`.env` 配置的回显）。
`web/isochrone.js` 的读法是 `config.data_mode_in_effect || health.data_mode`，两处都兜住；
但脚本里若只取顶层的 `data_mode_in_effect`，会直接 `KeyError` 而不是给出可读结论。

⚠️ 历史注记（留着以免有人把它当成历史包袱删掉）：M4 之前 `api/app.py` 的 `ROUTERS` 是空表，
`/api/health` 返回 404；当时若按「等于 200 才算健康」写，Compose 会给一个其实活着的服务
打上 `unhealthy`，直接违背 §6.2 的「不出现红色报错」。

探测命令用 `python -c` 调 `httpx`（`requirements.txt` 已有），不装 curl：slim 镜像里没有 curl，
为一条健康检查多开一层 apt 不值得。

## 6. 已知未验证项（本机没有 Docker）

以下都只做了静态校验（`yaml.safe_load`、`bash -n`、`[ScriptBlock]::Create` 解析、
Dockerfile 指令与上下文路径核对），**没有实机跑过 `docker build` / `docker compose up`**：

- 镜像能否构建成功（`pip install -r requirements.txt` 在 python:3.11-slim 上的 wheel 可得性）。
- `python:3.11-slim` 基础镜像里 `mkdir`/`chown` 可用（Debian userland，应无问题，但未实测）。
- 非 root（数字 UID 1000）在 **Linux 宿主**上对 bind mount `./data` 的写权限：宿主用户不是 1000 时
  SQLite 会报 `attempt to write a readonly database`，需要 `sudo chown -R 1000:1000 data`。
  Docker Desktop（Windows / macOS）会自动处理映射，不受影响。
- 评审机的 Compose 版本与本文件用到的语法子集是否匹配（这里刻意只用 v1/v2 共有的字段，
  但同样未经实机确认）。
- 容器内时区：`QUOTA_BREAKER_TZ_OFFSET_HOURS=8` 用固定偏移而不是 tzdata（`infra/config.py` 的既有
  设计），因此 slim 镜像缺时区库不影响「次日零时」判定；这一点没有实机验证。
