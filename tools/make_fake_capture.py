#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_fake_capture.py —— 生成"假"的 Sniffer 抓包文件，用于无硬件自检

它复用 test_solver_sim.py 里的空中报文仿真，把报文按 LPS Node 的
sniffer 二进制帧格式（0xBC 帧头 + 40 位硬件时间戳 + 源/目的 + 长度 + payload）
写成 .bin 文件，同时生成配套的锚点坐标 YAML。

这样就能在没有任何硬件的情况下验证整条链路：
    sniffer 帧解析 -> TDoA3 解包 -> 时钟修正 -> TDoA -> 三维解算

用法：
    python tools/make_fake_capture.py
    python tools/lps_tdoa3_solver.py --input captures/fake_tdoa3.bin ^
           --anchors captures/fake_anchors.yaml

预期：解算位置 ≈ (2.700, 2.200, 1.000)，误差 < 1 cm
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_solver_sim as sim  # noqa: E402

SNIFFER_SYNC = 0xBC
MASK40 = (1 << 40) - 1


def write_capture(path, trace):
    n = 0
    with open(path, "wb") as f:
        for (rx_tag, src, _seq, payload) in trace:
            ts5 = (rx_tag & MASK40).to_bytes(8, "little")[:5]
            length = len(payload)
            frame = (bytes([SNIFFER_SYNC]) + ts5 + bytes([src & 0xFF, 0xFF])
                     + length.to_bytes(2, "little") + payload
                     + length.to_bytes(2, "little"))
            f.write(frame)
            n += 1
    return n


def write_anchors(path):
    lines = ["# 由 make_fake_capture.py 生成：与仿真数据严格对应", "", "anchors:"]
    for aid in sorted(sim.anchors):
        p = sim.anchors[aid]
        lines.append("  %d: {x: %.3f, y: %.3f, z: %.3f}" % (aid, p[0], p[1], p[2]))
    lines.append("")
    lines.append("# 仿真标签真实位置: %.3f, %.3f, %.3f"
                 % (sim.tag_true[0], sim.tag_true[1], sim.tag_true[2]))
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="生成假 sniffer 抓包（离线自检）")
    ap.add_argument("--out", default=str(ROOT / "captures" / "fake_tdoa3.bin"),
                    help="输出 .bin 路径")
    ap.add_argument("--anchors", default=str(ROOT / "captures" / "fake_anchors.yaml"),
                    help="输出的锚点坐标 YAML 路径")
    args = ap.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    trace = sim.build_air_trace()
    n = write_capture(args.out, trace)
    write_anchors(args.anchors)

    size = os.path.getsize(args.out)
    print("已生成 %d 帧 -> %s (%.1f KB)" % (n, args.out, size / 1024))
    print("配套锚点坐标 -> %s" % args.anchors)
    print("仿真标签真实位置: (%.3f, %.3f, %.3f)" % tuple(sim.tag_true))
    print("\n自检命令：")
    print("  python tools/lps_tdoa3_solver.py --input \"%s\" --anchors \"%s\""
          % (args.out, args.anchors))


if __name__ == "__main__":
    main()
