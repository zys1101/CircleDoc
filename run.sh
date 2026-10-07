#!/usr/bin/env bash
# CircleDoc 一键启动（Linux / macOS；Windows 的 Git Bash 也能直接跑）—— PRD FR-408。
#
# 为什么要有「退路」这一段：这台演示机根本没装 Docker，`docker compose up` 只会留下
# `command not found`。对使用者那不是「部署方式选错了」而是「一片红字、程序没起」，
# 所以脚本在 Docker 缺失 / 守护进程没启动时**自动退回本机 venv 跑 uvicorn**，并把
# 「为什么退回、怎么装回 Docker」写在同一段输出里。
#
# 为什么 setup 之外的路径都不自动 pip install：静默改环境会让人分不清「刚才那个错是谁装的包」。
# 缺 venv 时只打印一条可复制的命令（./run.sh setup）。
#
# 用法：./run.sh [up|local|build|down|logs|status|check|setup|help]

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE="circledoc"

say() { printf '%s\n' "$*"; }
hr() { printf -- '--------------------------------------------------------------------------\n'; }

# 为什么自己解析 .env 而不是 source：source 会把整份文件当 shell 代码执行，值里带空格或
# 反引号就炸了；这里只要一个键，读不出来就算「没配」。
env_value() {
    if [[ -f .env ]]; then
        sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -n 1
    fi
}

# 宿主访问端口：shell 环境变量 > .env 里的 APP_PORT > 8000（与 docker-compose.yml 的默认一致）。
host_port() {
    local p="${CIRCLEDOC_HOST_PORT:-}"
    if [[ -z "$p" ]]; then
        p="$(env_value APP_PORT)"
    fi
    printf '%s' "${p:-8000}"
}

# 本机监听地址：默认回环；手机联调时 APP_HOST=0.0.0.0 ./run.sh local。
local_host() { printf '%s' "${APP_HOST:-127.0.0.1}"; }

url_hint() {
    local p
    p="$(host_port)"
    say "  访问：      http://127.0.0.1:${p}/"
    say "  接口文档：  http://127.0.0.1:${p}/api/docs"
    say "  健康检查：  http://127.0.0.1:${p}/api/health（W3 合并前返回 404 JSON，属预期）"
    say "  零配置：    不填 .env 也能跑；缺服务端 AK 时按 mock 出完整报告（data_source=sample）。"
}

venv_python() {
    # 为什么先试 Scripts/：Windows 的 venv 布局（Git Bash 同）；再试 bin/：Linux/macOS。
    local c
    for c in ".venv/Scripts/python.exe" ".venv/bin/python" ".venv/bin/python3"; do
        if [[ -f "$c" ]]; then
            printf '%s' "$c"
            return 0
        fi
    done
    return 1
}

compose_bin() {
    # docker compose（v2 子命令）优先，docker-compose（v1 独立二进制）兜底。
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        printf 'docker compose'
        return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
        printf 'docker-compose'
        return 0
    fi
    return 1
}

docker_state() {
    # 三种状态让提示能精确到「该点哪个图标」：no-cli（没装）、no-daemon（装了没开）。
    if ! command -v docker >/dev/null 2>&1; then
        printf 'no-cli'
    elif docker info >/dev/null 2>&1; then
        printf 'ok'
    else
        printf 'no-daemon'
    fi
}

explain_no_docker() {
    hr
    case "${1:-no-cli}" in
    no-cli)
        say "没有检测到 Docker CLI（docker 命令不存在）。"
        say "这是部署方式的问题，不是代码或配置的问题。三条路任选："
        say "  A. 先用退路跑通演示：./run.sh local"
        say "     直接拿本机 .venv 起 uvicorn，接口和行为一致，只是没有容器隔离。"
        say "  B. 装 Docker Desktop：https://www.docker.com/products/docker-desktop/"
        say "     Windows 装完要重启一次，并确认 WSL 2 已启用（wsl --install）。"
        say "  C. Linux 用发行版自己的包：sudo apt install docker.io docker-compose-plugin"
        ;;
    no-daemon)
        say "docker 命令在，但连不上守护进程（docker info 失败）。"
        say "通常是 Docker Desktop 没启动，或当前用户不在 docker 组："
        say "  Windows / macOS：启动 Docker Desktop，等状态变成 Engine running 再重跑。"
        say "  Linux：sudo usermod -aG docker \$USER 后重新登录一次才生效。"
        say "急着看效果就先走退路：./run.sh local"
        ;;
    esac
    hr
}

run_local() {
    local py host port
    if ! py="$(venv_python)"; then
        say "找不到 .venv 里的 Python。先建环境（本脚本不静默装包）："
        say "  ./run.sh setup"
        say "等价手工命令：python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
        return 1
    fi
    host="$(local_host)"
    port="$(host_port)"
    hr
    say "本机模式启动（不是 Docker）：$py -m uvicorn api.app:app --host $host --port $port"
    say "Ctrl-C 停止。日志是单行 JSON，与容器里看到的同格式。"
    url_hint
    hr
    exec "$py" -m uvicorn api.app:app --host "$host" --port "$port"
}

