#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_wizard.py —— LPS Node 交互式配置向导（对话式 / 循环 / 状态机）

设计目标：上电后就在 PowerShell 里对话式完成所有事，不用记参数。

    python tools\\lps_wizard.py

它会自动检测 COM 口与 DFU 设备，然后给你一个菜单：
    刷固件 + 配置成基站 / 标签、只配置、查看配置、诊断、恢复卡住的 DFU
每做完一件事都回到菜单，方便一块接一块地配置（循环）。

针对异常状态的处理（引导状态机）：
    * 掉线 / 拔了线      → 提示重新插入，回到菜单
    * DFU 卡在残留状态   → 给出"按住 DFU 键重新上电"的恢复指引
    * 刷写中途失败       → 明确告知不会变砖，并给出恢复步骤
    * 节点没出 COM 口    → 自动诊断是否是 libusb 抢占了 CDC 接口

稳定性优先：刷写默认使用官方原速（严格按 STM32 上报的 5 秒等待，约 11 分钟/块）。
"""

import os
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import lps_flash as F          # noqa: E402  (USB / DFU / 刷写)

# 载入 DfuSe 实现（lps-tools 或 clones/lps-tools）
DFU_CLS, DFUSE = F.load_dfuse()

FW_DIR = ROOT / "firmware"
DEFAULT_FW = FW_DIR / "lps-node-firmware-2022.09.dfu"

MODE_KEYS = {
    "tdoa3": [b"m", b"4"],
    "tdoa2": [b"m", b"3"],
    "sniffer": [b"s"],
    "twr-anchor": [b"a"],
    "twr-tag": [b"t"],
}
MODE_NAME = {
    "tdoa3": "TDoA Anchor V3",
    "tdoa2": "TDoA Anchor V2",
    "sniffer": "Sniffer",
    "twr-anchor": "TWR Anchor",
    "twr-tag": "TWR Tag",
}

WARN_FLASHING = (
    "  ⚠ 刷写期间：不要拔 USB、不要按 Reset、不要打开官方 GUI（它会抢设备）\n"
    "  ⚠ 中途失败不会变砖：按住 DFU 键重新上电即可重来\n"
)


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------
def clear():
    os.system("cls" if os.name == "nt" else "clear")


def hr(title=""):
    print("=" * 66)
    if title:
        print("  " + title)
        print("=" * 66)


def ok(m):
    print("  [OK]   " + m)


def info(m):
    print("  [--]   " + m)


def warn(m):
    print("  [警告] " + m)


def err(m):
    print("  [失败] " + m)


def ask(prompt, default=""):
    s = input("  %s%s: " % (prompt, (" [%s]" % default) if default else "")).strip()
    return s if s else default


def pause(msg="按回车返回菜单 ..."):
    try:
        input("\n  " + msg)
    except KeyboardInterrupt:
        raise


# ---------------------------------------------------------------------------
# 状态扫描（状态机的"感知"部分）
# ---------------------------------------------------------------------------
def scan():
    """扫描当前硬件状态，返回 dict"""
    snap = {
        "ports": [],         # 正常运行模式的 LPS Node 串口
        "dfu": None,         # bState 或 None
        "usb_nodes": 0,      # USB 上能看到的 0483:5740 数量
        "driver_trouble": False,
    }
    try:
        snap["ports"] = F.find_node_ports()
    except Exception:
        pass
    try:
        d, st = F.open_dfu(DFUSE)
        if d is not None:
            snap["dfu"] = st
            F.release_dfu(d)
    except Exception:
        pass
    try:
        import usb.core
        snap["usb_nodes"] = len(list(
            usb.core.find(find_all=True, idVendor=F.NODE_VID, idProduct=F.NODE_PID) or []))
    except Exception:
        pass
    snap["driver_trouble"] = (snap["usb_nodes"] > 0 and not snap["ports"])
    return snap


def render(snap, message=None):
    print()
    hr("LPS Node 配置向导")
    print("  当前状态：")
    if snap["ports"]:
        for p in snap["ports"]:
            print("    ● 串口节点 : %-8s %s" % (p.device, p.description))
    else:
        print("    ○ 串口节点 : 无（节点要处于运行模式才会出现 COM 口）")
    if snap["dfu"] is None:
        print("    ○ DFU 设备 : 无")
    else:
        state_name = F.DFU_STUCK_STATES.get(snap["dfu"], "DFU_ERROR" if snap["dfu"] == 10 else "?")
        mark = "可刷写" if snap["dfu"] in (2, 10) else "需恢复"
        print("    ● DFU 设备 : 0483:df11  bState=%d (%s) → %s"
              % (snap["dfu"], state_name, mark))
    if snap["driver_trouble"]:
        print("    ⚠ 检测到 %d 个 USB 节点但没有串口 → 驱动被 libusb/Zadig 抢占"
              % snap["usb_nodes"])
    if message:
        print()
        for line in str(message).splitlines():
            print("  " + line)
    print()
    hr()
    print("  [1] 刷固件 + 配置为【基站】      (TDoA Anchor V3)")
    print("  [2] 刷固件 + 配置为【标签/数据出口】(Sniffer)")
    print("  [3] 只配置（不刷固件）")
    print("  [4] 查看节点当前配置")
    print("  [5] 重新扫描 / 诊断")
    print("  [6] 恢复卡住的 DFU 设备")
    print("  [q] 退出")
    hr()


# ---------------------------------------------------------------------------
# 串口读写（配置阶段）
# ---------------------------------------------------------------------------
def read_config(port, seconds=1.8):
    import serial
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        err("打不开串口 %s：%s" % (port, e))
        print("       官方 GUI（python -m lpstools）可能正占用它，关掉再试。")
        return None
    try:
        end = time.time() + seconds
        buf = b""
        while time.time() < end:
            chunk = ser.read(4096)
            if chunk:
                buf += chunk
    finally:
        ser.close()
    return buf.decode("utf-8", "replace")


def parse_banner(text):
    import re
    cfg = {}
    m = re.search(r"Address is 0x([0-9A-Fa-f]+)", text)
    if m:
        cfg["id"] = int(m.group(1), 16)
    m = re.search(r"Mode is (.+)", text)
    if m:
        cfg["mode"] = m.group(1).strip()
    m = re.search(r"Bitrate: (\w+)", text)
    if m:
        cfg["bitrate"] = m.group(1)
    m = re.search(r"Preamble: (\w+)", text)
    if m:
        cfg["preamble"] = m.group(1)
    cfg["ok_count"] = text.count("[OK]")
    cfg["bad_count"] = text.count("[FAIL]") + text.count("[ERROR]")
    return cfg


def write_config(port, node_id, mode, radio=None):
    import serial
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        err("打不开串口 %s：%s" % (port, e))
        return False
    try:
        end = time.time() + 1.2
        while time.time() < end:
            ser.read(4096)
        keys = ([str(node_id).encode()] if node_id < 10
                else [b"i", str(node_id).encode(), b"\n"])
        keys += MODE_KEYS[mode]
        if radio is not None:
            keys += [b"r", str(radio).encode()]
        for k in keys:
            ser.write(k)
            ser.flush()
            time.sleep(0.12)
        time.sleep(0.4)
    finally:
        ser.close()
    return True


def wait_port(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(p.device.upper() == port.upper() for p in F.list_ports_safe()):
            return True
        time.sleep(0.5)
    return False


def wait_new_port(before, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = {p.device for p in F.find_node_ports()}
        new = sorted(now - before)
        if len(new) == 1:
            return new[0]
        if len(new) > 1:
            warn("出现了多个新节点，取第一个 %s（建议只插一块）" % new[0])
            return new[0]
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# 动作：刷固件
# ---------------------------------------------------------------------------
def do_flash(fw_path):
    """刷写；返回 True 表示成功（设备已在重启中）"""
    if not fw_path.exists():
        err("固件不存在：%s" % fw_path)
        print("       请把 .dfu 固件放到 firmware\\ 目录（项目默认提供 2022.09 版本）")
        print("       或运行 bootstrap.ps1 自动下载。")
        return False

    d, state = F.open_dfu(DFUSE)
    if d is None:
        err("没有检测到 DFU 设备（0483:df11）")
        print("       让板子进刷机模式：拔掉 USB → 按住 DFU 键(6) → 插 USB → 松手")
        return False
    F.release_dfu(d)

    ready, msg = F.ensure_dfu_idle(DFUSE, recover=False)
    if not ready:
        err(msg)
        return False
    ok(msg)

    print()
    print(WARN_FLASHING)
    input("  准备就绪，按回车开始刷写（约 11 分钟）...")
    print()
    try:
        F.flash_file(DFU_CLS, fw_path)
    except SystemExit:
        err("刷写中断或失败 —— 不会变砖")
        print("       恢复：拔掉 USB → 按住 DFU 键(6) → 插 USB → 松手 → 回菜单重试")
        return False
    except Exception as e:
        err("刷写异常：%s: %s" % (type(e).__name__, e))
        print("       恢复：拔掉 USB → 按住 DFU 键(6) → 插 USB → 松手 → 回菜单重试")
        return False
    return True


# ---------------------------------------------------------------------------
# 动作：配置（写 ID + 模式）并校验
# ---------------------------------------------------------------------------
def do_configure(port, node_id, mode):
    print()
    info("配置前状态：")
    text = read_config(port)
    if text is None:
        return False
    before = parse_banner(text)
    print("       当前编号 = %s，模式 = %s" % (before.get("id", "?"), before.get("mode", "?")))

    if not write_config(port, node_id, mode):
        return False
    ok("已写入：编号 = %d，模式 = %s" % (node_id, MODE_NAME[mode]))
    print()
    print("  >> 让配置生效：按一下节点上的 Reset 键（右边靠下那个），或拔插一次 USB")
    try:
        input("     复位完成后按回车校验（Ctrl+C 放弃）...")
    except KeyboardInterrupt:
        raise

    if not wait_port(port, 20.0):
        err("串口 %s 没有再出现（可能换了 COM 号或没插好）" % port)
        print("       回菜单选 [5] 重新扫描查看。")
        return False
    text = read_config(port)
    cfg = parse_banner(text or "")
    print()
    good = True
    if cfg.get("id") == node_id:
        ok("编号 = %d" % node_id)
    else:
        warn("读回编号 = %s，期望 %d（再按一次 Reset 或拔插 USB 后重看）"
             % (cfg.get("id"), node_id))
        good = False
    if cfg.get("mode") == MODE_NAME[mode]:
        ok("模式 = %s" % MODE_NAME[mode])
    else:
        warn("读回模式 = %s，期望 %s" % (cfg.get("mode"), MODE_NAME[mode]))
        good = False
    if cfg.get("bad_count"):
        warn("开机自检有 %d 项失败，检查 USB 线/供电" % cfg["bad_count"])
        good = False
    else:
        ok("开机自检通过（%d 个 [OK]）" % cfg.get("ok_count", 0))
    info("比特率 %s / 前导码 %s" % (cfg.get("bitrate", "?"), cfg.get("preamble", "?")))
    return good


# ---------------------------------------------------------------------------
# 动作：一键（刷 + 配）
# ---------------------------------------------------------------------------
def action_flash_and_config(role):
    mode = "tdoa3" if role == "anchor" else "sniffer"
    label = "基站" if role == "anchor" else "标签/数据出口"
    default_id = "0" if role == "anchor" else "20"

    hr("刷固件 + 配置为【%s】（模式 %s）" % (label, MODE_NAME[mode]))
    snap = scan()

    # 没有 DFU 设备时，尽量帮忙进入
    if snap["dfu"] is None:
        if len(snap["ports"]) == 1:
            ans = ask("没有 DFU 设备。是否让脚本通过串口把 %s 送进 DFU？(y/n)"
                      % snap["ports"][0].device, "y")
            if ans.lower().startswith("y"):
                info("向 %s 发送 'u' ..." % snap["ports"][0].device)
                F.enter_dfu(snap["ports"][0].device, quiet=True)
                time.sleep(1.0)
                snap = scan()
        if snap["dfu"] is None:
            err("仍未检测到 DFU 设备")
            print("       请手动进入刷机模式：拔掉 USB → 按住 DFU 键(6) → 插 USB → 松手")
            print("       然后回菜单重试（菜单选 [5] 可重新扫描）")
            return

    node_id = ask("要设置的编号 ID (0-255)", default_id)
    try:
        node_id = int(node_id)
        assert 0 <= node_id <= 255
    except Exception:
        err("编号必须是 0-255 的整数")
        return

    before = {p.device for p in F.find_node_ports()}
    print()
    if not do_flash(DEFAULT_FW):
        return

    info("等待节点重启并出现串口 ...")
    port = wait_new_port(before, 30.0)
    if port is None:
        err("没等到新的节点串口")
        print("       按一下 Reset 键(4) 或拔插 USB，然后回菜单选 [3] 只配置 / [5] 扫描")
        return
    ok("串口 = %s" % port)
    do_configure(port, node_id, mode)


def action_config_only():
    hr("只配置（不刷固件）")
    snap = scan()
    if not snap["ports"]:
        if snap["driver_trouble"]:
            err("USB 上有节点但没有 COM 口 —— 驱动被 libusb/Zadig 抢占了")
            print("       解决：设备管理器 → libusb-win32 devices → 'Crazyflie 2.x (Interface 0)'")
            print("             → 更新驱动程序 → 从列表选 'USB 串行设备' → 拔插 USB")
        else:
            err("没有在线的节点（节点要在运行模式，不在 DFU 模式）")
            print("       板子刚上电时若停在 DFU，请拔掉 USB 后【不按键】重新插上")
        return

    if len(snap["ports"]) == 1:
        port = snap["ports"][0].device
        info("自动选中串口 %s" % port)
    else:
        for i, p in enumerate(snap["ports"]):
            print("    [%d] %s  %s" % (i + 1, p.device, p.description))
        sel = ask("选择节点序号", "1")
        try:
            port = snap["ports"][int(sel) - 1].device
        except Exception:
            err("序号无效")
            return

    role = ask("配置为 基站(a) / 标签(t) ?", "a").lower()[:1]
    mode = "tdoa3" if role == "a" else "sniffer"
    default_id = "0" if role == "a" else "20"
    node_id = ask("编号 ID (0-255)", default_id)
    try:
        node_id = int(node_id)
        assert 0 <= node_id <= 255
    except Exception:
        err("编号必须是 0-255 的整数")
        return
    do_configure(port, node_id, mode)


def action_read():
    hr("查看节点当前配置")
    ports = F.find_node_ports()
    if not ports:
        err("没有在线的节点（DFU 模式下不会出现 COM 口）")
        return
    for p in ports:
        text = read_config(p.device)
        cfg = parse_banner(text or "")
        print()
        print("  %s" % p.device)
        print("    编号   : %s" % cfg.get("id", "?"))
        print("    模式   : %s" % cfg.get("mode", "?"))
        print("    比特率 : %s    前导码: %s" % (cfg.get("bitrate", "?"), cfg.get("preamble", "?")))
        print("    自检   : %d 个 [OK]，%d 个异常" % (cfg.get("ok_count", 0), cfg.get("bad_count", 0)))


def action_recover():
    hr("恢复卡住的 DFU 设备")
    d, state = F.open_dfu(DFUSE)
    if d is None:
        info("当前没有 DFU 设备。")
        if F.find_node_ports():
            info("节点在运行模式，可以正常配置。")
        return

    if state in (2, 10):
        ok("DFU 状态正常（bState=%d），不需要恢复" % state)
        F.release_dfu(d)
        return
    name = F.DFU_STUCK_STATES.get(state, "未知")
    warn("DFU 卡在 bState=%d (%s)" % (state, name))
    print("     这是上次刷写被中断留下的残留状态，STM32 会拒绝新命令。")
    print("     唯一可靠的恢复动作：")
    print("        拔掉 USB  →  按住 DFU 键(6)  →  插入 USB  →  松手")
    try:
        F.release_dfu(d)
    except Exception:
        pass


def action_diagnose():
    hr("诊断")
    snap = scan()
    print("  串口节点数 : %d" % len(snap["ports"]))
    print("  USB 节点数 : %d" % snap["usb_nodes"])
    print("  DFU 状态   : %s" % (snap["dfu"] if snap["dfu"] is not None else "无"))
    if snap["driver_trouble"]:
        print()
        warn("USB 上有节点但没有 COM 口（典型 Zadig/libusb 抢占）")
        print("     确认当前哪些设备被占用：")
        print("       powershell -ExecutionPolicy Bypass -File .\\tools\\fix_serial_driver.ps1")
        print("     修复方式见上面输出（推荐逐个节点在设备管理器里改，不要删 radio 驱动）")
    else:
        ok("驱动看起来正常")
    print()
    info("提示：哪块板子要改驱动，就只插哪一块，避免一次影响多台设备。")


# ---------------------------------------------------------------------------
# 主循环（状态机）
# ---------------------------------------------------------------------------
def main():
    hr("LPS Node 配置向导")
    print("  上电后在这里完成：自动识别 COM → 刷固件 / 配置节点，循环可用。")
    print("  固件目录：firmware\\   默认：%s" % DEFAULT_FW.name)
    print("  刷写稳定性优先：默认官方原速，约 11 分钟/块。")
    if not DEFAULT_FW.exists():
        warn("默认固件不存在：%s（请放入 firmware\\ 或运行 bootstrap.ps1）" % DEFAULT_FW)

    last_msg = ""
    while True:
        try:
            snap = scan()
            clear()
            render(snap, last_msg)
            last_msg = ""
            choice = input("  请选择操作: ").strip().lower()
        except KeyboardInterrupt:
            print("\n  已退出。")
            return

        try:
            if choice == "1":
                action_flash_and_config("anchor")
                last_msg = "上一步：刷固件并配置为【基站】"
            elif choice == "2":
                action_flash_and_config("tag")
                last_msg = "上一步：刷固件并配置为【标签/数据出口】"
            elif choice == "3":
                action_config_only()
                last_msg = "上一步：只配置"
            elif choice == "4":
                action_read()
                last_msg = "上一步：查看配置"
            elif choice == "5":
                action_diagnose()
                last_msg = "上一步：诊断"
            elif choice == "6":
                action_recover()
                last_msg = "上一步：DFU 恢复"
            elif choice in ("q", "quit", "exit"):
                print("\n  已退出。")
                return
            else:
                last_msg = "无效选择：%s" % choice
                continue
            pause()
        except KeyboardInterrupt:
            print("\n  已中断当前操作，返回菜单。")
            continue
        except Exception as e:
            err("操作出错：%s: %s" % (type(e).__name__, e))
            pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  已退出。")
