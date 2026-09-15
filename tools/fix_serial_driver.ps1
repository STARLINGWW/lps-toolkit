# ============================================================================
#  fix_serial_driver.ps1 —— 让 LPS Node 出现 COM 口（默认：逐个节点修）
#
#  背景：Crazyflie 与 LPS Node 的 USB VID:PID 完全相同（0483:5740）。
#  若之前用 Zadig 给 Crazyflie 装过 libusb-win32 驱动，它会按硬件 ID
#  (VID_0483&PID_5740&MI_00) 把 LPS Node 的 CDC 接口也抢走，于是没有 COM 口。
#
#  推荐做法（默认行为）：一次只插一块要用的板子，在设备管理器里单独把
#  它的接口改成 "USB 串行设备" —— 这样不会动到 Crazyradio/Crazyflie 的驱动。
#
#  用法（默认只读检查，不改系统）：
#      powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1
#
#  只有当你要"一次性修好所有节点"时才用（会影响 Crazyflie USB 直连）：
#      powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1 -Apply -Global
# ============================================================================

param(
    [switch]$Apply,      # 与 -Global 一起用才会真正动系统
    [switch]$Global,     # 全局删除 libusb 驱动包（影响所有 0483:5740 设备）
    [switch]$Yes         # 跳过确认
)

$TargetId = 'VID_0483&PID_5740&MI_00'

function Say($m)  { Write-Host "[驱动] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[驱动] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[驱动] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[驱动] $m" -ForegroundColor Red }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host "=============================================="
Write-Host " LPS Node 串口驱动检查"
Write-Host "=============================================="

# --- 1. 当前有哪些 0483:5740 设备、各自绑的什么驱动 -------------------------
Say "当前 0483:5740 设备与驱动绑定："
$devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like '*VID_0483&PID_5740*' }
$bad = @()
if (-not $devs) {
    Warn "  当前没有插着 0483:5740 设备（LPS Node / Crazyflie）"
} else {
    foreach ($d in $devs) {
        $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverInfPath -ErrorAction SilentlyContinue).Data
        $isBad = ($d.InstanceId -like "*MI_00*") -and ($inf -notlike 'usbser*')
        $tag = if ($isBad) { "   <-- 被占用，需要改成 USB 串行设备" } else { "" }
        Write-Host ("   {0,-34} INF={1}{2}" -f $d.FriendlyName, $inf, $tag)
        if ($isBad) { $bad += $d }
    }
}

Say "当前串口："
$ports = [System.IO.Ports.SerialPort]::GetPortNames()
if ($ports) { Write-Host ("   " + ($ports -join ', ')) } else { Write-Host "   无" }

# --- 2. 没有被占用的设备，直接结束 ------------------------------------------
if ($bad.Count -eq 0) {
    Write-Host ""
    Ok "没有发现被 libusb 抢占的节点，无需修复。"
    Write-Host ""
    Write-Host "  若某个节点仍不出现 COM 口，检查："
    Write-Host "    - 是否处于 DFU 刷机模式（该模式不会出现 COM 口）"
    Write-Host "    - USB 线是否为数据线、是否插稳"
    Write-Host "    - 用 python tools\lps_wizard.py 选 [5] 诊断"
    exit 0
}

# --- 3. 默认路线：逐个节点改（推荐） ----------------------------------------
Write-Host ""
Say "需要修复的节点：$($bad.Count) 个"
Write-Host ""
Write-Host "  ── 推荐做法：逐个节点修改（不动 Crazyradio / Crazyflie 的驱动）──"
Write-Host ""
Write-Host "  1) 只插【要用的那一块】节点（其他节点和 Crazyradio 都拔掉）"
Write-Host "  2) 打开【设备管理器】"
Write-Host "  3) 展开 libusb-win32 devices，找到 'Crazyflie 2.x (Interface 0)'"
Write-Host "     （它的硬件 ID 就是 $TargetId）"
Write-Host "  4) 右键 → 更新驱动程序 → 浏览我的电脑上的驱动程序"
Write-Host "     → 让我从计算机上的可用驱动程序列表中选取"
Write-Host "     → 选择 'USB 串行设备'（若看不到，取消勾选『显示兼容硬件』）→ 下一步"
Write-Host "  5) 拔插一次 USB，设备管理器里应出现【端口 (COM 和 LPT) → USB 串行设备 (COMx)】"
Write-Host "  6) 换下一块节点，重复 1)~5)"
Write-Host ""
Write-Host "  这样每块板子的绑定是【各自独立】的，Crazyradio 与 Crazyflie 不受影响。"
Write-Host ""

if (-not ($Apply -and $Global)) {
    Write-Host "  ── 备选做法：一次性修好所有节点（会影响 Crazyflie USB 直连）──"
    Write-Host "      powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1 -Apply -Global"
    Write-Host "      代价：删除 libusb 驱动包后，Crazyflie 通过 USB 直连（cflib usb://）会失效，"
    Write-Host "            需要时用 Zadig 重新装回（Crazyradio 无线不受影响）。"
    exit 0
}

# --- 4. -Apply -Global：全局修复 -------------------------------------------
if (-not (Test-Admin)) {
    Fail "全局修复需要管理员权限。请用管理员身份重新打开 PowerShell。"
    exit 1
}

$hits = @()
Get-ChildItem (Join-Path $env:windir 'INF') -Filter 'oem*.inf' -ErrorAction SilentlyContinue | ForEach-Object {
    $text = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
    if ($text -and $text.Contains($TargetId)) {
        $prov = 'unknown'
        $m = [regex]::Match($text, '(?im)^\s*Provider\s*=\s*(.+?)\s*$')
        if ($m.Success) { $prov = $m.Groups[1].Value.Trim([char]34, [char]32) }
        $hits += [pscustomobject]@{ Inf = $_.Name; Provider = $prov }
    }
}

if (-not $Yes) {
    Warn "将对以下驱动包执行删除（影响所有 0483:5740 设备）："
    $hits | ForEach-Object { Write-Host ("   {0}  provider={1}" -f $_.Inf, $_.Provider) }
    $a = Read-Host "  确认继续？输入 yes 继续"
    if ($a -ne 'yes') { Warn "已取消"; exit 0 }
}

foreach ($h in $hits) {
    if ($h.Provider -notlike '*libusb*') { Warn "跳过 $($h.Inf)（provider=$($h.Provider)）"; continue }
    Say "删除驱动包 $($h.Inf) ..."
    & pnputil /delete-driver $h.Inf /uninstall /force
    if ($LASTEXITCODE -eq 0) { Ok "  已删除 $($h.Inf)" } else { Warn "  删除失败（先拔掉节点再重试）" }
}

Say "重新扫描设备 ..."
& pnputil /scan-devices | Out-Null
Start-Sleep -Seconds 2

Ok "完成。请拔插节点，然后运行：python tools\lps_config.py --list"
