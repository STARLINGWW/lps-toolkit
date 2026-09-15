#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_capture.py —— 从 Sniffer 模式的 LPS Node 抓取原始 UWB 数据

做的事：
    1. 打开 sniffer 节点串口，发送 'b' 切到二进制输出模式（掉电失效，不影响 EEPROM）
    2. 把原始字节流原样写入 captures/*.bin（这就是解算器 --input 能回放的格式）
    3. 同时解析帧做统计：每个锚点收到多少包、收包率、最近一次出现时间

用法：
    # 抓 30 秒
    python tools/lps_capture.py --port COM5 --seconds 30

    # 抓 60 秒，并打印每个锚点的收包统计（系统联调验收）
    python tools/lps_capture.py --port COM5 --seconds 60 --summary

    # 无限抓，Ctrl+C 结束
    python tools/lps_capture.py --port COM5 --seconds 0

抓完后可以直接回放解算：
    python tools/lps_tdoa3_solver.py --input captures/xxx.bin ^
        --anchors tools/anchors_example.yaml --check

说明：节点的二进制模式只存在于 RAM，拔插 USB 即恢复正常串口菜单。
"""

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = ROOT / "captures"

SNIFFER_SYNC = 0xBC


def pick_port(explicit):
    from serial.tools import list_ports
    all_ports = list(list_ports.comports())
    nodes = [p for p in all_ports if p.vid == 0x0483 and p.pid == 0x5740]
    if explicit:
        known = {p.device.upper() for p in all_ports}
        if explicit.upper() not in known:
            print("!! 串口 %s 不存在。当前系统上的串口有：" % explicit)
            for p in all_ports:
                print("   %-8s %s" % (p.device, p.description))
            if not all_ports:
                print("   （一个都没有）")
            sys.exit(2)
        return explicit
    if len(nodes) == 1:
        return nodes[0].device
    if not nodes:
        print("!! 没找到 LPS Node。确认 USB 数据线 / 供电 / 不在 DFU 模式。")
        sys.exit(2)
    print("!! 多个节点在线，请用 --port 指定（应该是那个配置成 Sniffer 的节点）：")
    for p in nodes:
        print("   %-8s %s" % (p.device, p.description))
    sys.exit(2)


class FrameScanner:
    """在线解析 sniffer 二进制帧，只做统计（原始字节照旧原样保存）"""

    def __init__(self):
        self.buf = bytearray()
        self.frames = 0
        self.by_src = Counter()
        self.tdoa3 = 0
        self.last_seen = {}
        self.bad_sync = 0

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
                self.bad_sync += 1
                continue
            if len(self.buf) < 12 + length:
                return
            length2 = int.from_bytes(bytes(self.buf[10 + length:12 + length]), "little")
            src = self.buf[6]
            payload = bytes(self.buf[10:10 + length])
            del self.buf[:12 + length]
            if length != length2:
                self.bad_sync += 1
                continue
            self.frames += 1
            self.by_src[src] += 1
            self.last_seen[src] = now
            if payload and payload[0] == 0x30:
                self.tdoa3 += 1


def main():
    ap = argparse.ArgumentParser(description="抓取 Sniffer 节点的原始 UWB 数据")
    ap.add_argument("--port", help="Sniffer 节点串口，例如 COM5")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="抓取时长（秒）；0 表示无限直到 Ctrl+C")
    ap.add_argument("--out", help="输出文件路径（默认 captures/cap_时间戳.bin）")
    ap.add_argument("--summary", action="store_true",
                    help="结束时打印每个锚点的收包统计")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    args = ap.parse_args()

    import serial

    port = pick_port(args.port)
    DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = DEFAULT_OUTDIR / time.strftime("cap_%Y%m%d_%H%M%S.bin")

    print("--> 打开 %s 并切换到二进制模式" % port)
    try:
        ser = serial.Serial(port, 115200, timeout=0.05)
    except Exception as e:
        print("!! 打不开串口 %s：%s" % (port, e))
        print("   另一个程序（比如官方 GUI `python -m lpstools`）可能正占用它。")
        sys.exit(2)
    time.sleep(0.3)
    ser.reset_input_buffer()
    ser.write(b"b")
    ser.flush()
    time.sleep(0.3)

    scanner = FrameScanner()
    total_bytes = 0
    t0 = time.time()
    last_report = t0

    print("--> 开始抓取 -> %s" % out_path)
    print("    （时长 %s，Ctrl+C 可提前结束）"
          % ("无限" if args.seconds == 0 else "%.0f 秒" % args.seconds))

    try:
        with open(out_path, "wb") as f:
            while True:
                if args.seconds and (time.time() - t0) >= args.seconds:
                    break
                chunk = ser.read(8192)
                now = time.time()
                if chunk:
                    f.write(chunk)
                    total_bytes += len(chunk)
                    scanner.feed(chunk, now)
                else:
                    time.sleep(0.005)

                if not args.quiet and (now - last_report) >= 2.0:
                    rate = scanner.frames / max(1e-6, now - t0)
                    print("    %5.1fs  %8.1f KB  %6d 帧  %6.0f 帧/秒  锚点 %d 个"
                          % (now - t0, total_bytes / 1024, scanner.frames, rate,
                             len(scanner.by_src)))
                    last_report = now
    except KeyboardInterrupt:
        print("\n(用户中断)")
    finally:
        ser.close()

    elapsed = time.time() - t0
    print("--> 结束：%.1f 秒，%.1f KB，%d 帧（%.0f 帧/秒）"
          % (elapsed, total_bytes / 1024, scanner.frames,
             scanner.frames / max(1e-6, elapsed)))
    print("    文件：%s" % out_path)

    if scanner.frames == 0:
        print("!! 一帧都没收到。检查：")
        print("   - 这个节点确实被配置成 Sniffer 模式（lps_config.py --read）")
        print("   - 锚点已上电并在发包")
        print("   - 射频参数（比特率/前导码）与锚点一致")
        return

    if args.summary:
        print("\n=== 每个锚点的收包统计 ===")
        print("  %-6s %-10s %-12s %s" % ("锚点", "包数", "收包率", "最近出现"))
        for src in sorted(scanner.by_src):
            cnt = scanner.by_src[src]
            age = time.time() - scanner.last_seen.get(src, 0)
            print("  %-6d %-10d %-12s %.2f s 前"
                  % (src, cnt, "%.1f Hz" % (cnt / max(1e-6, elapsed)), age))
        print("  TDoA3 包(type=0x30)：%d / %d" % (scanner.tdoa3, scanner.frames))
        if scanner.bad_sync:
            print("  同步失败丢弃：%d" % scanner.bad_sync)

    print("\n回放/解算命令：")
    print("  python tools/lps_tdoa3_solver.py --input \"%s\" "
          "--anchors tools/anchors_example.yaml --check" % out_path)


if __name__ == "__main__":
    main()
