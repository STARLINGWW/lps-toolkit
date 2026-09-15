# ============================================================================
#  fix_serial_driver.ps1 —— 让 LPS Node 恢复出现 COM 口
#
#  背景：Crazyflie 与 LPS Node 的 USB VID:PID 完全相同（0483:5740）。
#  若之前用 Zadig 给 Crazyflie 装过 libusb-win32 驱动，它会按硬件 ID
#  (VID_0483&PID_5740&MI_00) 把 LPS Node 的 CDC 接口也抢走，Windows 就不会
#  创建 COM 口。表现：
#      python tools\lps_config.py --list
#      → 检测到 0483:5740 USB 设备，但系统没有生成任何串口
#
#  这个绑定是【按设备实例】的：手动给一块板子换过驱动后，换另一块板子插上
#  又会变回 libusb。要一次性解决，就删掉那个驱动包（本脚本 -Apply 干的活）。
#
#  用法（默认只读检查，不改动系统）：
#      powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1
#      # 确认后，用【管理员】身份执行：
#      powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1 -Apply
#
#  代价：删掉后 Crazyflie 用 USB 直连（cflib 的 usb://）会失效，需要时用
#        Zadig 重新装回即可（Crazyradio 无线不受影响）。
#        DFU 刷机口 (0483:df11) 的驱动不动，刷写不受影响。
# ============================================================================

param([switch]$Apply)

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
Write-Host " LPS Node 串口驱动修复"
Write-Host "=============================================="

# --- 1. 找出抢走 CDC 接口的驱动包 -------------------------------------------
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

Say "匹配硬件 ID '$TargetId' 的驱动包："
if ($hits.Count -eq 0) {
    Ok "  没有（说明没有第三方驱动抢占，问题不在驱动）"
} else {
    foreach ($h in $hits) {
        if ($h.Provider -like '*libusb*') {
            Write-Host ("   {0}  provider={1}   <-- 抢占了 CDC 接口" -f $h.Inf, $h.Provider)
        } else {
            Write-Host ("   {0}  provider={1}" -f $h.Inf, $h.Provider)
        }
    }
}

Say "当前 0483:5740 设备的驱动绑定："
$devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like '*VID_0483&PID_5740*' }
if (-not $devs) {
    Warn "  当前没有插着 LPS Node"
} else {
    foreach ($d in $devs) {
        $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverInfPath -ErrorAction SilentlyContinue).Data
        Write-Host ("   {0,-38} INF={1}" -f $d.FriendlyName, $inf)
    }
}

Say "当前串口："
$ports = [System.IO.Ports.SerialPort]::GetPortNames()
if ($ports) { Write-Host ("   " + ($ports -join ', ')) } else { Write-Host "   无" }

if ($hits.Count -eq 0) {
    Write-Host ""
    Ok "无需修复。"
    exit 0
}

if (-not $Apply) {
    Write-Host ""
    Warn "当前是【只读检查】模式，未做任何改动。"
    Write-Host ""
    Write-Host "  一键修复（推荐，对以后所有节点都生效）："
    Write-Host "    以【管理员】身份打开 PowerShell，然后执行："
    Write-Host "      cd D:\_AAA_CODE_ALL\UWB"
    Write-Host "      powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1 -Apply"
    Write-Host ""
    Write-Host "  或者只修当前这一块（图形界面，不影响 Crazyflie）："
    Write-Host "    设备管理器 → libusb-win32 devices → 'Crazyflie 2.x (Interface 0)'"
    Write-Host "    → 更新驱动程序 → 浏览我的电脑 → 让我从列表中选取"
    Write-Host "    → 选 'USB 串行设备' → 完成 → 拔插 USB"
    Write-Host "    （这种方式是按设备实例生效的，换另一块板子要再做一次）"
    exit 0
}

# --- 2. 执行修复 ------------------------------------------------------------
if (-not (Test-Admin)) {
    Fail "需要管理员权限。请用管理员身份重新打开 PowerShell 再运行本脚本。"
    exit 1
}

foreach ($h in $hits) {
    if ($h.Provider -notlike '*libusb*') {
        Warn "跳过 $($h.Inf)（provider=$($h.Provider)）"
        continue
    }
    Say "删除驱动包 $($h.Inf) ..."
    & pnputil /delete-driver $h.Inf /uninstall /force
    if ($LASTEXITCODE -eq 0) {
        Ok "  已删除 $($h.Inf)"
    } else {
        Warn "  删除失败（可能仍被占用：先拔掉节点再重试）"
    }
}

Say "重新扫描设备 ..."
& pnputil /scan-devices | Out-Null
Start-Sleep -Seconds 2

Ok "完成。请拔插一次节点，然后验证："
Write-Host "      python tools\lps_config.py --list"
Write-Host "  应能看到 'USB 串行设备 (COMx)'。"
