# -*- coding: utf-8 -*-
"""
合成数据仿真：验证 lps_tdoa3_solver.py 的 TDoA 数学与符号约定。

构造一个"理想但带真实时钟漂移"的 TDoA3 空中报文序列：
  - 8 个锚点，各自有独立时钟速率(±10 ppm)和随机初相位
  - 锚点互相测距得到 tof 字段（理想值 = 真实距离）
  - 每个锚点包里带"最近收到的其它锚点包的 rx 时间戳 + 距离"
  - 标签(嗅探器)被动接收，记录本地 40 位接收时间戳
然后喂给 solver 的解码/时钟修正/TDoA/定位流程，看能否还原标签真实位置。

运行：python3 test_solver_sim.py
预期输出：解算位置误差 < 1 cm，rms ≈ 2 mm
"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lps_tdoa3_solver as S  # noqa: E402

TPM = 1.0 / S.M_PER_TICK          # ticks per meter
FREQ = S.LOCODECK_TS_FREQ
MASK40 = (1 << 40) - 1

anchors = {
    0: np.array([0.0, 0.0, 2.40]),
    1: np.array([6.0, 0.0, 2.40]),
    2: np.array([6.0, 5.0, 2.40]),
    3: np.array([0.0, 5.0, 2.40]),
    4: np.array([0.0, 0.0, 0.20]),
    5: np.array([6.0, 0.0, 0.20]),
    6: np.array([6.0, 5.0, 0.20]),
    7: np.array([0.0, 5.0, 0.20]),
}
tag_true = np.array([2.7, 2.2, 1.0])

rates = {i: 1.0 + (i - 4) * 2.5e-6 for i in anchors}      # ±10 ppm 量级
offsets = {i: (0x1_2345_6789 * (i + 1)) & 0xFFFFFFFF for i in anchors}


def clk_anchor(i, t_true):
    """锚点 i 在真实时刻 t_true(ticks) 时的本地时间戳"""
    return t_true * rates[i] + offsets[i]


def build_air_trace():
    """生成 (标签接收时刻, 锚点id, seq, 打包好的 payload) 列表"""
    period = int(0.010 * FREQ)          # 锚点平均 100 Hz
    t0 = 5_000_000_000                  # 任意起始时刻(ticks)
    tx_list = []
    for k in range(40):
        for i in sorted(anchors):
            tx_list.append((t0 + k * period + i * (period // 8), i, k % 128))
    tx_list.sort()

    latest = {}                          # j -> (seq, t_true)
    out = []
    for (t, i, seq) in tx_list:
        remotes = []
        for j in anchors:
            if j == i:
                continue
            entry = latest.get(j)
            if entry is None:
                continue
            seq_j, t_j = entry
            d_ij = float(np.linalg.norm(anchors[i] - anchors[j]))
            if t_j + d_ij * TPM > t:      # 还没传到，不能用
                continue
            rx_in_i = clk_anchor(i, t_j + d_ij * TPM)
            remotes.append((j, seq_j & 0x7F, int(rx_in_i) & 0xFFFFFFFF,
                            int(round(d_ij * TPM)) & 0xFFFF))
        remotes = remotes[:8]             # 协议上限 8 个

        payload = bytearray()
        payload += struct.pack("<BBLB", S.PACKET_TYPE_TDOA3, seq & 0x7F,
                               int(clk_anchor(i, t)) & 0xFFFFFFFF, len(remotes))
        for (j, seq_j, rx_ij, dist) in remotes:
            payload += struct.pack("<BBL", j, (seq_j & 0x7F) | 0x80, rx_ij)
            payload += struct.pack("<H", dist)

        d_i = float(np.linalg.norm(tag_true - anchors[i]))
        rx_tag = int(t + d_i * TPM) & MASK40
        out.append((rx_tag, i, seq & 0x7F, bytes(payload)))

        latest[i] = (seq, t)

    out.sort(key=lambda x: x[0])
    return out


def main():
    trace = build_air_trace()
    print(f"构造报文 {len(trace)} 个")

    states = {}
    for aid, p in anchors.items():
        st = S.AnchorState(aid)
        st.configured = True
        st.position_hint = p
        states[aid] = st

    stats = {"frames": 0, "tdoa3": 0, "measurements": 0, "bound_rejected": 0}
    latest = {}
    for (rx_tag, src, seq, payload) in trace:
        pkt = S.decode_tdoa3(payload)
        assert pkt is not None, "TDoA3 解码失败"
        assert pkt["seq"] == seq, f"seq 解码错误 {pkt['seq']} != {seq}"
        now = rx_tag / FREQ
        states[src].note_packet(rx_tag, pkt["seq"], pkt["tx"], now)
        for (i, j, d) in S.compute_tdoa(states, src, pkt, rx_tag, now, stats):
            latest[(i, j)] = d

    print(f"解出 TDoA 测量 {len(latest)} 组（stats={stats}）")
    print("锚点时钟修正估计值（应等于 1/rate，即 ~1 加减几个 ppm）：")
    for aid in sorted(states):
        print(f"  anchor {aid}: cc={states[aid].cc.value:.9f}  1/rate={1/rates[aid]:.9f}")

    meas = [(i, j, d) for (i, j), d in latest.items()]
    print("\nTDoA 测量 vs 真值（取前 8 组）：")
    for (i, j, d) in meas[:8]:
        truth = float(np.linalg.norm(tag_true - anchors[i]) - np.linalg.norm(tag_true - anchors[j]))
        print(f"  ({i},{j}) meas={d:+.4f} m  truth={truth:+.4f} m  err={d - truth:+.4f} m")

    p0 = np.mean(list(anchors.values()), axis=0)
    p, rms = S.solve_position(meas, anchors, p0)
    print(f"\n解算位置 = {np.round(p, 4)} m")
    print(f"真实位置 = {tag_true} m")
    print(f"误差 = {np.linalg.norm(p - tag_true) * 100:.2f} cm   rms={rms:.4f} m")


if __name__ == "__main__":
    main()
