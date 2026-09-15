#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_tdoa3_solver.py —— Bitcraze LPS 的 TDoA3 主机侧解算原型

【定位】本脚本补的是 Bitcraze 官方工具链的空缺：
    官方给了 sniffer 二进制流 + Python 解码器（tools/sniffer/*.py），
    但没有给 Python 的时钟修正和 TDoA 解算 => 本脚本实现了这部分。

【数据源】一个烧成 Sniffer 模式（LPS Node 串口菜单按 's'）的 LPS Node，USB 直连。
         Sniffer 是纯被动监听，DW1000 在硬件层给每个包打 40 位接收时间戳。

【实现了什么】（算法逐行对照 C 源码移植，不是自创）
    1. sniffer 二进制帧解析           <- lps-node-firmware/src/uwb_sniffer.c
    2. TDoA3 包解析 (type=0x30)       <- docs/protocols/tdoa3_protocol.md
    3. 时钟修正（漏桶+低通+离群剔除）  <- crazyflie-firmware/src/utils/src/clockCorrectionEngine.c
    4. TDoA 差值计算                  <- crazyflie-firmware/src/utils/src/tdoa/tdoaEngine.c : calcTDoA()
    5. 三维定位（高斯-牛顿 + Huber）   <- 本脚本自研（官方是喂给 EKF，见 mm_tdoa.c）

【未做 / 需要你自己验证的部分】
    - 未上机验证！所有算法常数都照抄 C 源码，但没有任何真实数据跑过。
    - 未做多径/NLOS 处理、未做滤波器（输出是逐帧解算的原始位置）。
    - 天线延迟（各节点 RF 延迟差异）未单独标定，会带来系统性偏差。

【用法】
    1) 先只做解码自检，确认 sniffer 能收到所有锚点：
       python3 lps_tdoa3_solver.py --port COM5 --anchors anchors.yaml --check
    2) 解算并打印位置：
       python3 lps_tdoa3_solver.py --port COM5 --anchors anchors.yaml
    3) 输出 JSON 行（便于管道给 MAVLink/ROS 桥）：
       python3 lps_tdoa3_solver.py --port COM5 --anchors anchors.yaml --json
    4) 回放之前录下来的原始流（例如 Linux 上 cat /dev/ttyACM0 > capture.bin）：
       python3 lps_tdoa3_solver.py --input capture.bin --anchors anchors.yaml

【依赖】pyserial, numpy, pyyaml
"""

import argparse
import json
import os
import struct
import sys
import time

import numpy as np
import yaml

# --------------------------------------------------------------------------
# 常数（全部来自 Bitcraze 源码，改动前请先对照）
# --------------------------------------------------------------------------
SPEED_OF_LIGHT = 299792458.0
LOCODECK_TS_FREQ = 499.2e6 * 128          # = 63,897,600,000 Hz（DW1000 时间戳单位 15.65 ps）
M_PER_TICK = SPEED_OF_LIGHT / LOCODECK_TS_FREQ   # ≈ 4.6918 mm / tick
TS_MASK = 0xFFFFFFFF                      # 锚点侧时间戳按 32 位截断（约 67 ms 回绕）

# 时钟修正引擎常数（clockCorrectionEngine.c）
MAX_CLOCK_DEVIATION_SPEC = 10e-6
CC_SPEC_MIN = 1.0 - MAX_CLOCK_DEVIATION_SPEC * 2      # 0.99998
CC_SPEC_MAX = 1.0 + MAX_CLOCK_DEVIATION_SPEC * 2      # 1.00002
CC_ACCEPTED_NOISE = 0.03e-6
CC_FILTER = 0.1
CC_BUCKET_MAX = 4

PACKET_TYPE_TDOA3 = 0x30
SNIFFER_SYNC = 0xBC
LPP_HEADER_SHORT = 0xF0
LPP_SHORT_ANCHOR_POSITION = 0x01

# 经验门限（本脚本自定，不是官方值）
MAX_REFERENCE_AGE_S = 0.1        # 引用的远端包最多多老
MAX_MATCH_PACKETS = 32           # 每个锚点保留的历史包数
OUTLIER_GATE_M = 1.0             # 位置解算的粗差门限
HUBER_DELTA_M = 0.3


# --------------------------------------------------------------------------
# 1. Sniffer 二进制帧解析
#    帧格式（uwb_sniffer.c）:
#      0xBC | ts(5B) | src(1B) | dst(1B) | len(2B) | payload(len) | len(2B)
# --------------------------------------------------------------------------
def read_frames(stream, is_file=False):
    """从字节流中切出 sniffer 帧，yield (ts_tag, src_id, dst_id, payload)

    is_file=True 时读到空数据视为结束；串口（is_file=False）时视为超时，继续等。
    """
    buf = bytearray()
    idle_since = time.time()
    while True:
        chunk = stream.read(4096)
        if not chunk:
            if is_file:
                return
            time.sleep(0.005)
            if time.time() - idle_since > 1.0 and len(buf) > 1:
                buf.clear()          # 长时间收不到数据，丢弃半截帧重新同步
                idle_since = time.time()
            continue
        idle_since = time.time()
        buf += chunk
        while True:
            try:
                start = buf.index(SNIFFER_SYNC)
            except ValueError:
                buf.clear()
                break
            if start:
                del buf[:start]
            if len(buf) < 10:
                break
            ts_raw = bytes(buf[1:6])
            ts = int.from_bytes(ts_raw + b"\x00\x00\x00", "little")
            src = buf[6]
            dst = buf[7]
            length = int.from_bytes(bytes(buf[8:10]), "little")
            if length > 1024:
                del buf[:1]          # 误同步，跳过
                continue
            if len(buf) < 10 + length + 2:
                break
            payload = bytes(buf[10:10 + length])
            length2 = int.from_bytes(bytes(buf[10 + length:12 + length]), "little")
            del buf[:12 + length]
            if length != length2:
                continue             # 同步校验失败，丢弃
            yield ts, src, dst, payload


# --------------------------------------------------------------------------
# 2. TDoA3 包解析
# --------------------------------------------------------------------------
def decode_tdoa3(payload):
    """解析 type=0x30 的 TDoA3 包，返回 dict 或 None"""
    if len(payload) < 7 or payload[0] != PACKET_TYPE_TDOA3:
        return None

    seq, tx_ts, remote_count = struct.unpack_from("<BLB", payload, 1)
    pkt = {"seq": seq, "tx": tx_ts, "remotes": [], "lpp_position": None}

    o = 7
    for _ in range(remote_count):
        if o + 6 > len(payload):
            return pkt              # 截断包，返回已有的部分
        rid, seqbyte, rx_ts = struct.unpack_from("<BBL", payload, o)
        o += 6
        has_distance = (seqbyte & 0x80) != 0
        remote = {"id": rid, "seq": seqbyte & 0x7F, "rx": rx_ts, "distance": None}
        if has_distance:
            if o + 2 > len(payload):
                return pkt
            remote["distance"] = struct.unpack_from("<H", payload, o)[0]
            o += 2
        pkt["remotes"].append(remote)

    # LPP 数据：锚点自己的坐标（0xF0 0x01 x y z 各 4 字节 float）
    if o + 2 < len(payload) and payload[o] == LPP_HEADER_SHORT:
        if payload[o + 1] == LPP_SHORT_ANCHOR_POSITION and o + 14 <= len(payload):
            pkt["lpp_position"] = struct.unpack_from("<fff", payload, o + 2)

    return pkt


# --------------------------------------------------------------------------
# 3. 时钟修正引擎（逐行对照 clockCorrectionEngine.c 移植）
# --------------------------------------------------------------------------
class ClockCorrection:
    def __init__(self):
        self.value = 0.0
        self.bucket = 0

    def calculate(self, new_ref, old_ref, new_x, old_x):
        """(新-旧)在参考时钟 / (新-旧)在x时钟 => 把 x 时钟换算到参考时钟的比值"""
        ticks_ref = (new_ref - old_ref) & TS_MASK
        ticks_x = (new_x - old_x) & TS_MASK
        if ticks_x == 0:
            return -1.0
        return float(ticks_ref) / float(ticks_x)

    def update(self, candidate):
        """返回 True 表示样本可信（已通过噪声门限并被低通滤波）"""
        reliable = False
        diff = candidate - self.value

        if -CC_ACCEPTED_NOISE < diff < CC_ACCEPTED_NOISE:
            # 简单低通：注意源码是 current*0.1 + candidate*0.9
            self.value = self.value * CC_FILTER + candidate * (1.0 - CC_FILTER)
            reliable = True
            if self.bucket < CC_BUCKET_MAX:
                self.bucket += 1
        else:
            if self.bucket > 0:
                self.bucket -= 1
            else:
                # 漏桶空了 => 允许重置参考值（但本样本不算 reliable）
                if CC_SPEC_MIN < candidate < CC_SPEC_MAX:
                    self.value = candidate

        return reliable


class AnchorState:
    """一个锚点在标签侧的状态"""

    def __init__(self, anchor_id):
        self.id = anchor_id
        self.configured = False      # 是否在 anchors.yaml 里配置了坐标
        self.position_hint = np.zeros(3)
        self.cc = ClockCorrection()
        self.last_rx_tag = None      # 上一包在本机(标签)时钟下的接收时刻
        self.last_tx_anchor = None   # 上一包在锚点时钟下的发送时刻
        self.history = {}            # seq -> (rx_tag, tx_anchor, wall_time)
        self.order = []
        self.packets = 0
        self.last_seen_wall = 0.0
        self.lpp_position = None

    def note_packet(self, ts_tag, seq, tx_anchor, wall):
        self.packets += 1
        self.last_seen_wall = wall

        # 时钟修正：用连续两包在同一锚点上的 (接收时刻, 发送时刻) 估计频率比
        if self.last_rx_tag is not None:
            cand = self.cc.calculate(ts_tag, self.last_rx_tag, tx_anchor, self.last_tx_anchor)
            if cand > 0:
                self.cc.update(cand)
        self.last_rx_tag = ts_tag
        self.last_tx_anchor = tx_anchor

        # 历史包（供远端引用匹配）
        self.history[seq] = (ts_tag, tx_anchor, wall)
        self.order.append(seq)
        while len(self.order) > MAX_MATCH_PACKETS:
            old = self.order.pop(0)
            if old != seq:
                self.history.pop(old, None)

    def lookup(self, seq, now):
        """按序号取历史包；过老则丢弃（保证 TDoA 用同一批包的戳）"""
        rec = self.history.get(seq)
        if rec is None:
            return None
        if now - rec[2] > MAX_REFERENCE_AGE_S:
            return None
        return rec


# --------------------------------------------------------------------------
# 4. TDoA 计算（对照 tdoaEngine.c 的 calcTDoA）
#    An = 当前包的锚点，Ar = 包内引用的远端锚点
#    tdoa_ticks = (rxAn_by_T - rxAr_by_T) - (tof_Ar_to_An + (txAn - rxAr_by_An)) * clockCorrection
#    物理含义： |P - A_An| - |P - A_Ar|
# --------------------------------------------------------------------------
def compute_tdoa(states, src, pkt, ts_tag, now, stats):
    out = []
    an = states[src]
    cc = an.cc.value
    if cc <= 0.0 or not an.configured:
        return out

    for remote in pkt["remotes"]:
        if remote["distance"] is None:
            continue
        ar = states.get(remote["id"])
        if ar is None or not ar.configured:
            continue
        rec = ar.lookup(remote["seq"], now)
        if rec is None:
            continue

        rx_ar_by_t = rec[0]                       # 标签收到 Ar 那个包的时刻（标签时钟）
        rx_ar_by_an = remote["rx"]                # An 收到 Ar 那个包的时刻（An 时钟）
        tof_ticks = remote["distance"]            # An 测到的到 Ar 的飞行时间（tick）
        tx_an = pkt["tx"]                         # An 本包的发送时刻（An 时钟）

        delta_tx = tof_ticks + ((tx_an - rx_ar_by_an) & TS_MASK)   # An 时钟，tick
        tdoa_ticks = ((ts_tag - rx_ar_by_t) & TS_MASK) - delta_tx * cc
        tdoa_m = tdoa_ticks * M_PER_TICK

        # 物理约束：|d(P,An) - d(P,Ar)| 不可能超过两锚点间距
        d_an_ar = np.linalg.norm(np.asarray(an.position_hint) - np.asarray(ar.position_hint))
        if d_an_ar > 0 and abs(tdoa_m) > d_an_ar + 0.5:
            stats["bound_rejected"] += 1
            continue

        out.append((src, remote["id"], tdoa_m))
        stats["measurements"] += 1

    return out


# --------------------------------------------------------------------------
# 5. 位置解算：高斯-牛顿 + Huber 权重 + 粗差剔除
#    残差 r = (|P - A_i| - |P - A_j|) - tdoa_ij
# --------------------------------------------------------------------------
def residuals(p, meas, pos):
    r = np.empty(len(meas))
    for k, (i, j, d) in enumerate(meas):
        r[k] = (np.linalg.norm(p - pos[i]) - np.linalg.norm(p - pos[j])) - d
    return r


def jacobian(p, meas, pos, h=1e-3):
    J = np.empty((len(meas), 3))
    for axis in range(3):
        dp = np.zeros(3)
        dp[axis] = h
        J[:, axis] = (residuals(p + dp, meas, pos) - residuals(p - dp, meas, pos)) / (2 * h)
    return J


def solve_position(meas, pos, p0, iterations=25, fix_z=None, max_step=1.0):
    """高斯-牛顿 + Huber 鲁棒核 + 粗差剔除。

    fix_z   : 固定高度（米）→ 真正的二维解算（只优化 x/y）。
              锚点几乎共面时 z 方向是病态的，必须用这个（或改用 3D 布点）。
    max_step: 单次迭代步长上限（米）。防止病态几何/坏初值时跑到无穷远。
    """
    p = np.array(p0, dtype=float)
    if fix_z is not None:
        p[2] = float(fix_z)
    n_free = 2 if fix_z is not None else 3

    def resid_free(x):
        q = np.array([x[0], x[1], 0.0]) if fix_z is not None else x
        if fix_z is not None:
            q[2] = float(fix_z)
        return residuals(q, meas, pos)

    for _ in range(iterations):
        r = resid_free(p[:n_free] if fix_z is not None else p)
        # 数值雅可比（只对自由变量求导）
        J = np.empty((len(meas), n_free))
        for axis in range(n_free):
            dx_ = np.zeros(n_free)
            dx_[axis] = 1e-3
            xp = (p[:n_free] if fix_z is not None else p) + dx_
            xm = (p[:n_free] if fix_z is not None else p) - dx_
            J[:, axis] = (resid_free(xp) - resid_free(xm)) / 2e-3
        # Huber 权重，抑制个别坏测量把解拉飞
        a = np.abs(r)
        w = np.ones_like(a)
        mask = a > HUBER_DELTA_M
        w[mask] = HUBER_DELTA_M / a[mask]
        Jw = J * w[:, None]
        rw = r * w
        try:
            dx, *_ = np.linalg.lstsq(Jw, -rw, rcond=None)
        except np.linalg.LinAlgError:
            return None, None
        step = float(np.linalg.norm(dx))
        if step > max_step:                 # 限制步长，防止发散
            dx = dx * (max_step / step)
        if fix_z is not None:
            p[0] += dx[0]
            p[1] += dx[1]
            p[2] = float(fix_z)
        else:
            p = p + dx
        if step < 1e-4:
            break
    r = residuals(p, meas, pos)
    keep = np.abs(r) < OUTLIER_GATE_M
    if keep.sum() < 4:
        return None, None
    if keep.sum() != len(r):
        r2 = residuals(p, [m for m, k in zip(meas, keep) if k], pos)
        return p, float(np.sqrt(np.mean(r2 ** 2)))
    return p, float(np.sqrt(np.mean(r ** 2)))


def solve_2d_with_height_search(meas, pos, p0, z_min=0.0, z_max=2.5, z_step=0.05):
    """锚点接近共面时的稳妥做法：扫一遍高度，取残差最小的那个高度做二维解。

    返回 (p, rms, z_used)；失败返回 (None, None, None)。
    """
    best = (None, None, None)
    z = z_min
    while z <= z_max + 1e-9:
        p, rms = solve_position(meas, pos, p0, fix_z=z)
        if p is not None and (best[1] is None or rms < best[1]):
            best = (p.copy(), rms, z)
        z += z_step
    return best


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def load_anchors(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return {int(k): np.array([v["x"], v["y"], v["z"]], dtype=float)
            for k, v in doc["anchors"].items()}


def open_source(args):
    if args.input:
        return open(args.input, "rb"), None
    import serial
    ser = serial.Serial(args.port, 115200, timeout=0.05)
    ser.write(b"b")          # 把 sniffer 切到二进制输出模式
    ser.flush()
    return ser, ser


def main():
    ap = argparse.ArgumentParser(description="LPS TDoA3 主机侧解算（原型，未上机验证）")
    ap.add_argument("--port", help="Sniffer 节点的串口，例如 COM5 或 /dev/ttyACM0")
    ap.add_argument("--input", help="改为回放已录制的原始二进制流")
    ap.add_argument("--anchors", required=True, help="锚点坐标 YAML")
    ap.add_argument("--check", action="store_true", help="只做解码自检并打印统计")
    ap.add_argument("--json", action="store_true", help="以 JSON 行输出位置")
    ap.add_argument("--z", default=None,
                    help="固定高度（米）做二维定位；填 auto 则自动搜索最佳高度"
                         "（锚点接近共面时推荐，例如 --z auto）")
    ap.add_argument("--rate", type=float, default=10.0, help="位置输出频率上限 Hz（默认 10）")
    ap.add_argument("--sign", type=float, default=1.0, choices=[1.0, -1.0],
                    help="TDoA 符号约定翻转（若位置明显镜像，改成 -1 试试）")
    args = ap.parse_args()

    if not args.port and not args.input:
        ap.error("需要 --port 或 --input")

    anchor_pos = load_anchors(args.anchors)

    stream, ser = open_source(args)
    states = {}
    # position_hint 只用于物理约束检查（锚点坐标已知，直接赋值）
    for aid, p in anchor_pos.items():
        st = AnchorState(aid)
        st.configured = True
        st.position_hint = p
        states[aid] = st

    stats = {"frames": 0, "tdoa3": 0, "measurements": 0, "bound_rejected": 0}
    prev = dict(stats)
    pending = []
    pos = None
    auto_z = [None]          # --z auto 时缓存搜索到的高度
    last_out = None
    t_start = None
    last_report = None

    try:
        for ts_tag, src, dst, payload in read_frames(stream, is_file=bool(args.input)):
            # 统一用 sniffer 自带的 UWB 时间戳作时基：
            #   1) 实时与回放(文件)行为一致
            #   2) 避免"主机墙钟"和"UWB 时钟"两个时间域混用
            now = ts_tag / LOCODECK_TS_FREQ
            if t_start is None:
                t_start = now
                last_report = now
            stats["frames"] += 1
            pkt = decode_tdoa3(payload)
            if pkt is None:
                continue
            stats["tdoa3"] += 1

            if src not in states:
                st = AnchorState(src)
                states[src] = st
                if not args.check:
                    print(f"# 警告：收到未配置坐标的锚点 id={src}，已忽略", file=sys.stderr)

            state = states[src]
            state.note_packet(ts_tag, pkt["seq"], pkt["tx"], now)
            if pkt["lpp_position"]:
                state.lpp_position = pkt["lpp_position"]

            # 只处理配置了坐标的锚点
            if src in anchor_pos:
                pending.extend(compute_tdoa(states, src, pkt, ts_tag, now, stats))

            if args.check:
                if now - last_report > 2.0:
                    dt = now - last_report
                    d = {k: stats[k] - prev[k] for k in stats}
                    print(f"[check] 最近 {dt:5.1f}s：帧 {d['frames']} "
                          f"({d['frames'] / max(dt, 1e-6):.0f}/s)，TDoA3 包 {d['tdoa3']}，"
                          f"TDoA 测量 {d['measurements']}，物理约束剔除 {d['bound_rejected']}")
                    for aid in sorted(states):
                        st = states[aid]
                        print(f"        锚点 {aid:>3}: 累计 {st.packets:<7} 包  "
                              f"时钟修正 cc={st.cc.value:.9f}  "
                              f"{'已配置坐标' if st.configured else '未配置坐标(请检查 anchors.yaml)'}")
                    prev = dict(stats)
                    last_report = now
                continue

            # 解算：只在测量足够时做，且限制输出频率
            if len(pending) >= 4 and (last_out is None or (now - last_out) >= 1.0 / args.rate):
                # 去重：同一对锚点取最新
                latest = {}
                for (i, j, d) in pending:
                    latest[(i, j)] = d
                meas = [(i, j, args.sign * d) for (i, j), d in latest.items()]
                ids = sorted({i for i, _, _ in meas} | {j for _, j, _ in meas})
                if len(ids) >= 4:
                    if pos is None:
                        pos0 = np.mean([anchor_pos[a] for a in ids if a in anchor_pos], axis=0)
                        pos0[2] = pos0[2] * 0.5
                    else:
                        pos0 = pos
                    z_used = None
                    if args.z == "auto":
                        if auto_z[0] is None:                 # 只在第一帧搜索一次，之后复用
                            pz, rz, zbest = solve_2d_with_height_search(meas, anchor_pos, pos0)
                            if pz is not None:
                                auto_z[0] = zbest
                                print("# 自动搜索到最佳高度 z = %.2f m（后续沿用）" % zbest)
                        if auto_z[0] is not None:
                            z_used = auto_z[0]
                            p, rms = solve_position(meas, anchor_pos, pos0, fix_z=z_used)
                        else:
                            p, rms = None, None
                    elif args.z is not None:
                        z_used = float(args.z)
                        p, rms = solve_position(meas, anchor_pos, pos0, fix_z=z_used)
                    else:
                        p, rms = solve_position(meas, anchor_pos, pos0)
                    if p is not None:
                        pos = p
                        if args.json:
                            print(json.dumps({
                                "t": round(now - t_start, 4),
                                "x": round(float(p[0]), 4),
                                "y": round(float(p[1]), 4),
                                "z": round(float(p[2]), 4),
                                "rms": round(rms, 4) if rms else None,
                                "n": len(meas),
                            }), flush=True)
                        else:
                            print(f"[{now - t_start:7.2f}s] "
                                  f"pos=({p[0]:7.3f}, {p[1]:7.3f}, {p[2]:7.3f}) m  "
                                  f"meas={len(meas):3d}  rms={rms:.3f} m", flush=True)
                        last_out = now
                # 清理过老测量，避免列表无限增长
                pending = pending[-400:]

    except KeyboardInterrupt:
        pass
    finally:
        if ser is not None:
            ser.close()

    # 收尾总结（实时模式和回放模式都会打印）
    if t_start is not None:
        print("\n=== 结束统计 ===")
        print(f"总帧数 {stats['frames']}，其中 TDoA3 包 {stats['tdoa3']}，"
              f"生成 TDoA 测量 {stats['measurements']} 条，"
              f"被物理约束剔除 {stats['bound_rejected']} 条")
        for aid in sorted(states):
            st = states[aid]
            print(f"  锚点 {aid:>3}: 收包 {st.packets:<7} 时钟修正 {st.cc.value:.9f} "
                  f"{'已配置坐标' if st.configured else '未配置坐标'}")
        if pos is not None:
            print("最后一次解算位置: (%.3f, %.3f, %.3f) m" % (pos[0], pos[1], pos[2]))
        else:
            print("没有解算出位置。常见原因：")
            print("  - 测量不足（需要 >= 4 组 TDoA，即至少 4 个锚点且时钟修正已收敛）")
            print("  - anchors.yaml 里的 ID 与节点实际 ID 不一致")
            print("  - TDoA3 包里的 distance(TOF) 字段为空（锚点间还没建立测距）")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # 下游管道被关闭（例如 head / Select-Object -First），安静退出
        os._exit(0)
    except OSError as e:
        if getattr(e, "errno", None) == 22:      # Windows 上管道关闭的表现
            os._exit(0)
        raise
