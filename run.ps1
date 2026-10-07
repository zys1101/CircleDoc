#requires -version 5.1
<#
CircleDoc 一键启动（Windows / PowerShell 5.1）—— PRD FR-408。

为什么要单独一份 .ps1：演示机与评审机大概率只有 PowerShell，没有 bash；而 run.sh 里的
退路逻辑（没 Docker 就本机起服务）在 Windows 上同样重要，不能只给 Unix 一侧。

两条 PS 5.1 的坑，这份脚本刻意避开（后续改动请一并遵守）：
  1) 5.1 不支持 shell 那种双与号链接符，多步一律拆成 `;` + `if ($LASTEXITCODE -ne 0)`；
  2) 环境变量必须写 `$env:NAME`，`$NAME` 只是脚本作用域变量，读不到 .env / 系统环境里的值。
  顺带：不用三元 `? :`、不用 `??`、不用 `$IsWindows`（都只在 7.x 有），也不打印 emoji
  （GBK 控制台下会变成问号，反而更像报错）。

如果系统拦了未签名脚本：powershell -ExecutionPolicy Bypass -File .\run.ps1
用法：.\run.ps1 [up|local|build|down|logs|status|check|setup|help]
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'local', 'build', 'down', 'logs', 'status', 'check', 'setup', 'help')]
    [string]$Action = 'up'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Service = 'circledoc'
# 为什么用 $PSScriptRoot 而不是 Get-Location：双击运行或从别的目录调用时 cwd 不是仓库根，
# 相对路径（compose 文件、.venv）会集体失效。5.1 里 $PSScriptRoot 在被执行的脚本内总可用。
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location -LiteralPath $Root

function Write-Line {
    param([string]$Text)
    Write-Host $Text
}

function Write-Hr { Write-Host ('-' * 74) }

function Get-EnvironmentValue {
    param([string]$Name)
    # PowerShell 不能写 `$env:$Name`（动态名字必须走 API），所以这里用 .NET 读取。
    return [Environment]::GetEnvironmentVariable($Name)
}

function Get-EnvFileValue {
    param([string]$Name)
    # 为什么自己解析 .env 而不是 dot-source：.env 不是 PowerShell 语法，整份执行会炸；
    # 这里只要一个键，读不出来就当「没配」，交给调用方给默认值。
    if (-not (Test-Path -LiteralPath '.env')) { return $null }
    $match = Select-String -Path '.env' -Pattern ("^\s*$Name\s*=\s*(.*)$") | Select-Object -Last 1
    if (-not $match) { return $null }
    return $match.Matches[0].Groups[1].Value.Trim()
}

function Get-HostPort {
    # 优先级：shell 环境变量 > .env 的 APP_PORT > 8000（与 docker-compose.yml 的默认一致）。
    $p = Get-EnvironmentValue 'CIRCLEDOC_HOST_PORT'
    if (-not $p) { $p = Get-EnvFileValue 'APP_PORT' }
    if (-not $p) { $p = '8000' }
    return $p
}

function Get-LocalHost {
    $h = Get-EnvironmentValue 'APP_HOST'
    if (-not $h) { $h = '127.0.0.1' }
    return $h
}

function Write-UrlHint {
    $p = Get-HostPort
    Write-Line "  访问：      http://127.0.0.1:$p/"
    Write-Line "  接口文档：  http://127.0.0.1:$p/api/docs"
    Write-Line "  健康检查：  http://127.0.0.1:$p/api/health（W3 合并前返回 404 JSON，属预期）"
    Write-Line '  零配置：    不填 .env 也能跑；缺服务端 AK 时按 mock 出完整报告（data_source=sample）。'
}

function Get-VenvPython {
    # Windows venv 是 Scripts\，WSL/Git Bash 挂进来的 Linux venv 是 bin\，两种都认。
    foreach ($c in @('.venv\Scripts\python.exe', '.venv\bin\python', '.venv\bin\python3')) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Invoke-NativeQuiet {
    param([string]$Exe, [string[]]$Arguments)
    # 为什么要临时把 ErrorActionPreference 调成 Continue：原生命令往 stderr 写一行进度，
    # 在 5.1 + Stop 下会被升级成 NativeCommandError 直接终止脚本——「docker info 失败」
    # 本来是正常分支，却会变成一片红字。这里只要退出码，输出全丢。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | Out-Null
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
}

function Get-ComposeCommand {
    # docker compose（v2 子命令）优先，docker-compose（v1 独立二进制）兜底。返回 argv 前缀。
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        if ((Invoke-NativeQuiet 'docker' @('compose', 'version')) -eq 0) { return @('docker', 'compose') }
    }
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) { return @('docker-compose') }
    return $null
}

