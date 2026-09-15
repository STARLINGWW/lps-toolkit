# ============================================================================
#  git_setup.ps1 —— 把本工程初始化成 git 仓库并准备推送到 GitHub
#
#  本地能做的它都做：初始化、写身份、写代理、提交、加远端；
#  需要你手动做的（在 GitHub 建仓、认证）会清楚列出来。
#
#  用法（两条命令，分两步）：
#    1) 填身份并完成首次提交：
#       powershell -ExecutionPolicy Bypass -File .\tools\git_setup.ps1 -Name "你的名字" -Email "你的邮箱"
#
#    2) 在 GitHub 建好空仓库后，加远端并推送：
#       powershell -ExecutionPolicy Bypass -File .\tools\git_setup.ps1 -RepoUrl "https://github.com/用户名/仓库名.git"
#       git push -u origin main
#
#  说明：
#    * 代理默认自动探测本地端口（7897/7890/10809/1080），且只写在本仓库
#      （--local），不影响你其它仓库；不想用代理加 -NoProxy。
#    * 提交身份也只写在本仓库，不动全局配置。
# ============================================================================

param(
    [string]$Name = "",
    [string]$Email = "",
    [string]$RepoUrl = "",
    [string]$Proxy = "",
    [switch]$NoProxy,
    [string]$Message = "UWB TDoA: LPS Node flash/config/solver toolchain"
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

function Say($m)  { Write-Host "[git] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[git] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[git] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[git] $m" -ForegroundColor Red }

function Test-TcpPort {
    param([string]$Target, [int]$Port, [int]$TimeoutMs = 400)
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect($Target, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if ($ok) { $c.EndConnect($iar) }
        $c.Close()
        return $ok
    } catch { return $false }
}

function Find-LocalProxy {
    foreach ($p in 7897, 7890, 10809, 10808, 1080) {
        if (Test-TcpPort -Target '127.0.0.1' -Port $p) { return "http://127.0.0.1:$p" }
    }
    return ""
}

Write-Host "=============================================="
Write-Host " 保存到 GitHub —— 本地准备"
Write-Host " 工程目录: $Root"
Write-Host "=============================================="

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "找不到 git，请先安装 Git for Windows。"
    exit 1
}
Say "git 版本: $(git --version)"

Push-Location $Root
try {
    if (-not (Test-Path (Join-Path $Root '.git'))) {
        Say "初始化仓库（默认分支 main）..."
        git init -b main | Out-Null
        Ok "已初始化"
    } else {
        Say "已是 git 仓库，跳过 init"
    }

    if ($Name -ne "")  { git config --local user.name  $Name;  Ok "user.name  = $Name" }
    if ($Email -ne "") { git config --local user.email $Email; Ok "user.email = $Email" }
    $curName  = (git config --local user.name)
    $curEmail = (git config --local user.email)
    if (-not $curName -or -not $curEmail) {
        Warn '还没配置提交身份，请重新运行并带上参数： -Name "你的名字" -Email "你的邮箱"'
    }

    if (-not $NoProxy) {
        $p = $Proxy
        if ($p -eq "") { $p = Find-LocalProxy }
        if ($p -ne "") {
            git config --local http.proxy  $p
            git config --local https.proxy $p
            Ok "本仓库代理: $p"
        } else {
            Warn "未探测到本地代理。若 push 被重置，手动执行："
            Write-Host "      git config --local http.proxy http://127.0.0.1:7897"
            Write-Host "      git config --local https.proxy http://127.0.0.1:7897"
        }
    }

    Say "扫描待提交文件（clones/ 与抓包数据由 .gitignore 排除）..."
    $files = git add -A -n 2>$null
    $count = ($files | Measure-Object).Count
    Say "  $count 个文件"
    if ($files) { $files | Select-Object -First 12 | ForEach-Object { Write-Host "      $_" } }
    if ($count -gt 12) { Write-Host "      ...（共 $count 个）" }

    if ($curName -and $curEmail) {
        git add -A
        git commit -q -m $Message
        if ($LASTEXITCODE -eq 0) {
            Ok "已提交: $(git log -1 --oneline)"
        } else {
            Warn "没有可提交的变更"
        }
    }

    if ($RepoUrl -ne "") {
        $hasOrigin = (git remote) -contains 'origin'
        if ($hasOrigin) { git remote set-url origin $RepoUrl } else { git remote add origin $RepoUrl }
        Ok "origin = $RepoUrl"
        Write-Host ""
        Say "下一步："
        Write-Host "      git push -u origin main"
    } else {
        Write-Host ""
        Say "还没设置远端。在 GitHub 网页新建一个【空仓库】后执行："
        Write-Host '      powershell -ExecutionPolicy Bypass -File .\tools\git_setup.ps1 -RepoUrl "https://github.com/你的用户名/仓库名.git"'
    }
} finally {
    Pop-Location
}
