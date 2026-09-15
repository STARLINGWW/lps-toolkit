#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_config.py —— LPS Node 配置工具（串口控制台封装）

节点固件的配置全部通过 USB 串口的一个字符菜单完成，本脚本把它自动化了。

菜单命令对照（源码 src/main.c 核实）：
    0-9      直接设置地址（ID）；  i + 数字 + 回车  设置 ≥10 的 ID
    a        TWR Anchor          t  TWR Tag        s  Sniffer
    m + 数字 模式菜单：0=TWR Anchor 1=TWR Tag 2=Sniffer 3=TDoA Anchor V2 4=TDoA Anchor V3
    r + 数字 射频模式（比特率/前导码）
    p + 数字 发射功率
    d        恢复默认配置
    b        串口切二进制输出（掉电失效，不会写进 EEPROM）
    u        进入 DFU 引导模式
    h        帮助

用法：
    python tools/lps_config.py --list
    python tools/lps_config.py --port COM5 --read
    python tools/lps_config.py --port COM5 --id 3 --mode tdoa3
    python tools/lps_config.py --port COM5 --mode sniffer
    python tools/lps_config.py --assign 0-7 --mode tdoa3     # 交互式批量编号

⚠ 配置改动需要**断电重启**才生效（脚本会提醒）。
"""

import argparse
import re
import sys
import time

VID, PID = 0x0483, 0x5740

MODE_KEYS = {
    "twr-anchor": [b"a"],
    "twr-tag":    [b"t"],
    "sniffer":    [b"s"],
    "tdoa2":      [b"m", b"3"],
    "tdoa3":      [b"m", b"4"],
}


def list_nodes():
    from serial.tools import list_ports
    return [p for p in list_ports.comports() if p.vid == VID and p.pid == PID]


def usb_driver_diagnostic():
    """pyserial 看不到串口、但 USB 上确实有 0483:5740 设备时的自动诊断。

    这是本工程最常见的坑：Crazyflie 与 LPS Node 的 VID:PID 完全相同
    (0483:5740)，如果之前用 Zadig 给 Crazyflie 装过 libusb-win32 驱动，
    它会按硬件 ID 把 LPS Node 的 CDC 接口也抢走，Windows 就不创建 COM 口了。
    """
    if list_nodes():
        return
    try:
        import usb.core
        devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or [])
    except Exception as e:
        print("  （pyusb 无法枚举 USB：%s）" % e)
        return
    if not devs:
        return

    print("")
    print("!! 检测到 %d 个 0483:5740 USB 设备，但系统没有生成任何串口。" % len(devs))
    for d in devs:
        try:
            name = d.product
        except Exception:
            name = "?"
        print("   - %04x:%04x  %s" % (d.idVendor, d.idProduct, name))
    print("")
    print("   原因：该 CDC 接口被 libusb-win32 / Zadig 驱动占用了（Crazyflie 与 LPS Node")
    print("         的 VID:PID 相同，装给 Crazyflie 的驱动会一起抢走 LPS Node）。")
    print("   修复：设备管理器 → libusb-win32 devices → 'Crazyflie 2.x (Interface 0)'")
    print("         → 更新驱动程序 → 浏览我的电脑 → 让我从计算机上的列表选取")
    print("         → 选择 'USB 串行设备' → 完成 → 拔插 USB")
    print("   或（管理员 PowerShell）：")
    print("         pnputil /delete-driver oem35.inf /uninstall /force   # 之后拔插 USB")


def pick_port(explicit):
    if explicit:
        return explicit
    nodes = list_nodes()
    if len(nodes) == 1:
        return nodes[0].device
    if not nodes:
        print("!! 没找到 LPS Node。检查 USB 数据线 / 供电 / 是否处于 DFU 模式。")
        sys.exit(2)
    print("!! 多个节点在线，请用 --port 指定：")
    for p in nodes:
        print("   %-8s %s" % (p.device, p.description))
    sys.exit(2)


def open_port(port, baud=115200):
    import serial
    try:
        return serial.Serial(port, baud, timeout=0.2)
    except Exception as e:
        print("!! 打不开串口 %s：%s" % (port, e))
        sys.exit(2)


def read_banner(ser, seconds=1.5):
    """节点在串口被连接时会重新打印开机信息，从中解析当前配置"""
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
    return buf.decode("utf-8", "replace")


def read_banner_until(ser, timeout=3.5):
    """可靠地读完整个开机横幅。

    节点是在 USB 串口"被连接"之后、由主循环里的 usbcommPrintWelcomeMessage()
    才把横幅打出来的，实测要 1~2 秒才到；固定读 1 秒会漏掉前半段。
    这里改成读到结束标志（"Press 'h' for help" / "Node started"）或超时。
    """
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            if b"Press 'h' for help" in buf or b"Node started" in buf:
                time.sleep(0.1)
                tail = ser.read(4096)
                if tail:
                    buf += tail
                break
        else:
            time.sleep(0.02)
    return buf.decode("utf-8", "replace")


def read_config_text(port, timeout=3.5):
    """打开串口读一次完整开机横幅，返回文本（失败返回 None）。

    节点只在"检测到新的串口连接"时才重新打印横幅；如果刚探测过就立刻重开串口，
    Windows 可能不产生断开事件，于是读不到内容 —— 所以这里重试几次。
    """
    text = ""
    for attempt in range(3):
        try:
            ser = open_port(port)
        except SystemExit:
            return None
        try:
            text = read_banner_until(ser, timeout if attempt == 0 else 2.0)
        finally:
            ser.close()
        if "Address is" in text or "Node started" in text:
            return text
        time.sleep(0.6)
    return text


def parse_banner(text):
    cfg = {}
    m = re.search(r"Address is 0x([0-9A-Fa-f]+)", text)
    if m:
        cfg["address"] = int(m.group(1), 16)
    m = re.search(r"Mode is (.+)", text)
    if m:
        cfg["mode"] = m.group(1).strip()
    m = re.search(r"Bitrate: (\w+)", text)
    if m:
        cfg["bitrate"] = m.group(1)
    m = re.search(r"Preamble: (\w+)", text)
    if m:
        cfg["preamble"] = m.group(1)
    m = re.search(r"Anchor position enabled: (\w+)", text)
    if m:
        cfg["position_enabled"] = m.group(1)
    m = re.search(r"Anchor position: ([-\d\.eE ]+)", text)
    if m:
        cfg["position"] = m.group(1).strip()
    return cfg


def show_config(port, quiet=False):
    ser = open_port(port)
    try:
        text = read_banner_until(ser)
    finally:
        ser.close()
    cfg = parse_banner(text)
    if not quiet:
        print("=== %s 当前配置 ===" % port)
        if cfg:
            print("  地址(ID) : %s" % cfg.get("address", "?"))
            print("  模式     : %s" % cfg.get("mode", "?"))
            print("  比特率   : %s" % cfg.get("bitrate", "?"))
            print("  前导码   : %s" % cfg.get("preamble", "?"))
            if "position_enabled" in cfg:
                print("  坐标     : %s %s" % (cfg["position_enabled"], cfg.get("position", "")))
        else:
            print("  (没读到配置输出，原始内容如下)")
            print(text.strip()[:500])
    return cfg


def probe_firmware(port, quiet=False):
    """探测节点固件能力：让节点打印它支持的模式列表。

    为什么不用版本号？—— LPS Node 固件**不输出版本号**（既没有 version 命令，
    开机横幅也不带），所以只能靠"能力探测"：
        菜单 'm' 会列出固件里注册的全部模式，
        出现 "TDoA Anchor V3" 就说明固件 ≥ 2018.10（TDoA3 从这版开始进入官方固件）。

    注意：'m' 会让节点停在模式选择菜单，所以必须再发一个无效字符让它退回主菜单。
    """
    ser = open_port(port)
    try:
        reset_console(ser)                    # 先确保不在子菜单里
        banner = read_banner_until(ser, 3.0)  # 开机横幅里带当前 编号/模式
        ser.write(b"m")
        ser.flush()
        time.sleep(0.4)
        text = read_banner(ser, 0.8)
        ser.write(b"x")                       # 无效选择 → 节点打印 Incorrect mode 并回主菜单
        ser.flush()
        time.sleep(0.3)
        text += read_banner(ser, 0.5)
        reset_console(ser)                    # 收尾：确保一定回到主菜单
    finally:
        ser.close()

    seen, has_tdoa3, has_tdoa2 = parse_mode_list(text)
    cfg = parse_banner(banner)

    if not quiet:
        print("=== %s 固件能力探测 ===" % port)
        if not seen:
            print("  (没读到模式列表，原始输出如下)")
            print(text.strip()[:400])
        else:
            print("  支持 %d 种模式：" % len(seen))
            for i, name in enumerate(seen):
                print("    %d - %s" % (i, name))
            print()
            if has_tdoa3:
                print("  → [OK] 支持 TDoA3，固件可直接使用（≥ 2018.10），**无需刷固件**")
                print("         直接配置即可：python tools\\lps_config.py --port %s --id N --mode tdoa3" % port)
            elif has_tdoa2:
                print("  → [旧] 只支持 TDoA2（2018.10 之前的固件）")
                print("         要么刷 2022.09 固件用 TDoA3，要么整套系统改用 TDoA2")
            else:
                print("  → [过旧] 连 TDoA2 都没有（很早期的固件），必须刷固件")
    cfg.update({"modes": seen, "has_tdoa3": has_tdoa3, "has_tdoa2": has_tdoa2,
                "raw": banner + "\n" + text})
    return cfg


def parse_mode_list(text):
    """从 'm' 菜单的输出里解析出支持的模式列表。

    节点源码（src/main.c printModeList）打印格式为：
        Available UWB modes:
         0 - TWR Anchor
         4 - TDoA Anchor V3 (Current mode)
    """
    modes = []
    for m in re.finditer(r"^\s*(\d+)\s*-\s*(.+?)\s*(\(\s*Current mode\s*\))?\s*$", text, re.M):
        name = m.group(2).strip()
        if name and name not in modes:
            modes.append(name)
    has_tdoa3 = any("TDoA Anchor V3" in x for x in modes)
    has_tdoa2 = any("TDoA Anchor V2" in x for x in modes)
    return modes, has_tdoa3, has_tdoa2


def send_keys(ser, keys, gap=0.10):
    for k in keys:
        ser.write(k)
        ser.flush()
        time.sleep(gap)


def reset_console(ser, gap=0.12):
    """把节点控制台从任何子菜单状态拉回主菜单（幂等，不改变配置）。

    '\\r' 结束数字输入类菜单；'x' 在子菜单里是非法选项（节点会打印 Incorrect ... 并回主菜单），
    在主菜单里则是无害的未知字符。做这套动作可保证后续按键真被主菜单收到。
    """
    for k in (b"\r", b"x", b"\r"):
        try:
            ser.write(k)
            ser.flush()
        except Exception:
            return
        time.sleep(gap)


def write_config(port, node_id, mode, mode_keys=None, radio=None):
    """按串口菜单的按键序列写入 ID 与模式（写进 EEPROM，重启后生效）。

    mode_keys 允许外部传入模式按键表（默认用本模块的 MODE_KEYS）。
    返回 True/False。
    """
    keys_map = mode_keys or MODE_KEYS
    if mode not in keys_map:
        print("!! 未知模式 %s" % mode)
        return False
    try:
        ser = open_port(port)
    except SystemExit:
        return False
    try:
        read_banner_until(ser, 2.0)          # 先吃掉开机横幅
        reset_console(ser)                   # 再确保处于主菜单
        keys = ([str(node_id).encode()] if node_id < 10
                else [b"i", str(node_id).encode(), b"\n"])
        keys += list(keys_map[mode])
        if radio is not None:
            keys += [b"r", str(radio).encode()]
        send_keys(ser, keys)
        time.sleep(0.4)
    finally:
        ser.close()
    return True


def wait_port(port, timeout=20.0):
    """等待某个串口重新出现（复位/拔插后）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(p.device.upper() == port.upper() for p in list_nodes()):
            return True
        if any(p.device.upper() == port.upper() for p in list_ports_all()):
            return True
        time.sleep(0.5)
    return False