function Get-DockerState {
    # 三种状态，提示才能精确到「该点哪个图标」：no-cli（没装）/ no-daemon（装了没开）/ ok。
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return 'no-cli' }
    if ((Invoke-NativeQuiet 'docker' @('info')) -eq 0) { return 'ok' }
    return 'no-daemon'
}

function Write-NoDockerHelp {
    param([string]$State)
    Write-Hr
    if ($State -eq 'no-cli') {
        Write-Line '没有检测到 Docker CLI（docker 命令不存在）。'
        Write-Line '这是部署方式的问题，不是代码或配置的问题。三条路任选：'
        Write-Line '  A. 先用退路跑通演示：.\run.ps1 local'
        Write-Line '     直接拿本机 .venv 起 uvicorn，接口和行为一致，只是没有容器隔离。'
        Write-Line '  B. 装 Docker Desktop：https://www.docker.com/products/docker-desktop/'
        Write-Line '     装完重启一次，并在「Windows 功能」里启用 WSL 2（或用 Hyper-V 后端）。'
        Write-Line '  C. 只在 WSL 里跑：wsl --install 后在 WSL 内 apt install docker.io docker-compose-plugin'
    }
    else {
        Write-Line 'docker 命令在，但连不上守护进程（docker info 失败）。'
        Write-Line '请先启动 Docker Desktop，等右下角状态变成 Engine running 再重跑本脚本。'
        Write-Line '急着看效果就先走退路：.\run.ps1 local'
    }
    Write-Hr
}

function Start-Compose {
    param([string[]]$ComposeArguments)
    # 返回退出码；输出用 Out-Host 直接进控制台——compose up 的构建日志必须实时可见，
    # 一旦经过函数成功流回传，调用方 `$code = ...` 会把整段日志当成返回值吞掉。
    $cmd = Get-ComposeCommand
    if ($null -eq $cmd) {
        Write-NoDockerHelp (Get-DockerState)
        return 1
    }
    $exe = $cmd[0]
    $prefix = @($cmd | Select-Object -Skip 1)
    $argv = @($prefix + $ComposeArguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @argv | Out-Host
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
}

function Start-LocalServer {
    $py = Get-VenvPython
    if (-not $py) {
        Write-Line '找不到 .venv 里的 Python。先建环境（本脚本不在背后偷偷 pip install）：'
        Write-Line '  .\run.ps1 setup'
        return 1
    }
    $h = Get-LocalHost
    $p = Get-HostPort
    Write-Hr
    Write-Line "本机模式启动（不是 Docker）：$py -m uvicorn api.app:app --host $h --port $p"
    Write-Line '按 Ctrl-C 停止。日志是单行 JSON，与容器里同格式。'
    Write-UrlHint
    Write-Hr
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $py -m uvicorn api.app:app --host $h --port $p | Out-Host
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
}

function Show-Check {
    Write-Hr
    Write-Line 'CircleDoc 部署自检（只读环境，不启动任何进程）'
    Write-Line ("  Docker 状态      : " + (Get-DockerState))
    $cmd = Get-ComposeCommand
    if ($null -ne $cmd) { Write-Line ("  Compose 命令     : " + ($cmd -join ' ')) }
    else { Write-Line '  Compose 命令     : 不可用（docker compose 与 docker-compose 都没找到）' }
    $py = Get-VenvPython
    if ($py) {
        Write-Line "  本机 venv Python : $py"
        # 引号坑（PS 5.1 打原生进程时不给内层双引号转义，python 会收到半截表达式）：
        # 这段 -c 代码里**一个双引号都不能有**，标签文字由 PowerShell 自己拼。
        # 控制台不是 UTF-8 代码页时 python 的中文输出会花，所以让它只输出版本号。
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $ver = ((& $py -c 'import sys, fastapi, uvicorn, httpx; print(sys.version.split()[0])' 2>$null) | Out-String).Trim()
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -eq 0 -and $ver) {
            Write-Line "  deps import OK   : python $ver"
        }
        else {
            Write-Line '  依赖可导入       : 失败——跑 .\run.ps1 setup 重装 requirements.txt'
        }
    }
    else {
        Write-Line '  本机 venv Python : 不存在（.\run.ps1 setup 建一个）'
    }
    if (Test-Path -LiteralPath '.env') {
        Write-Line '  .env             : 存在（compose 用 ${VAR:-默认} 插值读取，不需要 env_file 指它）'
    }
    else {
        Write-Line '  .env             : 不存在——正是 §6.2 零配置断言的场景，服务照样能起'
    }
    if (Test-Path -LiteralPath '.env.example') {
        Write-Line '  .env.example     : 存在（compose 的 env_file 基线；全新 clone 里必然有）'
    }
    else {
        Write-Line '  .env.example     : 缺失！docker-compose.yml 的 env_file 会因此失败，请恢复该文件'
    }
    if (Test-Path -LiteralPath 'data\cache.db') {
        Write-Line '  data\cache.db    : 存在（本机运行产物；.dockerignore 已排除，不会进镜像 FR-402）'
    }
    Write-Line '  业务路由         : 见 api\app.py 的 ROUTERS（W3 之前 /api/health 返回 404 属预期）'
    Write-UrlHint
    Write-Hr
}

