# ============================================================================
#  bootstrap.ps1 —— UWB TDoA 工作目录一键初始化
#
#  做三件事：
#    1) clone 需要的第三方仓库到 clones\（已存在则跳过，可重复运行）
#    2) 安装 Python 依赖（pyserial / pyusb / numpy / pyyaml / PyQt5 / lps-tools）
#    3) 确保官方节点固件 .dfu 在 firmware\
#
#  用法（PowerShell）：
#      cd D:\_AAA_CODE_ALL\UWB
#      powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
#
#  网络参数（国内直连 github.com 常被重置，脚本会自动处理）：
#      -Proxy http://127.0.0.1:7897   手动指定代理
#      -Mirror                        强制走 ghfast.top 镜像
#
#  其他参数：
#      -WithRefs      额外 clone 参考实现（DW1000-TDoA3 / MultilaterationTDOA / libdw1000）
#      -SkipPip       跳过 pip 安装
#      -SkipFirmware  跳过固件检查/下载
# ============================================================================

param(
    [string]$Proxy = "",
    [switch]$Mirror,
    [switch]$WithRefs,
    [switch]$SkipPip,
    [switch]$SkipFirmware
)

$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot
$CloneDir  = Join-Path $Root 'clones'
$FwDir     = Join-Path $Root 'firmware'
$FwVersion = '2022.09'
$FwFile    = Join-Path $FwDir "lps-node-firmware-$FwVersion.dfu"
$FwUrl     = "https://github.com/bitcraze/lps-node-firmware/releases/download/$FwVersion/lps-node-firmware-$FwVersion.dfu"
$MirrorPrefix = 'https://ghfast.top/'

function Say([string]$m)  { Write-Host "[UWB] $m" -ForegroundColor Cyan }
function Ok([string]$m)   { Write-Host "[UWB] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "[UWB] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "[UWB] $m" -ForegroundColor Red }

function Test-TcpPort {
    param([string]$Target, [int]$Port, [int]$TimeoutMs = 400)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($Target, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if ($ok) { $client.EndConnect($iar) }
        $client.Close()
        return $ok
    } catch {
        return $false
    }
}

function Find-LocalProxy {
    foreach ($p in 7897, 7890, 10809, 10808, 1080, 8080) {
        if (Test-TcpPort -Target '127.0.0.1' -Port $p) {
            return "http://127.0.0.1:$p"
        }
    }
    return ""
}

Write-Host "=============================================="
Write-Host " UWB TDoA bootstrap"
Write-Host " 工作目录: $Root"
Write-Host "=============================================="

# --- 0. 前置检查 ------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "找不到 git，请先安装 Git for Windows 并加入 PATH。"
    exit 1
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "找不到 python，请先安装 Python 3 并加入 PATH。"
    exit 1
}
Say "Python: $((python --version 2>&1))"

New-Item -ItemType Directory -Force -Path $CloneDir, $FwDir | Out-Null

# --- 网络路线选择 -----------------------------------------------------------
$UseProxy = $false
if ($Proxy -ne "") {
    $UseProxy = $true
    Say "使用指定代理: $Proxy"
} elseif (-not $Mirror) {
    $auto = Find-LocalProxy
    if ($auto -ne "") {
        $Proxy = $auto
        $UseProxy = $true
        Say "自动检测到本地代理: $Proxy"
    } else {
        Warn "未检测到本地代理端口（7897/7890/10809/1080/8080）"
    }
}

if ($UseProxy) {
    $env:HTTP_PROXY  = $Proxy
    $env:HTTPS_PROXY = $Proxy
    $env:http_proxy  = $Proxy
    $env:https_proxy = $Proxy
}

function New-CloneArgs {
    param([string]$Url, [string]$Dest, [string[]]$Extra)
    $a = @('clone')
    if ($UseProxy) { $a += @('-c', "http.proxy=$Proxy", '-c', "https.proxy=$Proxy") }
    $a += $Extra
    $a += @($Url, $Dest)
    return $a
}

