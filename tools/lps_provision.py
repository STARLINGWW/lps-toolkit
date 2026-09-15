#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_provision.py —— LPS Node 一键「刷固件 + 配置角色/编号 + 自检」

典型用法（你先把板子置于 DFU 刷机状态，然后一条命令搞定）：

    python tools\\lps_provision.py --anchor 0        # 刷成基站，编号 0
    python tools\\lps_provision.py --anchor 3        # 刷成基站，编号 3
    python tools\\lps_provision.py --tag 20          # 刷成标签(数据出口)，编号 20

其他常用：

    --anchors 0-7         交互式批量刷基站（每次插一块，自动分配编号）
    --mode twr-tag        覆盖角色默认模式（详见 --help 的模式表）
    --skip-flash          不刷固件，只做配置（板子已在运行）
    --enter-dfu --port COM5
                          板子还在运行，让脚本先发 'u' 让它进 DFU
    --test                刷完做功能自检（标签会真的听空口包并统计各基站）
    --test --with-sniffer COM7
                          基站自检：借另一块 sniffer 节点验证本基站是否在发包
    --dry-run             只预检，不写入任何东西
    --yes                 全程免确认（跳过"拔插 USB"的提示）

流程：
    1. 预检：固件能否解析、DFU 设备在不在（不在就告诉你怎么让它进 DFU）
    2. 刷写 .dfu（用的是 Bitcraze 官方 lps-tools 的 DfuSe 实现，与 GUI 同源）
    3. 等节点重新枚举出串口 → 写入 ID 与模式（EEPROM，掉电保存）
    4. 提示拔插 USB 让配置生效 → 回读开机信息校验 地址/模式/自检 [OK]
    5. 可选功能自检：真收空口包统计，或借 sniffer 验证基站发包

角色与默认模式的对应（可被 --mode 覆盖）：
    基站 --anchor  -> tdoa3    (TDoA Anchor V3，本方案的标准锚点)
    标签 --tag     -> sniffer  (纯被动监听的数据出口；LPS Node 没有 TDoA 标签模式)

模式取值：
    tdoa3      TDoA Anchor V3   （基站，推荐）
    tdoa2      TDoA Anchor V2   （基站，8 锚点以内，TDoA3 异常时的兜底）
    sniffer    Sniffer          （标签/数据出口，纯监听）
    twr-anchor TWR Anchor
    twr-tag    TWR Tag          （官方 TWR 标签，一次只能有一个，输出距离不输出位置）
