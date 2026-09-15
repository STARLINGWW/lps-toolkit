#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_wizard.py — Loco Positioning System (LPS) node console

Interactive, bilingual (English / 中文) console for Bitcraze LPS Node:

    python tools\\lps_wizard.py

    [0] switch language (EN / CN)      [1] flash firmware
    [2] configure only                [3] show current configuration
    [4] COM scan / diagnostics        [5] recover an interrupted DFU device

Design notes
------------
* A node only exposes a serial port while it runs the application. In DFU mode
  it appears as a USB DFU device instead, and never as a COM port.
* Flashing uses the official timing by default (honouring the 5 s poll timeout
  reported by the STM32 bootloader): about 10 minutes per board, and that is the
  stable choice. Compressing the wait causes mid-flash failures.
* An interrupted flash cannot brick a node: the DFU bootloader lives in ROM.
  Recover with: unplug → hold BM & DFU → plug in → release.
"""

import os
import sys
import time
import unicodedata
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import lps_flash as F          # noqa: E402  (USB / DFU / flashing)
import lps_config as C         # noqa: E402  (serial console / capability probe)

FW_DIR = ROOT / "firmware"
DEFAULT_FW = FW_DIR / "lps-node-firmware-2022.09.dfu"
PROJECT = "lps-toolkit"

# ---------------------------------------------------------------------------
# Mode tables
# ---------------------------------------------------------------------------
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
# key, display name, note (en), note (cn), default id
MODE_TABLE = [
    ("sniffer",    "Sniffer",        "passive listener / data output", "纯被动监听 / 数据出口", "20"),
    ("tdoa3",      "TDoA Anchor V3", "anchor, unlimited count, multi-room", "锚点，数量不限、可跨房间", "0"),
    ("tdoa2",      "TDoA Anchor V2", "anchor, max 8, fallback for TDoA3", "锚点，最多 8 个，TDoA3 的兜底", "0"),
    ("twr-anchor", "TWR Anchor",     "two-way-ranging anchor", "双向测距锚点", "0"),
    ("twr-tag",    "TWR Tag",        "official TWR tag (one at a time, ranges only)", "官方 TWR 标签（一次一个，只输出距离）", "20"),
]
# shortcut menu: 0 = tag/data output, 1 = anchor
SHORTCUT = {"0": "sniffer", "1": "tdoa3"}


def mode_note(mode):
    i = 2 if LANG["code"] == "en" else 3
    for row in MODE_TABLE:
        if row[0] == mode:
            return row[i]
    return ""


def mode_default_id(mode):
    for row in MODE_TABLE:
        if row[0] == mode:
            return row[4]
    return "0"


# ---------------------------------------------------------------------------
# Localisation (English is the default language)
# ---------------------------------------------------------------------------
LANG = {"code": "en"}

S = {
    "en": {
        "title": "LPS node console",
        "tagline": "%s — flash / configure / inspect Bitcraze LPS Nodes",
        "status": "Status",
        "com_scan": "COM scan      :",
        "com_found": "run-mode node(s): %s",
        "com_none": "no run-mode node",
        "com_hint1": "A node only gets a COM port while it runs the application (not in DFU mode).",
        "com_hint2": "If it is in run mode but you cannot connect, fix the serial driver per node",
        "com_hint3": "(see docs/操作流程.md — Windows driver section).",
        "fw_ok": "%-8s firmware OK: %d modes incl. TDoA3   current: %s / ID %s   [no flashing needed]",
        "fw_old2": "%-8s firmware old: TDoA2 only   current: %s / ID %s   [flash to get TDoA3]",
        "fw_old1": "%-8s firmware very old   [flashing required]",
        "fw_unknown": "%-8s firmware unknown (probe failed)",
        "node_fw": "firmware",
        "node_cur": "current",
        "node_verdict": "verdict",
        "fw_sum_ok": "OK, %d modes incl. TDoA3",
        "fw_sum_old2": "old: TDoA2 only, no TDoA3",
        "fw_sum_old1": "very old (no TDoA2 either)",
        "fw_sum_unknown": "unknown (probe failed)",
        "verdict_ok": "usable as-is, no flashing needed",
        "verdict_old2": "flash to get TDoA3",
        "verdict_old1": "flashing required",
        "dfu_verdict_ready": "flashable (menu [1])",
        "dfu_verdict_stuck": "needs recovery (menu [5])",
        "dfu": "DFU device    :",
        "dfu_none": "none",
        "dfu_ready": "0483:df11  bState=%d (%s)  -> flashable",
        "dfu_stuck": "0483:df11  bState=%d (%s)  -> needs recovery",
        "menu0": "[0] Language: %s   (press 0 to switch EN/CN)",
        "menu1": "[1] Flash firmware (est. 10 min+)",
        "menu2": "[2] Configure only (no flashing)",
        "menu3": "[3] Show current node configuration",
        "menu4": "[4] COM scan / diagnostics",
        "menu5": "[5] Recover an interrupted DFU device",
        "menuq": "[q] Quit",
        "select": "Select",
        "bye": "Bye.",
        "enter_to_menu": "Press Enter to return to the main menu ...",
        "enter_to_continue": "Press Enter to continue ...",
        "invalid": "Invalid choice: %s",
        "invalid_retry": "Invalid input: %s — please try again",
        "hint_q": "(q = back)",
        "back_to_menu": "Back to the main menu.",
        "mode_prompt": "Target mode:",
        "mode_shortcut": "[0] Tag / data output (Sniffer)      [1] Anchor / base (TDoA Anchor V3)      [2] More official modes",
        "mode_choose": "Select mode",
        "mode_more": "All modes supported by the official firmware:",
        "mode_unsupported": "[not supported by current firmware]",
        "id_prompt": "Node ID (0-255)",
        "id_bad": "ID must be an integer 0-255",
        "no_dfu": "No DFU device detected.",
        "no_dfu_manual": "Put the board in DFU mode: unplug USB → hold BM & DFU → plug in → release.",
        "send_u": "Send 'u' to %s to enter DFU mode? (y/n)",
        "dfu_stuck_msg": "The DFU device is stuck in bState=%d (%s) — left over from an interrupted flash. Use option [5].",
        "flash_intro": "Flashing — what happens next:",
        "flash_intro1": "- takes about 10 minutes (official timing: the stable choice)",
        "flash_intro2": "- do NOT unplug USB, do NOT press RESET, do NOT open the official GUI",
        "flash_intro3": "- progress is shown with elapsed / remaining time",
        "flash_intro4": "- an interrupted flash cannot brick the node (DFU bootloader is in ROM)",
        "fw_already_ok": "This node's firmware is already usable (supports TDoA3).",
        "fw_already_ok2": "Option [2] can change its role in seconds without re-flashing.",
        "flash_anyway": "Flash anyway? (y/N)",
        "start_flash": "Type y then Enter to start flashing (anything else cancels):",
        "flashing": "Flashing",
        "flash_failed": "Flashing failed or was interrupted — the node is not bricked.",
        "flash_recover": "Recover: unplug USB → hold BM & DFU → plug in → release, then try again.",
        "wait_port": "Waiting for the node to reboot and expose a serial port ...",
        "port_found": "serial port = %s",
        "port_missing": "No new serial port appeared.",
        "port_missing_hint": "Press the RESET button (or replug USB), then use option [2] or [4].",
        "cfg_now": "Current configuration read from the node:",
        "writing": "Writing configuration ...",
        "cfg_written": "Written: ID %d, mode %s",
        "do_reset": "Apply it: press the RESET button on the node, or replug USB.",
        "after_reset": "Press Enter after the reset ...",
        "verify": "Verifying ...",
        "done": "Configuration complete",
        "done_port": "serial port",
        "done_id": "node ID",
        "done_mode": "mode",
        "done_radio": "bitrate/preamble",
        "done_selftest": "boot self-test",
        "selftest_ok": "%d x [OK]",
        "selftest_bad": "%d x [ERROR]",
        "mismatch": "read back %s, expected %s (press RESET or replug, then check again)",
        "no_node": "No run-mode node found.",
        "no_node_hint": "A node in DFU mode has no COM port. Unplug it and plug it back in without pressing any button.",
        "driver_trouble": "A 0483:5740 device is on USB but Windows created no COM port.",
        "driver_how": "Device Manager → libusb-win32 devices → 'Crazyflie 2.x (Interface 0)' → Update driver →",
        "driver_how2": "Browse my computer → Let me pick from a list → 'USB Serial Device' → replug.",
        "driver_note": "Do this per node; do not remove the Crazyradio driver.",
        "diag_title": "COM scan / diagnostics",
        "diag_ports": "COM ports (all): %s",
        "diag_nodes": "LPS nodes (VID 0483 PID 5740): %d",
        "diag_usb": "USB 0483:5740 devices: %d",
        "diag_dfu": "DFU device: %s",
        "diag_driver_ok": "Serial driver looks fine.",
        "recover_title": "Recover an interrupted DFU device",
        "recover_none": "No DFU device present.",
        "recover_ok": "DFU state is normal (bState=%d) — nothing to recover.",
        "recover_do": "Recovery (the only reliable action):",
        "recover_do2": "unplug USB → hold BM & DFU → plug in → release",
        "press0": "0",
    },
    "cn": {
        "title": "LPS 节点控制台",
        "tagline": "%s — 刷写 / 配置 / 查看 Bitcraze LPS Node",
        "status": "当前状态",
        "com_scan": "COM 扫描结果  :",
        "com_found": "运行模式节点：%s",
        "com_none": "未检测到运行模式节点",
        "com_hint1": "节点只有在运行模式才会出现 COM 口（DFU 模式下不会）。",
        "com_hint2": "若处于运行模式但连不上，请按文档为每个 LPS Node 修改串口驱动",
        "com_hint3": "（见 docs/操作流程.md 的 Windows 驱动章节）。",
        "fw_ok": "%-8s 固件 OK：%d 种模式（含 TDoA3）  当前: %s / ID %s  [无需刷固件]",
        "fw_old2": "%-8s 固件偏旧：只有 TDoA2  当前: %s / ID %s  [要 TDoA3 需刷固件]",
        "fw_old1": "%-8s 固件过旧  [必须刷固件]",
        "fw_unknown": "%-8s 固件未知（探测失败）",
        "node_fw": "固件",
        "node_cur": "当前",
        "node_verdict": "结论",
        "fw_sum_ok": "OK，%d 种模式（含 TDoA3）",
        "fw_sum_old2": "偏旧：只有 TDoA2，没有 TDoA3",
        "fw_sum_old1": "过旧（连 TDoA2 都没有）",
        "fw_sum_unknown": "未知（探测失败）",
        "verdict_ok": "无需刷固件，可直接配置",
        "verdict_old2": "要 TDoA3 需刷固件",
        "verdict_old1": "需要刷固件",
        "dfu_verdict_ready": "可刷写（菜单 [1]）",
        "dfu_verdict_stuck": "需要恢复（菜单 [5]）",
        "dfu": "DFU 设备    :",
        "dfu_none": "无",
        "dfu_ready": "0483:df11  bState=%d (%s)  -> 可刷写",
        "dfu_stuck": "0483:df11  bState=%d (%s)  -> 需要恢复",
        "menu0": "[0] 语言：%s   （按 0 切换 中文/EN）",
        "menu1": "[1] 刷固件（预计 10 分钟以上）",
        "menu2": "[2] 改配置（不刷固件）",
        "menu3": "[3] 查看当前节点配置",
        "menu4": "[4] COM 扫描 / 诊断",
        "menu5": "[5] 恢复意外中断的 DFU 设备",
        "menuq": "[q] 退出",
        "select": "请选择操作",
        "bye": "已退出。",
        "enter_to_menu": "按回车返回主界面 ...",
        "enter_to_continue": "按回车继续 ...",
        "invalid": "无效选择：%s",
        "invalid_retry": "无效输入：%s —— 请重新输入",
        "hint_q": "(q=返回主菜单)",
        "back_to_menu": "已返回主界面。",
        "mode_prompt": "请选择节点配置模式：",
        "mode_shortcut": "[0] 标签 / 数据出口 (Sniffer)      [1] 基站 / 锚点 (TDoA Anchor V3)      [2] 更多官方模式",
        "mode_choose": "选择模式",
        "mode_more": "官方固件支持的全部模式：",
        "mode_unsupported": "[当前固件不支持]",
        "id_prompt": "节点序号 ID (0-255)",
        "id_bad": "ID 必须是 0-255 的整数",
        "no_dfu": "没有检测到 DFU 设备。",
        "no_dfu_manual": "让板子进入 DFU 模式：拔掉 USB → 按住 BM & DFU 键 → 插 USB → 松手。",
        "send_u": "是否让脚本通过串口把 %s 送进 DFU？(y/n)",
        "dfu_stuck_msg": "DFU 设备卡在 bState=%d (%s)——上次刷写中断的残留状态，请用菜单 [5]。",
        "flash_intro": "刷写说明（接下来会发生什么）：",
        "flash_intro1": "- 约需 10 分钟（官方原速，最稳的选择）",
        "flash_intro2": "- 期间不要拔 USB、不要按 RESET、不要打开官方 GUI",
        "flash_intro3": "- 进度条会显示已用时间与预计剩余时间",
        "flash_intro4": "- 中途失败不会变砖（DFU 引导在芯片 ROM 里）",
        "fw_already_ok": "该节点固件已经可用（支持 TDoA3）。",
        "fw_already_ok2": "用菜单 [2] 改配置只需几秒钟，不必重新刷。",
        "flash_anyway": "仍然要刷固件吗？(y/N)",
        "start_flash": "输入 y 再回车才会开始刷写（其它输入取消）：",
        "flashing": "刷写中",
        "flash_failed": "刷写失败或被中断 —— 节点不会变砖。",
        "flash_recover": "恢复：拔掉 USB → 按住 BM & DFU 键 → 插 USB → 松手，然后重试。",
        "wait_port": "等待节点重启并出现串口 ...",
        "port_found": "串口 = %s",
        "port_missing": "没有等到新的串口。",
        "port_missing_hint": "按一下 RESET 键（或拔插 USB），然后用菜单 [2] 或 [4]。",
        "cfg_now": "当前从节点读到的配置：",
        "writing": "正在写入配置 ...",
        "cfg_written": "已写入：编号 %d，模式 %s",
        "do_reset": "让配置生效：按一下节点上的 RESET 键，或拔插 USB。",
        "after_reset": "复位完成后按回车 ...",
        "verify": "正在校验 ...",
        "done": "配置完成",
        "done_port": "串口",
        "done_id": "编号 ID",
        "done_mode": "模式",
        "done_radio": "射频",
        "done_selftest": "开机自检",
        "selftest_ok": "%d 个 [OK]",
        "selftest_bad": "%d 个 [ERROR]",
        "mismatch": "读回 %s，期望 %s（按 RESET 或拔插 USB 后再看一次）",
        "no_node": "没有检测到运行模式的节点。",
        "no_node_hint": "DFU 模式下不会有 COM 口。请拔掉 USB，不按任何键重新插上。",
        "driver_trouble": "USB 上有 0483:5740 设备，但 Windows 没有创建 COM 口。",
        "driver_how": "设备管理器 → libusb-win32 devices → 'Crazyflie 2.x (Interface 0)' → 更新驱动程序 →",
        "driver_how2": "浏览我的电脑 → 让我从列表中选取 → 'USB 串行设备' → 拔插 USB。",
        "driver_note": "每个节点各做一次；不要直接卸载 Crazyradio 的驱动。",
        "diag_title": "COM 扫描 / 诊断",
        "diag_ports": "系统串口：%s",
        "diag_nodes": "LPS 节点（VID 0483 PID 5740）：%d",
        "diag_usb": "USB 上的 0483:5740 设备：%d",
        "diag_dfu": "DFU 设备：%s",
        "diag_driver_ok": "串口驱动看起来正常。",
        "recover_title": "恢复意外中断的 DFU 设备",
        "recover_none": "当前没有 DFU 设备。",
        "recover_ok": "DFU 状态正常（bState=%d），无需恢复。",
        "recover_do": "恢复动作（唯一可靠）：",
        "recover_do2": "拔掉 USB → 按住 BM & DFU 键 → 插 USB → 松手",
        "press0": "0",
    },
}


def t(key, *args):
    s = S[LANG["code"]].get(key, key)
    return s % args if args else s


# ---------------------------------------------------------------------------
# Small console helpers
# ---------------------------------------------------------------------------
def clear():
    os.system("cls" if os.name == "nt" else "clear")


def hr(ch="="):
    print(ch * 66)


def ok(m):
    print("  [OK]   " + m)


def info(m):
    print("  [--]   " + m)


def warn(m):
    print("  [!!]   " + m)


def err(m):
    print("  [ERR]  " + m)


class Back(Exception):
    """用户输入 q 要求返回上一级 —— 一路回退到主菜单。"""


BACK_WORDS = ("q", "quit", "exit", "back", "b")


def _read(prompt, hint=""):
    try:
        return input("  %s%s " % (prompt, hint)).strip()
    except EOFError:
        raise Back()          # 输入结束（管道/EOF）也当作返回，不要继续往下走


def ask(prompt, default=""):
    """普通提问：q 返回上一级；空回车取默认值。"""
    raw = _read(prompt, (" [%s]" % default if default else "") + "  " + t("hint_q"))
    if raw.lower() in BACK_WORDS:
        raise Back()
    return raw if raw else default


def ask_choice(prompt, options, default=None):
    """从 options（key -> 说明，可为 None）里选一个 key。

    * 非法输入 → 提示后【留在本界面重新询问】，绝不继续往下走
    * 输入 q → 抛 Back，返回主菜单
    """
    keys = list(options.keys())
    while True:
        hint = " [%s]  %s" % (default if default else "/".join(keys), t("hint_q"))
        raw = _read(prompt, hint).lower()
        if raw in BACK_WORDS:
            raise Back()
        if raw == "" and default:
            return default
        if raw in options:
            return raw
        warn(t("invalid_retry", raw))


def confirm_yn(prompt, default="n"):
    """y/n 询问：非法输入重问，q 返回上一级。"""
    while True:
        hint = " [%s]  %s" % ("Y/n" if default == "y" else "y/N", t("hint_q"))
        raw = _read(prompt, hint).lower()
        if raw in BACK_WORDS:
            raise Back()
        if raw == "":
            raw = default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        warn(t("invalid_retry", raw))


def disp_width(s):
    """字符串的显示宽度：中日韩全角字符按 2 列算，否则中文列会对不齐。"""
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width):
    s = str(s)
    return s + " " * max(0, width - disp_width(s))


def pause():
    try:
        input("\n  " + t("enter_to_menu"))
    except (EOFError, KeyboardInterrupt):
        pass


def pause_continue():
    try:
        input("  " + t("enter_to_continue"))
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Scan / probe
# ---------------------------------------------------------------------------
def scan():
    snap = {"nodes": [], "dfu": None, "usb_nodes": 0, "driver_trouble": False,
            "all_ports": []}
    try:
        snap["all_ports"] = [p.device for p in F.list_ports_safe()]
    except Exception:
        pass
    try:
        for p in F.find_node_ports():
            fw = None
            try:
                fw = C.probe_firmware(p.device, quiet=True)
            except Exception:
                fw = None
            snap["nodes"].append({"device": p.device, "desc": p.description, "fw": fw})
    except Exception:
        pass
    try:
        d, st = F.open_dfu(F.load_dfuse()[1])
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
    snap["driver_trouble"] = (snap["usb_nodes"] > 0 and not snap["nodes"])
    return snap


def dfu_state_name(state):
    return F.DFU_STUCK_STATES.get(state, "DFU_ERROR" if state == 10 else "?")


def render(snap, message=None):
    clear()
    hr()
    print("  " + t("title"))
    print("  " + t("tagline", PROJECT))
    hr()
    print("  " + t("status"))
    # --- COM scan result (the primary thing the user needs to see) ---
    if snap["nodes"]:
        print("    " + t("com_found", ", ".join(n["device"] for n in snap["nodes"])))
    else:
        print("    " + t("com_scan") + " " + t("com_none"))
        print("           " + t("com_hint1"))
        print("           " + t("com_hint2"))
        print("           " + t("com_hint3"))
    # --- firmware capability per node ---
    for n in snap["nodes"]:
        fw = n["fw"]
        print("      " + n["device"])
        if not fw:
            print("        " + pad(t("node_fw") + ":", 12) + t("fw_sum_unknown"))
            continue
        nid = fw.get("address", fw.get("id", "?"))
        cur = "%s / ID %s" % (fw.get("mode", "?"), nid)
        if fw.get("has_tdoa3"):
            summary, verdict = t("fw_sum_ok", len(fw.get("modes", []))), t("verdict_ok")
        elif fw.get("has_tdoa2"):
            summary, verdict = t("fw_sum_old2"), t("verdict_old2")
        else:
            summary, verdict = t("fw_sum_old1"), t("verdict_old1")
        print("        " + pad(t("node_fw") + ":", 12) + summary)
        print("        " + pad(t("node_cur") + ":", 12) + cur)
        print("        " + pad(t("node_verdict") + ":", 12) + verdict)
    if snap["driver_trouble"]:
        print()
        warn(t("driver_trouble"))
        print("           " + t("driver_how"))
        print("           " + t("driver_how2"))
        print("           " + t("driver_note"))
    # --- DFU ---
    if snap["dfu"] is None:
        print("    " + t("dfu") + " " + t("dfu_none"))
    elif snap["dfu"] in (2, 10):
        print("    " + t("dfu") + " " + t("dfu_ready", snap["dfu"], dfu_state_name(snap["dfu"])))
        print("        " + pad(t("node_verdict") + ":", 12) + t("dfu_verdict_ready"))
    else:
        print("    " + t("dfu") + " " + t("dfu_stuck", snap["dfu"], dfu_state_name(snap["dfu"])))
        print("        " + pad(t("node_verdict") + ":", 12) + t("dfu_verdict_stuck"))
    if message:
        print()
        for line in str(message).splitlines():
            print("  " + line)
    print()
    hr()
    print("  " + t("menu0", "EN" if LANG["code"] == "en" else "中文"))
    print("  " + t("menu1"))
    print("  " + t("menu2"))
    print("  " + t("menu3"))
    print("  " + t("menu4"))
    print("  " + t("menu5"))
    print("  " + t("menuq"))
    hr()


# ---------------------------------------------------------------------------
# Mode / ID prompts
# ---------------------------------------------------------------------------
def ask_mode(supported=None):
    print()
    print("  " + t("mode_prompt"))
    print("    " + t("mode_shortcut"))
    sel = ask_choice(t("mode_choose"), {"0": None, "1": None, "2": None}, default="1")
    if sel in SHORTCUT:
        return SHORTCUT[sel]
    # sel == "2"：列出官方固件支持的全部模式
    print()
    print("  " + t("mode_more"))
    opts = {}
    for i, row in enumerate(MODE_TABLE):
        key, name = row[0], row[1]
        mark = ""
        if supported is not None and name not in supported:
            mark = "   " + t("mode_unsupported")
        print("    [%d] %s%s%s" % (i + 1, pad(name, 18), mode_note(key), mark))
        opts[str(i + 1)] = None
    sel2 = ask_choice(t("mode_choose"), opts, default="2")
    return MODE_TABLE[int(sel2) - 1][0]


def ask_id(mode):
    dflt = mode_default_id(mode)
    while True:
        raw = _read(t("id_prompt"), " [%s]  %s" % (dflt, t("hint_q")))
        if raw.lower() in BACK_WORDS:
            raise Back()
        if raw == "":
            return int(dflt)
        if raw.isdigit() and 0 <= int(raw) <= 255:
            return int(raw)
        warn(t("invalid_retry", raw if raw else t("id_bad")))


# ---------------------------------------------------------------------------
# Flashing + configuration
# ---------------------------------------------------------------------------
def ensure_dfu(snap):
    """确保有可用的 DFU 设备；返回 True/False"""
    if snap["dfu"] is not None:
        if snap["dfu"] not in (2, 10):
            warn(t("dfu_stuck_msg", snap["dfu"], dfu_state_name(snap["dfu"])))
            return False
        return True

    if len(snap["nodes"]) == 1:
        if confirm_yn(t("send_u", snap["nodes"][0]["device"]), default="y"):
            info("u -> %s" % snap["nodes"][0]["device"])
            F.enter_dfu(snap["nodes"][0]["device"], quiet=True)
            time.sleep(1.0)
            s2 = scan()
            if s2["dfu"] is not None:
                return True
    warn(t("no_dfu"))
    print("        " + t("no_dfu_manual"))
    return False


def flash_fw():
    print()
    info(t("flash_intro"))
    print("      " + t("flash_intro1"))
    print("      " + t("flash_intro2"))
    print("      " + t("flash_intro3"))
    print("      " + t("flash_intro4"))
    if not DEFAULT_FW.exists():
        err("firmware not found: %s" % DEFAULT_FW)
        print("        Put the .dfu file into: %s" % FW_DIR)
        return False
    if not confirm_yn(t("start_flash"), default="n"):
        info("Cancelled - nothing was written.")
        return False
    try:
        F.flash_file(F.load_dfuse()[0], DEFAULT_FW)
    except SystemExit:
        err(t("flash_failed"))
        print("        " + t("flash_recover"))
        return False
    except Exception as e:
        err("%s: %s" % (type(e).__name__, e))
        print("        " + t("flash_recover"))
        return False
    return True


def show_node_config(port, text=None, cfg=None):
    """竖向展示节点配置。

    cfg 可直接传探测结果（probe_firmware 的返回值），省一次串口往返，
    也避免"刚探测完又重开串口读不到横幅"的问题。
    """
    if cfg is None:
        if text is None:
            text = C.read_config_text(port) or ""
        cfg = C.parse_banner(text)
    if text is None:
        text = ""
    # 竖排、逐行一个字段，并按显示宽度对齐（中文按 2 列）
    print("    " + pad(t("done_id") + ":", 20) + str(cfg.get("address", "?")))
    print("    " + pad(t("done_mode") + ":", 20) + str(cfg.get("mode", "?")))
    print("    " + pad(t("done_radio") + ":", 20) +
          "%s / %s" % (cfg.get("bitrate", "?"), cfg.get("preamble", "?")))
    src = text or cfg.get("raw", "")
    bad = src.count("[FAIL]") + src.count("[ERROR]")
    good = src.count("[OK]")
    print("    " + pad(t("done_selftest") + ":", 20) +
          (t("selftest_bad", bad) if bad else t("selftest_ok", good)))
    return cfg


def write_and_verify(port, node_id, mode):
    print()
    info(t("writing"))
    if not C.write_config(port, node_id, mode, MODE_KEYS):
        return False
    ok(t("cfg_written", node_id, MODE_NAME[mode]))
    print()
    print("  >> " + t("do_reset"))
    try:
        input("     " + t("after_reset"))
    except (EOFError, KeyboardInterrupt):
        pass
    info(t("verify"))
    if not C.wait_port(port, 20.0):
        text = C.read_config_text(port) or ""
        if not text:
            warn(t("port_missing"))
            print("        " + t("port_missing_hint"))
            return False
    text = C.read_config_text(port) or ""
    cfg = C.parse_banner(text)
    hr()
    print("  " + t("done"))
    print("    %-18s %s" % (t("done_port"), port))
    show_node_config(port, text)
    if cfg.get("address") != node_id:
        warn(t("mismatch", cfg.get("address"), node_id))
        return False
    if cfg.get("mode") != MODE_NAME[mode]:
        warn(t("mismatch", cfg.get("mode"), MODE_NAME[mode]))
        return False
    return True


def action_flash(snap):
    mode = ask_mode()
    node_id = ask_id(mode)
    # 固件已经可用时先提醒，避免白等 10 分钟
    for n in snap["nodes"]:
        fw = n["fw"]
        if fw and fw.get("has_tdoa3"):
            print()
            warn(t("fw_already_ok"))
            print("        " + t("fw_already_ok2"))
            if not confirm_yn(t("flash_anyway"), default="n"):
                info("OK - use menu [2] to change the configuration instead.")
                return
            break
    before = {p.device for p in F.find_node_ports()}
    if not ensure_dfu(snap):
        return
    if not flash_fw():
        return
    info(t("wait_port"))
    port = C.wait_new_port(before, 30.0)
    if not port:
        warn(t("port_missing"))
        print("        " + t("port_missing_hint"))
        return
    ok(t("port_found", port))
    write_and_verify(port, node_id, mode)


def action_config(snap):
    if not snap["nodes"]:
        if snap["driver_trouble"]:
            err(t("driver_trouble"))
            print("        " + t("driver_how"))
            print("        " + t("driver_how2"))
            print("        " + t("driver_note"))
        else:
            err(t("no_node"))
            print("        " + t("no_node_hint"))
        return

    if len(snap["nodes"]) == 1:
        target = snap["nodes"][0]
    else:
        for i, n in enumerate(snap["nodes"]):
            print("    [%d] %s  %s" % (i + 1, n["device"], n["desc"]))
        opts = {str(i + 1): None for i in range(len(snap["nodes"]))}
        sel = ask_choice(t("select"), opts, default="1")
        target = snap["nodes"][int(sel) - 1]
    port = target["device"]
    print()
    info(t("cfg_now"))
    show_node_config(port, cfg=target["fw"])

    mode = ask_mode()
    node_id = ask_id(mode)
    write_and_verify(port, node_id, mode)


def action_show(snap):
    if not snap["nodes"]:
        err(t("no_node"))
        return
    for n in snap["nodes"]:
        print()
        info(n["device"])
        show_node_config(n["device"])


def action_diag(snap):
    print("  " + t("diag_title"))
    print("    " + t("diag_ports", ", ".join(snap["all_ports"]) or "-"))
    print("    " + t("diag_nodes", len(snap["nodes"])))
    print("    " + t("diag_usb", snap["usb_nodes"]))
    print("    " + t("diag_dfu", snap["dfu"] if snap["dfu"] is not None else t("dfu_none")))
    if snap["driver_trouble"]:
        print()
        warn(t("driver_trouble"))
        print("      " + t("driver_how"))
        print("      " + t("driver_how2"))
        print("      " + t("driver_note"))
    elif snap["nodes"]:
        ok(t("diag_driver_ok"))


def action_recover(snap):
    print("  " + t("recover_title"))
    if snap["dfu"] is None:
        info(t("recover_none"))
        return
    if snap["dfu"] in (2, 10):
        ok(t("recover_ok", snap["dfu"]))
        return
    warn(t("dfu_stuck_msg", snap["dfu"], dfu_state_name(snap["dfu"])))
    print()
    print("    " + t("recover_do"))
    print("       " + t("recover_do2"))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    message = ""
    while True:
        snap = scan()
        render(snap, message)
        message = ""
        try:
            choice = input("  " + t("select") + ": ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  " + t("bye"))
            return

        try:
            if choice == "0":
                LANG["code"] = "cn" if LANG["code"] == "en" else "en"
                continue
            elif choice == "1":
                action_flash(snap)
            elif choice == "2":
                action_config(snap)
            elif choice == "3":
                action_show(snap)
            elif choice == "4":
                action_diag(snap)
            elif choice == "5":
                action_recover(snap)
            elif choice in ("q", "quit", "exit"):
                print("\n  " + t("bye"))
                return
            else:
                message = t("invalid", choice)
                continue
            pause()
        except (EOFError, KeyboardInterrupt):
            print("\n  " + t("bye"))
            return
        except Back:
            message = t("back_to_menu")
            continue
        except BrokenPipeError:
            os._exit(0)
        except Exception as e:
            err("%s: %s" % (type(e).__name__, e))
            pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n" + S[LANG["code"]]["bye"])
    except BrokenPipeError:
        os._exit(0)