function Show-Usage {
    Write-Line '用法：.\run.ps1 [命令]'
    Write-Line '  up      默认。docker compose up --build（前台跑，Ctrl-C 停止）；Docker 不可用时自动走本机模式'
    Write-Line '  local   跳过 Docker，用本机 .venv 跑 uvicorn'
    Write-Line '  build   只构建镜像'
    Write-Line '  down    停止并移除容器（.\data 里的 cache.db 保留，二次命中靠它）'
    Write-Line "  logs    跟踪 $Service 容器的日志"
    Write-Line '  status  compose ps 与健康状态'
    Write-Line '  check   只自检环境，不启动任何东西'
    Write-Line '  setup   建 .venv 并安装 requirements.txt（会真的执行 pip）'
    Write-Line '  help    本段说明'
}

switch ($Action) {
  ('help') { Show-Usage; break }
  ('check') { Show-Check; break }
  ('setup') {
      $sysPy = $null
      foreach ($c in @('python', 'py')) {
          if (Get-Command $c -ErrorAction SilentlyContinue) { $sysPy = $c; break }
      }
      if (-not $sysPy) {
          Write-Line '找不到系统 Python。先装 3.11（https://www.python.org/downloads/）并勾选 Add to PATH。'
          exit 1
      }
      Write-Hr
      Write-Line '建 .venv 并安装 requirements.txt（版本已钉住，与镜像里装的是同一套）'
      & $sysPy -m venv .venv | Out-Host
      $py = Get-VenvPython
      & $py -m pip install --upgrade pip | Out-Host
      & $py -m pip install -r requirements.txt | Out-Host
      Write-Line '完成。.\run.ps1 local 启动。'
      break
  }
  ('local') { $code = Start-LocalServer; exit $code }
  ('build') {
      if ((Get-DockerState) -ne 'ok') {
          Write-NoDockerHelp (Get-DockerState)
          exit 1
      }
      $code = Start-Compose @('build')
      exit $code
  }
  ('down') {
      if ($null -eq (Get-ComposeCommand)) {
          Write-Line '没有 compose 命令，也没有容器可停。本机模式请直接 Ctrl-C。'
          break
      }
      $code = Start-Compose @('down')
      exit $code
  }
  ('logs') {
      if ((Get-DockerState) -ne 'ok') {
          Write-NoDockerHelp (Get-DockerState)
          exit 1
      }
      $code = Start-Compose @('logs', '--follow', $Service)
      exit $code
  }
  ('status') {
      if ((Get-DockerState) -ne 'ok') {
          Write-NoDockerHelp (Get-DockerState)
          exit 1
      }
      $code = Start-Compose @('ps')
      exit $code
  }
  default {
      # up：能走 Docker 就走 Docker，否则先解释原因、再自动落本机模式。
      if ((Get-DockerState) -eq 'ok') {
          Write-Hr
          Write-Line 'docker compose up --build（首次要装依赖；之后只有 requirements.txt 变化才重装）'
          Write-UrlHint
          Write-Hr
          $code = Start-Compose @('up', '--build')
          exit $code
      }
      Write-NoDockerHelp (Get-DockerState)
      Write-Line '自动切换到本机模式启动（Ctrl-C 停止）。只想修 Docker 的话用 .\run.ps1 check 看缺什么。'
      $code = Start-LocalServer
      exit $code
  }
}
