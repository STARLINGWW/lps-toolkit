#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_flash.py —— LPS Node 固件刷写工具

流程：
    1. 通过串口向运行中的节点发送 'u'，让它重启进入 STM32 DFU 引导模式
    2. 等待 USB 上出现 DFU 设备（0483:df11）
    3. 用 Bitcraze 官方 lps-tools 的 DfuSe 实现烧写 .dfu 固件（与 GUI 完全同一条代码路径）
    4. 烧完自动 leave DFU，节点重启回固件

用法：
    # 看当前有哪些节点 / DFU 设备
    python tools/lps_flash.py --list

    # 刷一个节点（自动进 DFU）
    python tools/lps_flash.py --port COM5

    # 指定固件
    python tools/lps_flash.py --port COM5 --firmware firmware/lps-node-firmware-2022.09.dfu

    # 设备已经在 DFU 模式（不再发送 'u'）
    python tools/lps_flash.py --no-enter

    # 交互式批量刷（每次提示插一个节点）
    python tools/lps_flash.py --all

注意：
    * 刷写需要 pyusb 能访问 DFU 设备。Windows 上如果报 "No DfuSe compatible device"，
      说明 0483:df11 没有绑定可用驱动，用 Zadig 给它装 WinUSB/libusb 驱动。
    * 若一切失败，可直接双击已安装的 lpstool.exe（官方 GUI）刷写。
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FW = ROOT / "firmware" / "lps-node-firmware-2022.09.dfu"

NODE_VID, NODE_PID = 0x0483, 0x5740      # 正常运行：CDC 串口
DFU_VID, DFU_PID = 0x0483, 0xDF11        # DFU 引导模式


# ---------------------------------------------------------------------------
# 依赖加载：优先用已安装的 lpstools，否则退回仓库里的源码
# ---------------------------------------------------------------------------
def load_dfuse():
    try:
        from lpstools.dfu import dfu           # noqa: F401
        import dfuse                          # noqa: F401
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
            print("!! 无法导入 lpstools/dfuse：%s" % e)
            print("   请先运行 bootstrap.ps1（它会 pip install -e clones/lps-tools）")
            print("   或手动执行：python -m pip install pyserial pyusb")
            sys.exit(2)


_FAST_WAIT_APPLIED = False