# --- 1. clone 仓库 ----------------------------------------------------------
function Clone-Repo {
    param(
        [string]$Name,
        [string]$Url,
        [string[]]$Extra = @()
    )
    $dest = Join-Path $CloneDir $Name
    if (Test-Path (Join-Path $dest '.git')) {
        Say "已存在，跳过: $Name"
        return $true
    }

    $candidates = @()
    if (-not $Mirror) {
        $candidates += @{ label = 'proxy/直连'; url = $Url }
    }
    $candidates += @{ label = '镜像 ghfast.top'; url = "$MirrorPrefix$Url" }

    foreach ($c in $candidates) {
        Say "clone: $Name  [$($c.label)]"
        $args = New-CloneArgs -Url $c.url -Dest $dest -Extra $Extra
        & git @args
        if ($LASTEXITCODE -eq 0) {
            Ok "  -> 成功: $Name"
            return $true
        }
        Warn "  -> 失败，尝试下一条路线"
        if (Test-Path $dest) { Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Fail "clone 失败: $Name（可加 -Proxy http://127.0.0.1:端口 重试）"
    return $false
}

# 节点固件：含官方 sniffer 解码脚本（tools/sniffer/*.py），带子模块以便从源码编译
$ok1 = Clone-Repo -Name 'lps-node-firmware' -Url 'https://github.com/bitcraze/lps-node-firmware.git' -Extra @('--depth', '1', '--recurse-submodules', '--shallow-submodules')

# 配置/烧录工具：提供 DfuSe 烧写实现（tools/lps_flash.py 复用）
$ok2 = Clone-Repo -Name 'lps-tools' -Url 'https://github.com/bitcraze/lps-tools.git' -Extra @('--depth', '1')

# 参考固件：TDoA 引擎 / EKF 观测模型的权威实现（只读参考）
$ok3 = Clone-Repo -Name 'crazyflie-firmware' -Url 'https://github.com/bitcraze/crazyflie-firmware.git' -Extra @('--depth', '1', '--no-recurse-submodules')

if ($WithRefs) {
    Clone-Repo -Name 'DW1000-TDoA3'        -Url 'https://github.com/dy-dx-1/DW1000-TDoA3.git' -Extra @('--depth', '1') | Out-Null
    Clone-Repo -Name 'MultilaterationTDOA' -Url 'https://github.com/AlexisTM/MultilaterationTDOA.git' -Extra @('--depth', '1') | Out-Null
    Clone-Repo -Name 'libdw1000'           -Url 'https://github.com/bitcraze/libdw1000.git' -Extra @('--depth', '1') | Out-Null
}

# --- 2. Python 依赖 ---------------------------------------------------------
if (-not $SkipPip) {
    Say "安装 Python 依赖 ..."
    python -m pip install --quiet --disable-pip-version-check pyserial pyusb numpy pyyaml
    if ($LASTEXITCODE -ne 0) { Warn "pip 基础依赖安装失败（可加 -Proxy 重试）" } else { Ok "基础依赖就绪" }

    $lpsTools = Join-Path $CloneDir 'lps-tools'
    if (Test-Path (Join-Path $lpsTools 'setup.py')) {
        Say "安装 lps-tools (editable, 含 PyQt5 GUI 与 DfuSe 实现) ..."
        Push-Location $lpsTools
        try { python -m pip install --quiet --disable-pip-version-check -e ".[pyqt5]" }
        finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Warn "lps-tools 安装失败（GUI 不可用，但刷写/配置脚本仍可用）" } else { Ok "lps-tools 就绪" }
    }
}

# --- 3. 固件 ----------------------------------------------------------------
if (-not $SkipFirmware) {
    if (Test-Path $FwFile) {
        Ok "固件已存在: firmware\lps-node-firmware-$FwVersion.dfu"
    } else {
        $urls = @($FwUrl)
        if ($Mirror) { $urls = @("$MirrorPrefix$FwUrl") } else { $urls += "$MirrorPrefix$FwUrl" }
        foreach ($u in $urls) {
            Say "下载固件: $u"
            try {
                if ($UseProxy) {
                    Invoke-WebRequest -Uri $u -OutFile $FwFile -UseBasicParsing -Proxy $Proxy
                } else {
                    Invoke-WebRequest -Uri $u -OutFile $FwFile -UseBasicParsing
                }
                if (Test-Path $FwFile) { break }
            } catch {
                Warn "  -> 失败: $($_.Exception.Message)"
            }
        }
    }
    if (Test-Path $FwFile) {
        $size = (Get-Item $FwFile).Length
        Say ("固件大小: {0} 字节" -f $size)
        if ($size -lt 50000) { Warn "固件文件偏小，可能没下载完整" }
    } else {
        Warn "没有固件文件。可手动下载 2022.09 的 .dfu 放到 firmware\ 目录。"
    }
}

# --- 4. 结果汇总 ------------------------------------------------------------
Write-Host ""
Write-Host "===================== 完成 ====================="
if (-not ($ok1 -and $ok2 -and $ok3)) {
    Warn "有仓库未 clone 成功，可重试："
    Warn "  powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Proxy http://127.0.0.1:7897"
}
Write-Host "下一步："
Write-Host "  0) 无硬件自检:    python tools\make_fake_capture.py ; python tools\lps_tdoa3_solver.py --input captures\fake_tdoa3.bin --anchors captures\fake_anchors.yaml"
Write-Host "  1) 查看硬件:      python tools\lps_config.py --list"
Write-Host "  2) 配置节点:      python tools\lps_config.py --port COM5 --id 0 --mode tdoa3"
Write-Host "  3) 刷固件:        python tools\lps_flash.py --port COM5"
Write-Host "  4) 抓包验证:      python tools\lps_capture.py --port COM7 --seconds 30 --summary"
Write-Host "  5) 解算位置:      python tools\lps_tdoa3_solver.py --port COM7 --anchors tools\anchors_example.yaml --check"
Write-Host "详细步骤见 docs\LPS-TDoA-完整部署操作手册.md"
Write-Host "================================================"
