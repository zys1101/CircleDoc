# CircleDoc 运行镜像 —— PRD FR-408 / §9「一键部署」。
#
# 为什么是 python:3.11-slim：依赖全是 binary wheel（fastapi / uvicorn / pydantic /
# pydantic-settings / httpx），不需要 gcc；slim 比完整版少 300 MB 以上，且不带编译工具链，
# 供应链面更小。Python 版本与 `pyproject.toml` 的 target-version = py311 对齐。
#
# 为什么没有 node/npm 层（决策 D2）：前端是 Vue 3 global build，vendored 在 `web/` 里直接
# 随镜像分发（PRD §5「前端资源」禁止公网 CDN）。加一层构建只会长 build 时间、不产出新东西。
#
# 🔴 镜像内不得出现 `data/*.db`、`.env`、`.venv/`（FR-402 与条款 3.3.4）：排除规则写在同目录
# `.dockerignore`。改动 COPY 清单时请同步那份文件——尤其**不要退回 `COPY . .`**，那会把开发机
# 刚跑出来的 `data/cache.db` 打进镜像，让「二次运行命中率 ≥ 95 %」变成假指标。

FROM python:3.11-slim

# PYTHONUNBUFFERED：日志必须立刻出现在 `docker compose logs`，被 stdout 缓冲吃掉半屏会让
#   现场演示看不到降级原因（PRD §5 可观测）。
# PYTHONDONTWRITEBYTECODE / PIP_NO_CACHE_DIR：不在镜像里留 .pyc 与 pip 缓存垃圾。
# APP_ENV / LOG_JSON：`docker run`（不经 compose）时也应当是 prod 形态的 JSON 日志。
# 为什么这里不设 APP_HOST：容器内监听 0.0.0.0 属于编排层的事，写在镜像里会让 `.env` 的
#   APP_HOST=127.0.0.1（本机 uvicorn 用的默认值）失去意义；见 docker-compose.yml 的 environment。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=prod \
    LOG_JSON=true

WORKDIR /app

# 依赖层单独在前：只有 requirements.txt 变化才会重装依赖，改业务代码不触发 pip 层。
# 为什么直接装整个 requirements.txt（含 pytest/ruff/black）而不拆 prod 版：本机 venv 与镜像
# 读同一份钉过版本的清单，避免「CI 过了、镜像起不来」。拆分留给 M2（多阶段构建）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 非 root 运行。为什么用数字 UID 1000 而不是 `useradd`：python:*-slim 里 useradd 由 passwd
# 包提供，不值得为「一个用户名」再开一层 apt；数字 UID 在任何 base 镜像上都成立，也能被
# K8s 的 runAsUser 直接复用。/app/data 必须归它，否则 SQLite 建不了库（写库要目录可写）。
# 代码目录本身保持 root:root + 755（只读），符合「运行时不可自改代码」的最小权限预期。
RUN mkdir -p /app/data && chown 1000:1000 /app/data

# 逐个目录 COPY（而不是 `COPY . .`）：让「哪几份内容进镜像」在 code review 里一眼可见。
# 源目录名后的斜杠是刻意的——`COPY api/ ./api/` 把内容放进 /app/api，写成 `COPY api ./api/`
# 会变成 /app/api/api。data/ 只带 samples/（*.db 已被 .dockerignore 排除），这是 FR-412
# 合成样例的落点，也是 compose 里 bind mount 的挂载点。
COPY --chown=1000:1000 api/ ./api/
COPY --chown=1000:1000 services/ ./services/
COPY --chown=1000:1000 infra/ ./infra/
COPY --chown=1000:1000 web/ ./web/
COPY --chown=1000:1000 config/ ./config/
COPY --chown=1000:1000 data/ ./data/

USER 1000:1000

EXPOSE 8000

# 健康检查打 `/api/health`（PRD §5 可观测、§6.2 启动探测）。
# 判定条件是 status < 500 而不是 == 200，这是**刻意保留**的：`/api/health` 在零配置
# （无 AK）时也应当返回 200，但启动探测本身若抛异常，服务仍需被判为「活着」而不是
# `unhealthy` —— §6.2 要求「不出现红色报错」，而一个探测失败但能出完整离线报告的服务
# 恰恰是合法状态。真正的语义由响应体表达：顶层 `data_mode`（值域 live|cache|mock）
# 说明本次生效的模式，`config.data_mode_in_effect` 是 `.env` 配置的回显 ——
# 不靠 HTTP 码，也**不要**只读顶层 `data_mode_in_effect`（那个键不存在，会 KeyError）。
# 历史注记：M4 之前 `ROUTERS` 还是空表、该路径返回 404，当时这条
# 「< 500」的写法救过一次误判；现在路由齐了，它继续承担「探测异常不算宕机」这一层。
# 为什么用 python + httpx 而不是 curl/wget：slim 镜像里两者都没装，为健康检查装 curl 会
# 多一层 apt；httpx 是 requirements.txt 里已有的依赖。
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import sys, httpx; sys.exit(0 if httpx.get('http://127.0.0.1:8000/api/health', timeout=4).status_code < 500 else 1)"]

# exec 形式（不是 shell 形式）：这样 uvicorn 是 PID 1，能收到 docker stop 发的 SIGTERM 并
# 优雅退出；`sh -c` 会把信号挡在 shell 外面，compose 每次 down 都要等满 10 s 超时。
# --host 0.0.0.0：容器里监听回环等于没对外暴露，宿主端口映射会直接 connection refused。
# --port 固定 8000：容器内端口与 APP_PORT 解耦（改宿主端口请动 CIRCLEDOC_HOST_PORT）。
# --no-access-log：`api/app.py` 的中间件已经打了带三栏计量的请求日志，再留一份 uvicorn
# access log 只会把 docker logs 刷成两行一请求。
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