def apply_fast_wait(dfuse_mod, cap_ms=50):
    """修掉"一次刷写要 11 分钟"的问题。

    STM32 的 DFU 引导在 GET_STATUS 里会返回 bwPollTimeout = 5000 ms，
    Bitcraze 官方 DfuSe 实现会照睡 5 秒；而每块扇区要等 3 次（擦除/设地址/写入），
    90016 字节 = 44 块 → 44 × 3 × 5s ≈ 660 秒。

    这里把单次睡眠上限压到 cap_ms，睡眠结束后继续轮询状态：
    只要设备仍是 BUSY 就会继续等，协议上是安全的，只是轮询更密。
    想恢复官方行为可用 --no-fast-wait。
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


def official_wait_notice():
    print("  [--]   等待策略：官方原速（严格按 STM32 上报的 bwPollTimeout 等待）")
    print("         一次刷写约 11 分钟，期间请勿拔线/关闭窗口。想加速可加 --wait-cap 500")


def list_ports_safe():
    from serial.tools import list_ports
    return list(list_ports.comports())


def find_node_ports():
    """返回处于正常运行模式的 LPS Node 串口"""
    out = []
    for p in list_ports_safe():
        if p.vid == NODE_VID and p.pid == NODE_PID:
            out.append(p)
    return out


def find_dfu_device(usb_mod):
    return usb_mod.core.find(idVendor=DFU_VID, idProduct=DFU_PID)


def open_dfu(dfuse_mod):
    """打开 DFU 设备、选中 Internal Flash 目标，返回 (DfuDevice, bState)"""
    import usb.core
    dev = usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID)
    if dev is None:
        return None, None
    d = dfuse_mod.DfuDevice(dev)
    for _name, intf in d.alternates():
        if intf.bAlternateSetting == 0:
            d.set_alternate(intf)
            break
    st = d.get_status()
    return d, st[1]


def release_dfu(d):
    """释放 pyusb 句柄/接口占用。

    libusb0 后端不允许同一进程重复 claim 同一接口，所以每次探测完必须释放，
    否则后续真正的刷写会在 claim_interface 处报"请求的资源在使用中"。
    """
    if d is None:
        return
    try:
        import usb.util
        usb.util.dispose_resources(d.dev)
    except Exception:
        pass


DFU_STUCK_STATES = {
    3: "DFU_DOWNLOAD_SYNC", 4: "DFU_DOWNLOAD_BUSY", 5: "DFU_DOWNLOAD_IDLE",
    6: "DFU_MANIFEST_SYNC", 7: "DFU_MANIFEST", 8: "DFU_MANIFEST_WAIT_RESET",
}


def ensure_dfu_idle(dfuse_mod, recover=False, timeout=25.0, quiet=False):
    """检查 DFU 设备是否可刷，必要时给出恢复建议。

    三种情况要分开处理：
      * bState=2  DFU_IDLE          → 正常，可以刷
      * bState=10 DFU_ERROR         → 清一次错误状态即可继续（官方 find_device 也这么做）
      * bState=3-8 下载/清单中间态   → 上次刷写被中断的残留，
                                      STM32 会拒绝新命令（表现为
                                      "win error: 连接到系统的设备没有发挥作用"），
                                      必须让设备重新进引导才能刷

    中间态的恢复：拔掉 USB → 按住 DFU 键 → 插 USB → 松手。
    （-recover 时会先尝试软件结束旧会话，但经常仍需要物理复位。）
    """
    d, state = open_dfu(dfuse_mod)
    if d is None:
        return False, "没有发现 DFU 设备"

    # DFU_ERROR：清掉错误状态，官方刷写流程本身也会清
    if state == 10:
        try:
            d.clear_status()
            time.sleep(0.5)
            st2 = d.get_status()[1]        # 同一个句柄再读一次（不能重复打开）
            if st2 == 2:
                release_dfu(d)
                return True, "已清除 DFU 错误状态，恢复为 dfuIDLE(2)"
            state = st2
        except Exception as e:
            if not quiet:
                print("         清除 DFU 错误状态失败：%s" % e)

    if state == 2:
        release_dfu(d)
        return True, "dfuIDLE(2) 正常"

    name = DFU_STUCK_STATES.get(state, "未知")
    if not recover:
        release_dfu(d)
        return False, (
            "DFU 停在 bState=%s(%s)——上次刷写中断留下的残留状态，"
            "STM32 这时会拒绝新命令。\n"
            "         恢复办法：拔掉 USB → 按住 DFU 键 → 插 USB → 松手，然后重跑本命令"
            % (state, name))

    if not quiet:
        print("  [警告] DFU 设备停在 bState=%s(%s)，这是上次刷写中断留下的残留状态"
              % (state, name))
        print("         正在自动恢复：结束上一次下载会话 ...")
    try:
        d.leave()                     # 零长度 DNLOAD，终止 DfuSe 下载会话
    except Exception as e:
        if not quiet:
            print("         leave 返回：%s（继续等设备重新枚举）" % e)
    release_dfu(d)
    del d

    import usb.core
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.8)
        if usb.core.find(idVendor=DFU_VID, idProduct=DFU_PID) is None:
            nodes = find_node_ports()
            if nodes:
                if not quiet:
                    print("         [--] 节点已启动(%s)，发送 'u' 让它重新进入 DFU"
                          % nodes[0].device)
                enter_dfu(nodes[0].device, quiet=True)
            continue
        d2, st2 = open_dfu(dfuse_mod)
        if d2 is not None:
            release_dfu(d2)
        if d2 is not None and st2 == 2:
            if not quiet:
                print("         [OK] 已恢复到 dfuIDLE，可以刷写")
            return True, "自动恢复成功"

    print("  [失败] 自动恢复未成功。请手动恢复：")
    print("         拔掉 USB → 按住 DFU 键 → 插 USB → 松手，然后重跑本命令")
    return False, "自动恢复失败"


def cmd_list():
    usb = __import__("usb")
    print("=== LPS Node（正常运行，CDC 串口）===")
    nodes = find_node_ports()
    if not nodes:
        print("  （未发现）")
    for p in nodes:
        print("  %-8s %s" % (p.device, p.description))

    # 关键诊断：USB 上有节点但系统没有串口 → 驱动被 libusb 占用
    if not nodes:
        try:
            usb_nodes = list(usb.core.find(find_all=True, idVendor=NODE_VID,
                                           idProduct=NODE_PID) or [])
        except Exception:
            usb_nodes = []
        if usb_nodes:
            print("")
            print("!! USB 上检测到 %d 个 0483:5740 设备（节点在运行），但系统没有串口：" %
                  len(usb_nodes))
            for d in usb_nodes:
                try:
                    name = d.product
                except Exception:
                    name = "?"
                print("   - %04x:%04x  %s" % (d.idVendor, d.idProduct, name))
            print("   原因：CDC 接口被 libusb-win32/Zadig 驱动抢占（与 Crazyflie 的 VID:PID 冲突）")
            print("   修复：设备管理器 → libusb-win32 devices → 'Crazyflie 2.x (Interface 0)'")
            print("         → 更新驱动程序 → 从列表选择 → 'USB 串行设备' → 拔插 USB")

    print("=== DFU 引导模式设备 ===")
    dev = find_dfu_device(usb)
    if dev is None:
        print("  （未发现）")
    else:
        try:
            print("  %04x:%04x  %s" % (dev.idVendor, dev.idProduct,
                                       dev.product or "STM32 BOOTLOADER"))
        except Exception:
            print("  %04x:%04x" % (dev.idVendor, dev.idProduct))


def pick_port(explicit=None):
    from serial import Serial
    if explicit:
        return explicit
    nodes = find_node_ports()
    if len(nodes) == 1:
        return nodes[0].device
    if not nodes:
        print("!! 没找到 LPS Node 串口。请确认：")
        print("   - USB 线是数据线（不是只供电的）")
        print("   - 节点已上电且不是处于 DFU 模式（DFU 模式下不出串口）")
        print("   - Windows 设备管理器中能看到 COM 口")
        sys.exit(2)
    print("!! 发现多个节点，请用 --port 指定：")
    for p in nodes:
        print("   %-8s %s" % (p.device, p.description))
    sys.exit(2)


def enter_dfu(port, baud=115200, quiet=False):
    """向节点串口发送 'u'，使其进入 DFU 引导模式"""
    import serial
    if not quiet:
        print("--> 让节点进入 DFU 模式（向 %s 发送 'u'）" % port)
    try:
        ser = serial.Serial(port, baud, timeout=0.3)
    except Exception as e:
        print("!! 打不开串口 %s：%s" % (port, e))
        sys.exit(2)
    try:
        time.sleep(0.3)
        ser.write(b"u")
        ser.flush()
        time.sleep(0.5)
    finally:
        ser.close()
    try:
        serial.Serial(port, baud, timeout=0.3).close()   # 等串口消失
    except Exception:
        pass


def wait_for_dfu(usb_mod, timeout=25.0, quiet=False):
    deadline = time.time() + timeout
    while time.time() < deadline:
        dev = find_dfu_device(usb_mod)
        if dev is not None:
            if not quiet:
                print("--> DFU 设备已就绪（0483:df11）")
            return dev
        time.sleep(0.4)
    print("!! 等待 DFU 设备超时（%.0fs）。可能原因：" % timeout)
    print("   - 节点没有进入引导模式（有些老固件不支持 'u' 命令）")
    print("   - 需要装驱动：用 Zadig 给 0483:df11 绑定 WinUSB/libusb")
    sys.exit(3)


def flash_file(dfu_cls, fw_path, quiet=False):
    fw_path = Path(fw_path)
    if not fw_path.exists():
        print("!! 固件不存在：%s" % fw_path)
        sys.exit(2)

    state = {"lastpct": -1}
    t0 = time.time()

    def cb(name, fraction):
        pct = int(round(fraction * 100))
        if pct != state["lastpct"]:
            state["lastpct"] = pct
            if not quiet:
                # 输出被下游关闭（例如 head / Select-Object -First）时不能影响刷写本身
                try:
                    elapsed = time.time() - t0
                    eta = ""
                    if fraction > 0.02:
                        left = elapsed / fraction - elapsed
                        eta = "  已用 %d:%02d  剩余约 %d:%02d" % (
                            int(elapsed) // 60, int(elapsed) % 60,
                            int(left) // 60, int(left) % 60)
                    bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
                    sys.stdout.write("\r    %s [%s] %3d%%%s" % (name, bar, pct, eta))
                    sys.stdout.flush()
                except (BrokenPipeError, OSError, ValueError):
                    state["quiet_output"] = True

    print("--> 烧写 %s（%d 字节）" % (fw_path.name, fw_path.stat().st_size))
    try:
        dfu_cls().flash(str(fw_path), cb)
    except Exception as e:
        print("\n!! 烧写失败：%s: %s" % (type(e).__name__, e))
        print("   可改用官方 GUI：双击 lpstool.exe 选择固件后刷写")
        sys.exit(4)
    if not quiet:
        print("\r    Flashing [%s] 100%%" % ("#" * 25))
    print("--> 完成，用时 %.1f 秒" % (time.time() - t0))


def flash_one(dfu_cls, dfuse_mod, usb_mod, port, fw, wait, do_enter,
              quiet=False, recover=False):
    if do_enter:
        enter_dfu(port, quiet=quiet)
    wait_for_dfu(usb_mod, timeout=wait, quiet=quiet)
    ready, msg = ensure_dfu_idle(dfuse_mod, recover=recover, quiet=quiet)
    if not ready:
        print("  [失败] " + msg)
        sys.exit(5)
    flash_file(dfu_cls, fw, quiet=quiet)
    time.sleep(1.5)
    print("    节点应已重启。建议拔插一次 USB 再用 --read 验证配置。")


def main():
    ap = argparse.ArgumentParser(description="LPS Node 固件刷写（进 DFU + DfuSe 烧写）")
    ap.add_argument("--list", action="store_true", help="列出节点与 DFU 设备后退出")
    ap.add_argument("--port", help="节点串口，例如 COM5；不指定则自动识别")
    ap.add_argument("--firmware", default=str(DEFAULT_FW), help=".dfu 固件路径")
    ap.add_argument("--wait", type=float, default=25.0, help="等待 DFU 设备出现的秒数")
    ap.add_argument("--no-enter", action="store_true", help="设备已在 DFU 模式，不发送 'u'")
    ap.add_argument("--all", action="store_true", help="交互式批量：逐个插节点刷写")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做预检：解析固件 + 检查 DFU 设备状态，不写入任何数据")
    ap.add_argument("--no-fast-wait", action="store_true",
                    help="不压缩 DFU 轮询等待（官方原速，一次刷写约 11 分钟）")
    ap.add_argument("--wait-cap", type=int, default=0, metavar="MS",
                    help="DFU 单次等待上限（毫秒）。默认 0 = 官方原速（最稳，约 11 分钟）；"
                         "想加速可设 500/1000（有中途失败风险）")
    ap.add_argument("--recover", action="store_true",
                    help="DFU 卡在残留状态时先尝试软件恢复（多数情况仍需手动拔插重进引导）")
    args = ap.parse_args()

    usb = __import__("usb")

    if args.list:
        cmd_list()
        return

    dfu_cls, dfuse_mod = load_dfuse()
    if args.wait_cap > 0:
        apply_fast_wait(dfuse_mod, cap_ms=args.wait_cap)
        print("  [--]   等待策略：加速模式（单次等待上限 %d ms，若中途失败请改用默认原速）"
              % args.wait_cap)
    else:
        official_wait_notice()

    if args.dry_run:
        fw = Path(args.firmware)
        print("=== 固件预检 ===")
        if not fw.exists():
            print("!! 固件不存在：%s" % fw)
            sys.exit(2)
        print("  文件: %s (%d 字节)" % (fw, fw.stat().st_size))
        try:
            dfuf = dfuse_mod.DfuFile(str(fw))
            for t in dfuf.targets:
                print("  target alternate=%s name=%s" % (t["alternate"], t.get("name")))
                for e in t["elements"]:
                    print("    element 地址=0x%08X 大小=%d 字节"
                          % (e["address"], len(e["data"])))
        except Exception as e:
            print("!! 固件解析失败：%s" % e)
            sys.exit(2)

        print("=== DFU 设备预检 ===")
        dev = find_dfu_device(usb)
        if dev is None:
            print("  未发现 DFU 设备（节点可能还在正常运行模式）。")
            print("  正常：加 --port COMx 后脚本会先让节点进入 DFU 模式。")
        else:
            print("  设备: %04x:%04x" % (dev.idVendor, dev.idProduct))
            try:
                d = dfuse_mod.DfuDevice(dev)
                alts = list(d.alternates())
                print("  可选目标: %s" % ", ".join(
                    "alt=%d%s" % (intf.bAlternateSetting, (" " + n) if n else "")
                    for n, intf in alts))
                for name, intf in alts:
                    if intf.bAlternateSetting == 0:
                        d.set_alternate(intf)
                        break
                st = d.get_status()
                print("  DFU 状态: bStatus=%d bState=%d（2=DFU_IDLE 正常）" % (st[0], st[1]))
            except Exception as e:
                print("  打开设备失败：%s: %s" % (type(e).__name__, e))
                print("  -> Windows 上可能需要用 Zadig 给 0483:df11 绑定 WinUSB/libusb 驱动")
        print("\n预检结束，未写入任何数据。")
        return

    if args.all:
        n = 0
        print("批量刷写模式：每次只插一个节点。Ctrl+C 退出。")
        try:
            while True:
                input("请插入/连接下一个要刷写的节点，然后按回车 ...")
                time.sleep(1.0)
                port = pick_port(None)
                n += 1
                print("=== 第 %d 个节点（%s）===" % (n, port))
                flash_one(dfu_cls, dfuse_mod, usb, port, args.firmware, args.wait,
                          not args.no_enter, recover=args.recover)
                input("请拔掉这个节点，然后按回车继续 ...")
        except KeyboardInterrupt:
            print("\n已退出。共刷写 %d 个节点。" % n)
        return

    if args.no_enter:
        wait_for_dfu(usb, timeout=args.wait)
        okrec, msg = ensure_dfu_idle(dfuse_mod, recover=args.recover)
        if not okrec:
            print("  [失败] " + msg)
            sys.exit(5)
        flash_file(dfu_cls, args.firmware)
        return

    port = pick_port(args.port)
    flash_one(dfu_cls, dfuse_mod, usb, port, args.firmware, args.wait, True,
              recover=args.recover)


if __name__ == "__main__":
    main()