def list_ports_all():
    from serial.tools import list_ports
    return list(list_ports.comports())


def wait_new_port(before, timeout=25.0):
    """等待出现一个新的 LPS Node 串口（刷完固件后用它认出新板子）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = {p.device for p in list_nodes()}
        new = sorted(now - set(before))
        if len(new) == 1:
            return new[0]
        if len(new) > 1:
            return new[0]
        time.sleep(0.5)
    return None


def configure(port, node_id=None, mode=None, radio=None, power=None, reset=False,
              quiet=False):
    ser = open_port(port)
    changed = False
    try:
        read_banner(ser, 1.0)          # 先清掉开机横幅

        if reset:
            send_keys(ser, [b"d"])
            changed = True
            if not quiet:
                print("--> 已发送：恢复默认配置")

        if node_id is not None:
            if node_id < 0 or node_id > 255:
                print("!! ID 必须在 0-255")
                sys.exit(2)
            if node_id < 10:
                send_keys(ser, [str(node_id).encode()])
            else:
                send_keys(ser, [b"i", str(node_id).encode(), b"\n"], gap=0.15)
            changed = True
            if not quiet:
                print("--> 已发送：设置 ID = %d" % node_id)

        if mode is not None:
            if mode not in MODE_KEYS:
                print("!! 未知模式 %s，可选：%s" % (mode, ", ".join(MODE_KEYS)))
                sys.exit(2)
            send_keys(ser, MODE_KEYS[mode])
            changed = True
            if not quiet:
                print("--> 已发送：模式 = %s" % mode)

        if radio is not None:
            send_keys(ser, [b"r", str(radio).encode()])
            changed = True
            if not quiet:
                print("--> 已发送：射频模式 = %s" % radio)

        if power is not None:
            send_keys(ser, [b"p", str(power).encode()])
            changed = True
            if not quiet:
                print("--> 已发送：功率档 = %s" % power)

        time.sleep(0.4)
        tail = read_banner(ser, 1.0)
    finally:
        ser.close()

    if changed:
        if "restart" in tail:
            print("!! EEPROM 配置已改变 —— 必须重启才生效：")
            print("   按一下节点上的 Reset 键（右边靠下那个），或拔插一次 USB")
        else:
            print("!! 配置命令已发送；请按 Reset 键（或拔插 USB）重启后再用 --read 验证")
    return tail


def parse_range(s):
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s.strip())
    if not m:
        print("!! 范围格式应为 0-7")
        sys.exit(2)
    a, b = int(m.group(1)), int(m.group(2))
    if b < a or b - a > 60:
        print("!! 范围不合法")
        sys.exit(2)
    return list(range(a, b + 1))


def wait_for_node(timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        nodes = list_nodes()
        if len(nodes) == 1:
            return nodes[0].device
        if len(nodes) > 1:
            print("!! 检测到多个节点，请只保留一个")
            sys.exit(2)
        time.sleep(0.5)
    print("!! 等待节点超时（只插一个节点，确认已上电）")
    sys.exit(2)


def assign_range(ids, mode):
    print("交互式批量编号：每次只插一个节点，脚本会自动分配下一个 ID")
    for nid in ids:
        input("请插入要配置为 ID %d 的节点，然后按回车 ..." % nid)
        port = wait_for_node()
        print("  使用 %s" % port)
        configure(port, node_id=nid, mode=mode)
        print("  验证（重启前的地址可能还是旧的，以模式为准）：")
        show_config(port)
        input("  请断电重连该节点使其生效，然后按回车继续 ...")
    print("全部完成。建议逐个运行 --read 复核地址。")


def main():
    ap = argparse.ArgumentParser(description="LPS Node 配置（ID / 模式 / 射频 / 功率）")
    ap.add_argument("--list", action="store_true", help="列出在线节点")
    ap.add_argument("--port", help="节点串口，例如 COM5；不指定则自动识别")
    ap.add_argument("--read", action="store_true", help="只读取并显示当前配置")
    ap.add_argument("--probe", action="store_true",
                    help="探测固件能力：列出节点支持的模式（判断是否需要刷固件）")
    ap.add_argument("--id", type=int, help="设置节点 ID（0-255）")
    ap.add_argument("--mode", choices=sorted(MODE_KEYS), help="设置工作模式")
    ap.add_argument("--radio", type=int, help="设置射频模式档位（菜单 r 的数字）")
    ap.add_argument("--power", type=int, help="设置发射功率档位（菜单 p 的数字）")
    ap.add_argument("--reset", action="store_true", help="恢复默认配置")
    ap.add_argument("--assign", help="交互式批量分配 ID，例如 0-7")
    args = ap.parse_args()

    if args.list:
        nodes = list_nodes()
        print("=== 在线 LPS Node ===")
        if not nodes:
            print("  （未发现）")
        for p in nodes:
            print("  %-8s %s" % (p.device, p.description))
        usb_driver_diagnostic()
        return

    if args.probe:
        probe_firmware(pick_port(args.port))
        return

    if args.assign:
        assign_range(parse_range(args.assign), args.mode)
        return

    port = pick_port(args.port)

    if args.read or (args.id is None and args.mode is None and args.radio is None
                     and args.power is None and not args.reset):
        show_config(port)
        return

    print("=== %s 配置前 ===" % port)
    show_config(port)
    configure(port, node_id=args.id, mode=args.mode, radio=args.radio,
              power=args.power, reset=args.reset)
    print("\n重启后再执行一次以下命令确认：")
    print("  python tools/lps_config.py --port %s --read" % port)


if __name__ == "__main__":
    main()