"""

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FW = ROOT / "firmware" / "lps-node-firmware-2022.09.dfu"

NODE_VID, NODE_PID = 0x0483, 0x5740        # 正常运行：CDC 串口
DFU_VID, DFU_PID = 0x0483, 0xDF11          # DFU 引导模式

SNIFFER_SYNC = 0xBC

ROLE_DEFAULT_MODE = {
    "anchor": "tdoa3",
    "tag": "sniffer",
}

MODE_KEYS = {
    "twr-anchor": [b"a"],
    "twr-tag":    [b"t"],
    "sniffer":    [b"s"],
    "tdoa2":      [b"m", b"3"],
    "tdoa3":      [b"m", b"4"],
}

MODE_BANNER = {
    "tdoa3": "TDoA Anchor V3",
    "tdoa2": "TDoA Anchor V2",
    "sniffer": "Sniffer",
    "twr-anchor": "TWR Anchor",
    "twr-tag": "TWR Tag",
}


def hr(title=""):
    if title:
        print("\n" + "=" * 62)
        print("  " + title)
        print("=" * 62)
    else:
        print("=" * 62)


def ok(msg):
    print("  [OK]   " + msg)


def info(msg):
    print("  [--]   " + msg)


def warn(msg):
    print("  [警告] " + msg)


def die(msg, code=1):
    print("  [失败] " + msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 依赖 / 端口辅助
# ---------------------------------------------------------------------------
def load_dfuse():
    """优先用已安装的 lpstools（bootstrap 装好的），否则退回 clones 里的源码"""
    try:
        from lpstools.dfu import dfu
        import dfuse
        return dfu, dfuse
    except ImportError:
        src = ROOT / "clones" / "lps-tools"
        if src.exists():
            sys.path.insert(0, str(src))
        try:
            from lpstools.dfu import dfu
            import dfuse
            return dfu, dfuse
        except ImportError as e:
            die("无法导入 lpstools/dfuse（%s）。请先运行 bootstrap.ps1" % e, 2)


_FAST_WAIT_APPLIED = False


def apply_fast_wait(dfuse_mod, cap_ms=50):
    """修掉"一次刷写要 11 分钟"的问题。

    STM32 的 DFU 引导在 GET_STATUS 里返回 bwPollTimeout = 5000 ms，
    Bitcraze 官方 DfuSe 实现会照睡 5 秒；每块扇区要等 3 次（擦除/设地址/写入），
    90016 字节 = 44 块 → 44 × 3 × 5s ≈ 660 秒（实测 680 秒）。

    这里把单次睡眠上限压到 cap_ms，睡眠后继续轮询状态：只要设备仍是 BUSY
    就继续等待，协议安全，只是轮询更密。想恢复官方行为用 --no-fast-wait。
    """
    global _FAST_WAIT_APPLIED
    if _FAST_WAIT_APPLIED:
        return

    def fast_wait_while_state(self, state):
        states = state if isinstance(state, (list, tuple)) else (state,)
        status = self.get_status()
        time.sleep(min(status[2], cap_ms) / 1000.0)
        while status[1] in states:
            status = self.get_status()
            time.sleep(min(status[2], cap_ms) / 1000.0)
        return status

    dfuse_mod.DfuDevice.wait_while_state = fast_wait_while_state
    _FAST_WAIT_APPLIED = True


def all_ports():
    from serial.tools import list_ports
    return list(list_ports.comports())


def node_ports():
    """处于正常运行模式的 LPS Node"""
    return [p for p in all_ports() if p.vid == NODE_VID and p.pid == NODE_PID]


def dfu_device():
    import usb.core
    return usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID)


def print_ports():
    info("当前串口列表：")
    ports = all_ports()
    if not ports:
        print("         （无）")
    for p in ports:
        tag = "  <- LPS Node" if (p.vid == NODE_VID and p.pid == NODE_PID) else ""
        print("         %-8s %s%s" % (p.device, p.description, tag))


# ---------------------------------------------------------------------------
# 1) 刷写
# ---------------------------------------------------------------------------
def enter_dfu(port):
    """向运行中的节点发 'u'，让它重启进入 DFU 引导"""
    import serial
    info("让 %s 进入 DFU（发送 'u'）" % port)
    try:
        ser = serial.Serial(port, 115200, timeout=0.3)
    except Exception as e:
        die("打不开串口 %s：%s" % (port, e), 2)
    try:
        time.sleep(0.3)
        ser.write(b"u")
        ser.flush()
        time.sleep(0.6)
    finally:
        ser.close()
    time.sleep(0.8)


def wait_dfu(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if dfu_device() is not None:
            return True
        time.sleep(0.4)
    return False


def do_flash(dfu_cls, fw_path):
    size = fw_path.stat().st_size
    info("刷写 %s（%d 字节）" % (fw_path.name, size))
    last = {"pct": -1}
    t0 = time.time()

    def cb(_name, fraction):
        pct = int(round(fraction * 100))
        if pct != last["pct"]:
            last["pct"] = pct
            elapsed = time.time() - t0
            eta = ""
            if fraction > 0.02:
                left = elapsed / fraction - elapsed
                eta = "  已用 %d:%02d  剩余约 %d:%02d" % (
                    int(elapsed) // 60, int(elapsed) % 60,
                    int(left) // 60, int(left) % 60)
            bar = "#" * (pct // 4) + "." * (25 - pct // 4)
            sys.stdout.write("\r         [%s] %3d%%%s" % (bar, pct, eta))
            sys.stdout.flush()

    try:
        dfu_cls().flash(str(fw_path), cb)
    except Exception as e:
        print()
        die("刷写失败：%s: %s\n"
            "         可用官方 GUI 兜底：python -m lpstools（或双击 lpstool.exe）"
            % (type(e).__name__, e), 3)
    print("\r         [%s] 100%%   （用时 %.1f 秒）" % ("#" * 25, time.time() - t0))


def wait_new_node_port(before, timeout=25.0):
    """刷完固件后，等待出现新的 LPS Node 串口（用差集避免认错别的节点）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = {p.device for p in node_ports()}
        new = sorted(now - before)
        if len(new) == 1:
            return new[0], True
        if len(new) > 1:
            warn("同时出现了多个新节点：%s" % ", ".join(new))
            return new[0], False
        time.sleep(0.4)
    return None, False