do_check() {
    local state c py
    hr
    say "CircleDoc 部署自检（只读环境，不启动任何进程）"
    state="$(docker_state)"
    say "  Docker 状态      : $state"
    if c="$(compose_bin)"; then
        say "  Compose 命令     : $c"
    else
        say "  Compose 命令     : 不可用（docker compose 与 docker-compose 都没找到）"
    fi
    if py="$(venv_python)"; then
        say "  本机 venv Python : $py"
        # 这一行由 python 打印：Windows 控制台不是 UTF-8 代码页时中文会花，故只输出 ASCII。
        if ! "$py" -c 'import sys, fastapi, uvicorn, httpx; print("  deps import OK   : python " + sys.version.split()[0])'; then
            say "  依赖可导入       : 失败——跑 ./run.sh setup 重装 requirements.txt"
        fi
    else
        say "  本机 venv Python : 不存在（./run.sh setup 建一个）"
    fi
    if [[ -f .env ]]; then
        say "  .env             : 存在（compose 用 \${VAR:-默认} 插值读取，不需要 env_file 指它）"
    else
        say "  .env             : 不存在——正是 §6.2 零配置断言的场景，服务照样能起"
    fi
    if [[ -f .env.example ]]; then
        say "  .env.example     : 存在（compose 的 env_file 基线；全新 clone 里必然有）"
    else
        say "  .env.example     : 缺失！docker-compose.yml 的 env_file 会因此失败，请恢复该文件"
    fi
    if [[ -f data/cache.db ]]; then
        say "  data/cache.db    : 存在（本机运行产物；.dockerignore 已排除，不会进镜像 FR-402）"
    fi
    say "  业务路由         : 见 api/app.py 的 ROUTERS（W3 之前 /api/health 返回 404 属预期）"
    url_hint
    hr
}

usage() {
    say "用法：./run.sh [命令]"
    say "  up      默认。docker compose up --build（前台跑，Ctrl-C 停止）；Docker 不可用时自动走本机模式"
    say "  local   跳过 Docker，用本机 .venv 跑 uvicorn"
    say "  build   只构建镜像"
    say "  down    停止并移除容器（./data 里的 cache.db 保留，二次命中靠它）"
    say "  logs    跟踪 $SERVICE 容器的日志"
    say "  status  compose ps 与健康状态"
    say "  check   只自检环境，不启动任何东西"
    say "  setup   建 .venv 并安装 requirements.txt（会真的执行 pip）"
    say "  help    本段说明"
}

ACTION="${1:-up}"
if [[ $# -gt 0 ]]; then
    shift
fi

# 为什么不在脚本开头就探测：docker info 在守护进程没启动时可能要等好几秒，
# 而 help / check / local 这几条路径根本不碰 Docker。用不到的命令一次都别发。
STATE=""
CMBIN=""
probe_docker() {
    local c
    STATE="$(docker_state)"
    CMBIN=""
    if c="$(compose_bin)"; then
        CMBIN="$c"
    fi
}

case "$ACTION" in
help | -h | --help)
    usage
    ;;
check | doctor)
    do_check
    ;;
setup)
    hr
    say "建 .venv 并安装 requirements.txt（版本已钉住，与镜像里装的是同一套）"
    python3 -m venv .venv
    "$(venv_python)" -m pip install --upgrade pip
    "$(venv_python)" -m pip install -r requirements.txt
    say "完成。./run.sh local 启动。"
    ;;
build)
    probe_docker
    if [[ "$STATE" == ok && -n "$CMBIN" ]]; then
        # $CMBIN 故意不加引号：'docker compose' 要拆成命令 + 子命令两个词。
        exec $CMBIN build
    else
        explain_no_docker "$STATE"
        exit 1
    fi
    ;;
down | stop)
    probe_docker
    if [[ -n "$CMBIN" ]]; then
        exec $CMBIN down
    fi
    say "没有 compose 命令，也没有容器可停。本机模式请直接 Ctrl-C。"
    ;;
logs)
    probe_docker
    if [[ "$STATE" == ok && -n "$CMBIN" ]]; then
        exec $CMBIN logs --follow "$SERVICE"
    else
        explain_no_docker "$STATE"
        exit 1
    fi
    ;;
status | ps)
    probe_docker
    if [[ "$STATE" == ok && -n "$CMBIN" ]]; then
        exec $CMBIN ps
    else
        explain_no_docker "$STATE"
        exit 1
    fi
    ;;
local)
    run_local
    ;;
up)
    probe_docker
    if [[ "$STATE" == ok && -n "$CMBIN" ]]; then
        hr
        say "docker compose up --build（首次要装依赖；之后只有 requirements.txt 变化才重装）"
        url_hint
        hr
        exec $CMBIN up --build
    else
        explain_no_docker "$STATE"
        say "自动切换到本机模式启动（Ctrl-C 停止）。只想修 Docker 的话用 ./run.sh check 看缺什么。"
        run_local
    fi
    ;;
*)
    say "未知命令：$ACTION"
    usage
    exit 2
    ;;
esac
