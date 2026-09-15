#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_capture.py — capture raw UWB traffic from an LPS Node running in Sniffer mode

    python tools\\lps_capture.py --port COM20 --seconds 30 --summary

What it does
------------
1. resets the node console back to the main menu (so our keystrokes are not
   swallowed by a sub-menu left over from a previous tool),
2. switches the sniffer to **binary** output (key `b`) and stores the raw byte
   stream into captures/*.bin,
3. parses the stream to give per-anchor statistics.

Two output formats are handled transparently:

    binary : 0xBC | ts(5B) | src | dst | len(2B) | payload | len(2B)
    text   : From 07 to ff @50c1e6c9a7: 307200806fb5...

The firmware prints the text form until the `b` key takes effect. If that is
what we see, the capture is still complete (the text carries the same hardware
timestamp and payload), so we parse it and additionally write a normalised
binary file `<name>.norm.bin` that the solver can replay with `--input`.

Note: binary mode lives in RAM only, so replugging the node resets it to text.
"""

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = ROOT / "captures"

SNIFFER_SYNC = 0xBC
TEXT_RE = re.compile(
    rb"From\s+([0-9a-fA-F]{2})\s+to\s+([0-9a-fA-F]{2})\s+@([0-9a-fA-F]{10}):\s*([0-9a-fA-F]*)")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lps_config as C          # noqa: E402  (reset_console)


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


class CaptureParser:
    """同一个流里【同时】解析二进制帧与文本行。

    真实抓包里两种格式会混在一起：刚打开串口时的横幅/文本模式数据在前，
    发 'b' 生效后的二进制帧在后。所以不能"锁定"某一种格式，而要逐次扫描缓冲区，
    谁先出现就按谁解析，并跳过无法识别的内容。
    """

    def __init__(self, norm_path=None):
        self.buf = bytearray()
        self.frames = 0
        self.binary_frames = 0
        self.text_frames = 0
        self.by_src = Counter()
        self.tdoa3 = 0
        self.last_seen = {}
        self.bad_sync = 0
        self.other_lines = 0
        self.norm_path = norm_path
        self._norm = None

    # ---------- 规范化输出（文本模式用） ----------
    def _norm_write(self, ts5, src, dst, payload):
        if self.norm_path is None:
            return
        if self._norm is None:
            self._norm = open(self.norm_path, "wb")
        ln = len(payload).to_bytes(2, "little")
        self._norm.write(bytes([SNIFFER_SYNC]) + ts5 + bytes([src, dst]) + ln + payload + ln)

    def close(self):
        if self._norm is not None:
            self._norm.close()

    # ---------- 主入口 ----------
    def feed(self, chunk, now):
        self.buf += chunk
        while True:
            i_bin = self.buf.find(b"\xbc")        # 二进制帧同步字节
            i_txt = self.buf.find(b"From ")       # 文本行起始

            if i_bin < 0 and i_txt < 0:
                # 什么都认不出：丢掉，只留尾部几个字节（可能是跨块的 "From " 前缀）
                if len(self.buf) > 8:
                    del self.buf[:-8]
                return
            # 选更早出现的那个格式
            if i_txt >= 0 and (i_bin < 0 or i_txt < i_bin):
                nl = self.buf.find(b"\n", i_txt)
                if nl < 0:
                    if len(self.buf) > 65536:     # 超长行，丢弃
                        del self.buf[:i_txt]
                    return
                line = bytes(self.buf[i_txt:nl])
                del self.buf[:nl + 1]
                self._parse_text_line(line, now)
                continue

            # 二进制帧
            if i_bin:
                del self.buf[:i_bin]
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
            ts5 = bytes(self.buf[1:6])
            src = self.buf[6]
            dst = self.buf[7]
            payload = bytes(self.buf[10:10 + length])
            del self.buf[:12 + length]
            if length != length2:
                self.bad_sync += 1
                continue
            self.binary_frames += 1
            self._norm_write(ts5, src, dst, payload)
            self._count(src, payload, now)

    # ---------- 文本行 ----------
    def _parse_text_line(self, line, now):
        m = TEXT_RE.search(line)
        if not m:
            if line.strip():
                self.other_lines += 1
            return
        src = int(m.group(1), 16)
        dst = int(m.group(2), 16)
        hx_ts = m.group(3).decode("ascii")
        hx_payload = m.group(4).decode("ascii")
        if len(hx_payload) % 2:          # 抓包在被截断时可能留下半个字节
            hx_payload = hx_payload[:-1]
        try:
            ts5 = bytes.fromhex(hx_ts)
            payload = bytes.fromhex(hx_payload)
        except ValueError:
            return
        if not payload:
            return
        self.text_frames += 1
        self._norm_write(ts5, src, dst, payload)
        self._count(src, payload, now)

    def mode_text(self):
        """给用户看的一行说明"""
        if self.binary_frames and self.text_frames:
            return "混合（文本 %d 帧 + 二进制 %d 帧）" % (self.text_frames, self.binary_frames)
        if self.binary_frames:
            return "二进制帧"
        if self.text_frames:
            return "文本格式"
        return "未知"

    def _count(self, src, payload, now):
        self.frames += 1
        self.by_src[src] += 1
        self.last_seen[src] = now
        if payload and payload[0] == 0x30:
            self.tdoa3 += 1


def main():
    ap = argparse.ArgumentParser(description="抓取 Sniffer 节点的原始 UWB 数据")
    ap.add_argument("--port", help="Sniffer 节点串口，例如 COM20")
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
    out_path = Path(args.out) if args.out else DEFAULT_OUTDIR / time.strftime("cap_%Y%m%d_%H%M%S.bin")
    norm_path = out_path.with_suffix(".norm.bin")

    print("--> 打开 %s" % port)
    try:
        ser = serial.Serial(port, 115200, timeout=0.05)
    except Exception as e:
        print("!! 打不开串口 %s：%s" % (port, e))
        print("   另一个程序（比如官方 GUI）可能正占用它。")
        sys.exit(2)

    time.sleep(0.3)
    print("--> 复位节点控制台，然后切换到二进制输出模式")
    C.reset_console(ser)          # 关键：先退出任何子菜单，否则 'b' 会被菜单吃掉
    ser.reset_input_buffer()
    ser.write(b"b")
    ser.flush()
    time.sleep(0.3)

    parser = CaptureParser(norm_path=norm_path)
    total_bytes = 0
    t0 = time.time()
    last_report = t0
    warned_text = False

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
                    parser.feed(chunk, now)
                    if (parser.text_frames and not parser.binary_frames) and not warned_text:
                        warned_text = True
                        print()
                        print("!! 节点仍在输出【文本格式】（'b' 没被主菜单收到，控制台可能停在子菜单里）")
                        print("   数据依然完整，脚本已自动按文本解析；")
                        print("   同时写出规范化文件：%s" % norm_path.name)
                        print()
                else:
                    time.sleep(0.005)

                if not args.quiet and (now - last_report) >= 2.0:
                    rate = parser.frames / max(1e-6, now - t0)
                    print("    %5.1fs  %8.1f KB  %6d 帧  %6.0f 帧/秒  锚点 %d 个"
                          % (now - t0, total_bytes / 1024, parser.frames, rate,
                             len(parser.by_src)))
                    last_report = now
    except KeyboardInterrupt:
        print("\n(用户中断)")
    finally:
        ser.close()
        parser.close()

    elapsed = time.time() - t0
    print("--> 结束：%.1f 秒，%.1f KB，%d 帧（%.0f 帧/秒）"
          % (elapsed, total_bytes / 1024, parser.frames,
             parser.frames / max(1e-6, elapsed)))
    print("    原始文件：%s" % out_path)
    print("    解析格式：%s（文本 %d 帧 / 二进制 %d 帧）"
          % (parser.mode_text(), parser.text_frames, parser.binary_frames))

    if parser.frames == 0:
        print("!! 一帧都没解析出来。检查：")
        print("   - 这个节点确实被配置成 Sniffer 模式（lps_config.py --read）")
        print("   - 锚点已上电并在发包（MODE 灯常亮、RANGING 灯闪烁）")
        print("   - 射频参数（比特率/前导码）与锚点一致")
        if parser.other_lines:
            print("   - 收到了 %d 行非数据文本，原始文件里能看到节点输出的具体内容" % parser.other_lines)
        return

    if args.summary:
        print("\n=== 每个锚点的收包统计 ===")
        print("  %-6s %-10s %-12s %s" % ("锚点", "包数", "收包率", "最近出现"))
        for src in sorted(parser.by_src):
            cnt = parser.by_src[src]
            age = time.time() - parser.last_seen.get(src, 0)
            print("  %-6d %-10d %-12s %.2f s 前"
                  % (src, cnt, "%.1f Hz" % (cnt / max(1e-6, elapsed)), age))
        print("  TDoA3 包(type=0x30)：%d / %d" % (parser.tdoa3, parser.frames))
        if len(parser.by_src) < 4:
            print("  !! 可见锚点少于 4 个，不足以三维定位（需要 >= 4 个基站）")
        if parser.bad_sync:
            print("  同步失败丢弃：%d" % parser.bad_sync)
        if parser.other_lines:
            print("  非数据文本行：%d" % parser.other_lines)

    replay = norm_path if norm_path.exists() else out_path
    print("\n回放/解算命令：")
    print("  python tools\\lps_tdoa3_solver.py --input \"%s\" --anchors tools\\anchors_example.yaml"
          % replay)


if __name__ == "__main__":
    main()