# ---------------------------------------------------------------------------
# 2) 串口配置
# ---------------------------------------------------------------------------
def open_serial(port, timeout=0.2):
    import serial
    try:
        return serial.Serial(port, 115200, timeout=timeout)
    except Exception as e:
        die("打不开串口 %s：%s（官方 GUI 可能在占用它）" % (port, e), 2)


def wait_port(port, timeout=15.0):
    """等串口重新出现（拔插 USB 后 Windows 需要一点时间重新枚举）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(p.device.upper() == port.upper() for p in all_ports()):
            return True
        time.sleep(0.4)
    return False


def read_some(ser, seconds):
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
    return buf.decode("utf-8", "replace")


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
    cfg["test_ok"] = text.count("[OK]")
    cfg["test_fail"] = text.count("[FAIL]") + text.count("[ERROR]")
    return cfg


def write_config(port, node_id, mode, radio=None):
    ser = open_serial(port)
    try:
        read_some(ser, 1.2)                       # 吃掉开机横幅

        if node_id < 10:
            keys = [str(node_id).encode()]
        else:
            keys = [b"i", str(node_id).encode(), b"\n"]
        keys += MODE_KEYS[mode]
        if radio is not None:
            keys += [b"r", str(radio).encode()]

        for k in keys:
            ser.write(k)
            ser.flush()
            time.sleep(0.12)
        time.sleep(0.4)
        tail = read_some(ser, 1.0)
    finally:
        ser.close()
    return tail


def verify_config(port, node_id, mode):
    """回读开机信息，校验地址/模式/自检结果"""
    if not wait_port(port, 15.0):
        print_ports()
        die("串口 %s 没有出现（可能换了 COM 号，或没插好）。可运行 "
            "python tools\\lps_config.py --list 查看" % port, 4)
    ser = open_serial(port)
    try:
        text = read_some(ser, 1.8)
    finally:
        ser.close()
    cfg = parse_banner(text)

    passed = True
    if cfg.get("address") != node_id:
        warn("回读地址 = %s，期望 %d（若刚改过，请再拔插一次 USB）"
             % (cfg.get("address"), node_id))
        passed = False
    else:
        ok("地址(ID) = %d" % node_id)

    expect = MODE_BANNER[mode]
    if cfg.get("mode") != expect:
        warn("回读模式 = %s，期望 %s" % (cfg.get("mode"), expect))
        passed = False
    else:
        ok("模式 = %s" % expect)

    if cfg.get("test_fail"):
        warn("开机自检有 %d 项失败，请检查硬件（USB 线/模块）" % cfg["test_fail"])
        passed = False
    else:
        ok("开机自检全部通过（%d 个 [OK]）" % cfg.get("test_ok", 0))

    info("比特率 %s / 前导码 %s" % (cfg.get("bitrate", "?"), cfg.get("preamble", "?")))
    return passed, cfg, text


# ---------------------------------------------------------------------------
# 3) 功能自检
# ---------------------------------------------------------------------------
class FrameStats:
    """解析 sniffer 二进制帧，统计每个来源锚点的收包数"""

    def __init__(self):
        self.buf = bytearray()
        self.by_src = {}
        self.frames = 0
        self.tdoa3 = 0
        self.last_seen = {}

    def feed(self, chunk, now):
        self.buf += chunk
        while True:
            try:
                idx = self.buf.index(SNIFFER_SYNC)
            except ValueError:
                self.buf.clear()
                return
            if idx:
                del self.buf[:idx]
            if len(self.buf) < 12:
                return
            length = int.from_bytes(bytes(self.buf[8:10]), "little")
            if length > 1024:
                del self.buf[:1]
                continue
            if len(self.buf) < 12 + length:
                return
            length2 = int.from_bytes(bytes(self.buf[10 + length:12 + length]), "little")
            src = self.buf[6]
            payload = bytes(self.buf[10:10 + length])
            del self.buf[:12 + length]
            if length != length2:
                continue
            self.frames += 1
            self.by_src[src] = self.by_src.get(src, 0) + 1
            self.last_seen[src] = now
            if payload and payload[0] == 0x30:
                self.tdoa3 += 1


def radio_test(port, seconds, expect_ids=None):
    """让 sniffer 节点听 seconds 秒空口包，返回统计结果"""
    ser = open_serial(port, timeout=0.05)
    try:
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b"b")                # 切二进制输出（RAM 标志，拔插即恢复）
        ser.flush()
        time.sleep(0.3)

        stats = FrameStats()
        t0 = time.time()
        print("         正在监听空口 ...（%.0f 秒）" % seconds)
        while time.time() - t0 < seconds:
            chunk = ser.read(8192)
            if chunk:
                stats.feed(chunk, time.time())
            else:
                time.sleep(0.005)
    finally:
        ser.close()

    elapsed = time.time() - t0
    print("         收到 %d 帧（%.0f 帧/秒），其中 TDoA3 包 %d"
          % (stats.frames, stats.frames / max(elapsed, 1e-6), stats.tdoa3))
    if stats.by_src:
        print("         各来源节点：")
        for src in sorted(stats.by_src):
            age = time.time() - stats.last_seen.get(src, 0)
            mark = ""
            if expect_ids is not None:
                mark = "  <- 目标基站" if src in expect_ids else ""
            print("           ID %-4d %6d 包  %6.1f Hz  最近 %.2f 秒前%s"
                  % (src, stats.by_src[src], stats.by_src[src] / max(elapsed, 1e-6),
                     age, mark))
    return stats, elapsed


def functional_test(role, mode, port, seconds, with_sniffer):
    """功能自检：能不能真的收到/发出 UWB 信号"""
    hr("功能自检")

    if mode in ("sniffer",):
        if role != "tag":
            info("该节点是 sniffer 模式，直接用它自己监听空口")
        stats, elapsed = radio_test(port, seconds)
        if stats.frames == 0:
            warn("一帧都没收到：基站可能没上电/太远，或射频参数不一致")
            return False
        ok("能正常接收 UWB 信号（%d 个来源节点可见）" % len(stats.by_src))
        if len(stats.by_src) < 4:
            warn("可见节点少于 4 个，不足以三维定位（需要 >= 4 个基站）")
            return False
        ok("可见节点数满足三维定位要求（>= 4）")
        if stats.tdoa3 == 0:
            warn("收到包但没有 TDoA3 包(0x30)：基站模式可能不是 TDoA Anchor V3")
            return False
        ok("收到 TDoA3 数据包，协议匹配")
        return True

    # 基站：自己不发数据上报，需要用另一块 sniffer 节点旁听
    info("基站模式不会往串口输出测量数据，功能验证需要一块 sniffer 节点旁听")
    if with_sniffer:
        sniffer_port = with_sniffer
        if sniffer_port in [p.device for p in node_ports()] or True:
            stats, _ = radio_test(sniffer_port, seconds)
            if stats.by_src:
                ids = ", ".join(str(i) for i in sorted(stats.by_src))
                ok("sniffer 收到来源节点：%s" % ids)
                if port == sniffer_port:
                    warn("你把 sniffer 口和基站口设成同一个了")
                return True
            warn("sniffer 没收到任何包，请检查基站是否上电/发包")
            return False
    else:
        info("想验证基站发包，请加参数：--test --with-sniffer COMx")
        info("或先用另一块配成 sniffer 的节点，运行：python tools\\lps_capture.py --port COMx --seconds 10 --summary")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_range(s):
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s.strip())
    if not m:
        die("范围格式应为 0-7")
    a, b = int(m.group(1)), int(m.group(2))
    if b < a or b - a > 60:
        die("范围不合法")
    return list(range(a, b + 1))


def provision_one(args, role, node_id, dfu_cls, dfuse_mod):
    mode = args.mode or ROLE_DEFAULT_MODE[role]
    if mode not in MODE_KEYS:
        die("未知模式 %s，可选：%s" % (mode, ", ".join(MODE_KEYS)))

    hr("目标：%s  编号 %d  模式 %s"
       % ("基站(anchor)" if role == "anchor" else "标签(tag)", node_id, mode))

    before = {p.device for p in node_ports()}
    port = args.port

    # --- 刷写 ---
    if not args.skip_flash:
        if dfu_device() is None:
            if args.enter_dfu:
                if not port:
                    ports = node_ports()
                    if len(ports) != 1:
                        print_ports()
                        die("需要 --port 指定要进入 DFU 的节点串口（当前在线 %d 个）"
                            % len(ports), 2)
                    port = ports[0].device
                enter_dfu(port)
            if not wait_dfu(args.dfu_timeout):
                hr()
                warn("没有检测到 DFU 设备（0483:df11）")
                print("")
                print("   请先让板子进入刷机状态，任选一种：")
                print("     A) 官方 GUI：python -m lpstools  → 选串口 → Update firmware")
                print("     B) 本脚本自动：python tools\\lps_provision.py --%s %d --enter-dfu --port COM5"
                      % (role, node_id))
                print("     C) 手动：往节点串口发一个字符 'u'，节点会重启进 DFU")
                print("")
                die("未进入 DFU，脚本结束", 3)
        ok("检测到 DFU 设备（0483:df11）")

        if not args.firmware.exists():
            die("固件不存在：%s" % args.firmware, 2)
        try:
            dfuf = dfuse_mod.DfuFile(str(args.firmware))
            seg = dfuf.targets[0]["elements"][0]
            info("固件：%s（目标地址 0x%08X，%d 字节）"
                 % (args.firmware.name, seg["address"], len(seg["data"])))
        except Exception as e:
            die("固件解析失败：%s" % e, 2)

        # 上次刷写被中断时，STM32 引导会停在 DNLOAD-IDLE(5)，
        # 这时直接刷会在 0% 报 "设备没有发挥作用"，先做一次恢复
        try:
            import lps_flash
            ready, msg = lps_flash.ensure_dfu_idle(
                dfuse_mod, recover=args.recover)
            if not ready:
                die("DFU 设备未就绪：%s" % msg, 5)
            info("DFU 状态检查：%s" % msg)
        except ImportError:
            pass

        do_flash(dfu_cls, args.firmware)

        if port is None:
            port, fresh = wait_new_node_port(before, args.wait)
            if port is None:
                print_ports()
                die("刷完后没等到新的节点串口。请拔插 USB 后加 --skip-flash --port COMx 重跑配置", 4)
            if fresh:
                ok("节点已重启，串口 = %s" % port)
            else:
                warn("使用串口 %s（无法确定是否为新刷的板子，建议只插这一块）" % port)
    else:
        info("跳过刷写（--skip-flash）")
        if port is None:
            ports = node_ports()
            if len(ports) != 1:
                print_ports()
                die("请用 --port 指定节点串口（当前在线 %d 个）" % len(ports), 2)
            port = ports[0].device

    # --- 配置 ---
    hr("写入配置（EEPROM，掉电保存）")
    write_config(port, node_id, mode, radio=args.radio)
    ok("已发送：编号 = %d，模式 = %s%s"
       % (node_id, mode, "，射频档 = %d" % args.radio if args.radio is not None else ""))

    # --- 让配置生效 ---
    if not args.yes:
        print("")
        print("  >> 让配置生效：按一下节点上的 Reset 键（右边靠下那个），或拔插一次 USB")
        print("     配置写在 EEPROM 里，必须重启才生效。")
        try:
            input("     复位完成后按回车继续校验（Ctrl+C 放弃校验）...")
        except KeyboardInterrupt:
            print()
            warn("已跳过校验。稍后可用：python tools\\lps_config.py --port %s --read" % port)
            return 0
    else:
        info("--yes：等 3 秒后直接校验（若显示旧值，按一下 Reset 键或拔插 USB 再查）")
        time.sleep(3.0)

    hr("校验配置")
    passed, cfg, _text = verify_config(port, node_id, mode)

    # --- 功能自检 ---
    if args.test:
        ft = functional_test(role, mode, port, args.test_seconds, args.with_sniffer)
    else:
        ft = True
        print("")
        info("未做功能自检。加 --test 可自动验证射频收发；")
        info("标签节点自检：python tools\\lps_provision.py --tag %d --skip-flash --port %s --test"
             % (node_id, port))

    hr("结果")
    if passed and ft:
        ok("刷写 + 配置 + 校验 全部通过：%s 编号 %d，模式 %s"
           % ("基站" if role == "anchor" else "标签", node_id, MODE_BANNER[mode]))
        print("")
        print("  接着做：")
        if role == "anchor":
            print("    - 按测量好的坐标填写 tools\\anchors_example.yaml（复制一份改成自己的）")
        else:
            print("    - 实时解算：python tools\\lps_tdoa3_solver.py --port %s --anchors tools\\anchors_example.yaml --check" % port)
            print("    - 抓包留档：python tools\\lps_capture.py --port %s --seconds 30 --summary" % port)
        return 0
    else:
        warn("有检查项未通过，请按上面提示处理")
        return 1


def main():
    ap = argparse.ArgumentParser(
        description="LPS Node 一键刷写 + 配置 + 自检",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--anchor", type=int, metavar="ID", help="刷成基站（TDoA Anchor V3），ID 0-255")
    g.add_argument("--tag", type=int, metavar="ID", help="刷成标签/数据出口（Sniffer），ID 0-255")
    g.add_argument("--anchors", metavar="A-B", help="交互式批量刷基站，例如 0-7")

    ap.add_argument("--mode", choices=sorted(MODE_KEYS), help="覆盖角色默认模式")
    ap.add_argument("--port", help="配置阶段用的串口（不填则自动识别新出现的节点）")
    ap.add_argument("--firmware", type=Path, default=DEFAULT_FW, help=".dfu 固件路径")
    ap.add_argument("--skip-flash", action="store_true", help="不刷固件，只配置")
    ap.add_argument("--enter-dfu", action="store_true", help="板子还在运行时，先发 'u' 让它进 DFU")
    ap.add_argument("--radio", type=int, help="射频模式档位（所有节点必须一致）")
    ap.add_argument("--test", action="store_true", help="刷完做功能自检")
    ap.add_argument("--test-seconds", type=float, default=10.0, help="功能自检监听时长（默认 10 秒）")
    ap.add_argument("--with-sniffer", metavar="COMx", help="基站自检时借用的 sniffer 节点串口")
    ap.add_argument("--dfu-timeout", type=float, default=20.0, help="等待 DFU 设备出现的秒数")
    ap.add_argument("--wait", type=float, default=25.0, help="刷完后等待节点串口出现的秒数")
    ap.add_argument("--yes", action="store_true", help="免确认（不提示拔插 USB）")
    ap.add_argument("--dry-run", action="store_true", help="只预检，不写入")
    ap.add_argument("--no-fast-wait", action="store_true",
                    help="不压缩 DFU 轮询等待（官方原速，一次刷写约 11 分钟）")
    ap.add_argument("--wait-cap", type=int, default=0, metavar="MS",
                    help="DFU 单次等待上限（毫秒）。默认 0 = 官方原速（最稳，约 11 分钟）；"
                         "想加速可设 500/1000（有中途失败风险）")
    ap.add_argument("--recover", action="store_true",
                    help="DFU 卡在残留状态时先尝试软件恢复（多数情况仍需手动拔插重进引导）")
    args = ap.parse_args()

    hr("LPS Node 刷写/配置工具")
    info("工程根目录：%s" % ROOT)

    # --- 预检 ---
    dfu_cls, dfuse_mod = load_dfuse()
    if args.wait_cap > 0:
        apply_fast_wait(dfuse_mod, cap_ms=args.wait_cap)
        info("等待策略：加速模式（单次等待上限 %d ms，若中途失败请去掉该参数用原速）"
             % args.wait_cap)
    else:
        info("等待策略：官方原速（严格按 STM32 上报的 bwPollTimeout 等待）")
        info("一次刷写约 11 分钟，期间不要拔线或关闭窗口；想加速可加 --wait-cap 500")
    print_ports()
    has_dfu = dfu_device() is not None
    info("DFU 设备：%s" % ("已就绪 (0483:df11)" if has_dfu else "未发现"))

    if args.dry_run:
        hr("预检结果")
        fw = args.firmware
        if not fw.exists():
            warn("固件不存在：%s" % fw)
        else:
            ok("固件存在：%s（%d 字节）" % (fw, fw.stat().st_size))
            try:
                dfuf = dfuse_mod.DfuFile(str(fw))
                for t in dfuf.targets:
                    for e in t["elements"]:
                        ok("  目标 alternate=%s 地址=0x%08X 大小=%d 字节"
                           % (t["alternate"], e["address"], len(e["data"])))
            except Exception as e:
                warn("固件解析失败：%s" % e)
        if has_dfu:
            try:
                d = dfuse_mod.DfuDevice(dfu_device())
                alts = list(d.alternates())
                for name, intf in alts:
                    if intf.bAlternateSetting == 0:
                        d.set_alternate(intf)
                        break
                st = d.get_status()
                ok("DFU 设备状态：bStatus=%d bState=%d（2=DFU_IDLE 正常）" % (st[0], st[1]))
            except Exception as e:
                warn("打开 DFU 设备失败：%s（Windows 上可能需要 Zadig 装 WinUSB/libusb 驱动）" % e)
        else:
            info("未发现 DFU 设备：先把板子置于刷机状态再运行（或加 --enter-dfu --port COMx）")
        info("预检结束，未写入任何数据。")
        return

    if not (0 <= (args.anchor if args.anchor is not None else
                  (args.tag if args.tag is not None else 0)) <= 255):
        die("ID 必须在 0-255")

    # --- 批量模式 ---
    if args.anchors:
        ids = parse_range(args.anchors)
        mode = args.mode or ROLE_DEFAULT_MODE["anchor"]
        print("")
        print("  交互式批量：每次只插一块板子。Ctrl+C 退出。")
        rc = 0
        for n, nid in enumerate(ids):
            try:
                input("\n  请把要配置为【基站 %d】的板子置于 DFU 状态并插好，然后按回车 ..." % nid)
            except KeyboardInterrupt:
                print("\n  已退出")
                return rc
            sub = argparse.Namespace(**vars(args))
            sub.anchors = None
            sub.anchor = nid
            sub.tag = None
            sub.port = args.port if n == 0 else None
            try:
                rc |= provision_one(sub, "anchor", nid, dfu_cls, dfuse_mod)
            except SystemExit as e:
                print("  [失败] 该节点未完成（退出码 %s），继续下一块" % e.code)
                rc |= int(e.code or 1)
        hr("批量完成")
        ok("共处理 %d 块板子（编号 %s）" % (len(ids), args.anchors))
        return

    role = "anchor" if args.anchor is not None else "tag"
    node_id = args.anchor if args.anchor is not None else args.tag
    sys.exit(provision_one(args, role, node_id, dfu_cls, dfuse_mod))


if __name__ == "__main__":
    main()
