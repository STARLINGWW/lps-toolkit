#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_calibrate.py - automatically calibrate the RELATIVE positions of anchors.

Principle: every TDoA3 packet carries the time-of-flight each anchor measured to
its neighbours, so one capture gives a full anchor-to-anchor distance matrix.
Classical MDS turns that matrix into relative coordinates (up to a rigid
transform). Optional known coordinates for 2-3 anchors resolve rotation and the
mirror ambiguity (Kabsch alignment).

Usage:
    python tools\\lps_calibrate.py --input captures\\cap_xxx.norm.bin
    python tools\\lps_calibrate.py --port COM20 --seconds 30 --align 7=0,0,0 --out tools\\anchors_cal.yaml

Caveats: the scale is physical (metres), there is a mirror ambiguity unless you
align, and with only 4 anchors the 6 edges leave no redundancy to reject a bad
link. Uncalibrated antenna delays bias each edge; separating geometry from
per-node delay needs 6-8 anchors, so this tool does geometry only.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import yaml

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import lps_tdoa3_solver as S          # noqa: E402

M_PER_TICK = S.M_PER_TICK


def collect_distances(stream, is_file, max_packets=200000):
    """Extract anchor-to-anchor distances from TDoA3 packets: {(i, j): [m, ...]}"""
    pair = {}
    packets = 0
    with_dist = 0
    raw_min = None
    for ts, src, dst, payload in S.read_frames(stream, is_file=is_file):
        pkt = S.decode_tdoa3(payload)
        if not pkt:
            continue
        packets += 1
        for r in pkt["remotes"]:
            d = r.get("distance")
            if not d:
                continue
            with_dist += 1
            if raw_min is None or d < raw_min:
                raw_min = d
            pair.setdefault((src, r["id"]), []).append(float(d))
        if packets >= max_packets:
            break
    return pair, packets, with_dist, raw_min


def build_matrix(pair, min_samples=10):
    """Symmetric distance matrix + quality info per edge."""
    ids = sorted({a for k in pair for a in k})
    D = {}
    info = {}
    for i in ids:
        for j in ids:
            if i == j:
                D[(i, j)] = 0.0
                continue
            fwd = pair.get((i, j), [])
            rev = pair.get((j, i), [])
            vals = []
            if len(fwd) >= min_samples:
                vals.append(float(np.mean(fwd)))
            if len(rev) >= min_samples:
                vals.append(float(np.mean(rev)))
            if not vals:
                continue
            D[(i, j)] = float(np.mean(vals))
            both = (len(fwd) >= min_samples and len(rev) >= min_samples)
            allv = list(fwd) + list(rev)
            info[(min(i, j), max(i, j))] = {
                "d": D[(i, j)],
                "n_fwd": len(fwd),
                "n_rev": len(rev),
                "sd": float(np.std(allv)) if allv else 0.0,
                "asym": abs(float(np.mean(fwd)) - float(np.mean(rev))) if both else float("nan"),
            }
    return ids, D, info


def classical_mds(ids, D, dims=3):
    """Classical MDS -> relative coordinates (n x dims) and eigenvalues."""
    n = len(ids)
    M = np.zeros((n, n))
    for a, i in enumerate(ids):
        for b, j in enumerate(ids):
            if i == j:
                M[a, b] = 0.0
            elif (i, j) in D:
                M[a, b] = D[(i, j)]
            else:
                best = np.inf
                for c in range(n):
                    if c in (a, b):
                        continue
                    if (ids[a], ids[c]) in D and (ids[c], ids[b]) in D:
                        best = min(best, D[(ids[a], ids[c])] + D[(ids[c], ids[b])])
                M[a, b] = best if np.isfinite(best) else 0.0
    J = np.eye(n) - np.ones((n, n)) / n
    B = -0.5 * J @ (M ** 2) @ J
    w, V = np.linalg.eigh(B)
    order = np.argsort(w)[::-1]
    w = w[order]
    V = V[:, order]
    keep = min(dims, n - 1)
    X = V[:, :keep] * np.sqrt(np.clip(w[:keep], 0, None))
    if keep < dims:
        X = np.hstack([X, np.zeros((n, dims - keep))])
    return X, w


def align_rigid(X, ids, known):
    """Kabsch alignment (reflection allowed): relative -> known coordinates."""
    idx = [k for k, i in enumerate(ids) if i in known]
    if len(idx) < 2:
        return None, None, 0
    P = X[idx]
    Q = np.array([known[ids[k]] for k in idx], dtype=float)
    pc, qc = P.mean(axis=0), Q.mean(axis=0)
    H = (P - pc).T @ (Q - qc)
    U, _s, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    Xa = (R @ X.T).T + (qc - R @ pc)
    res = float(np.sqrt(np.mean(np.sum((Xa[idx] - Q) ** 2, axis=1))))
    return Xa, res, len(idx)


def parse_align(items):
    known = {}
    for it in items or []:
        try:
            k, v = it.split("=")
            xyz = [float(t) for t in v.split(",")]
            assert len(xyz) == 3
            known[int(k)] = xyz
        except Exception:
            print("!! --align 格式应为 7=0,0,0，收到：%s" % it)
            sys.exit(2)
    return known


def capture_live(port, seconds):
    """Reset the console, switch the sniffer to binary and capture into memory."""
    import serial
    import io
    import lps_config as C
    print("--> 打开 %s，复位控制台并切二进制，采集 %.0f 秒" % (port, seconds))
    ser = serial.Serial(port, 115200, timeout=0.05)
    time.sleep(0.3)
    try:
        C.reset_console(ser)
    except Exception:
        pass
    ser.reset_input_buffer()
    ser.write(b"b")
    ser.flush()
    time.sleep(0.3)
    buf = io.BytesIO()
    end = time.time() + seconds
    while time.time() < end:
        chunk = ser.read(8192)
        if chunk:
            buf.write(chunk)
        else:
            time.sleep(0.005)
    ser.close()
    buf.seek(0)
    return buf


def main():
    ap = argparse.ArgumentParser(description="自动标定锚点之间的相对位置（MDS）")
    ap.add_argument("--port", help="Sniffer 节点串口（现场采集）")
    ap.add_argument("--input", help="改用已有抓包文件（.bin / .norm.bin）")
    ap.add_argument("--seconds", type=float, default=30.0, help="现场采集时长（默认 30 秒）")
    ap.add_argument("--min-samples", type=int, default=10, help="每条边至少多少样本才算有效")
    ap.add_argument("--tof-offset", type=float, default=None,
                    help="distance 字段里的天线延迟常量（tick）。默认自动取观测到的最小值")
    ap.add_argument("--force", action="store_true",
                    help="即使判定测距数据不可用也继续算（仅调试用）")
    ap.add_argument("--align", action="append", metavar="ID=x,y,z",
                    help="用已知坐标锚点做刚体对齐，可重复（2~3 个即可）")
    ap.add_argument("--out", help="把标定结果写成 anchors yaml")
    args = ap.parse_args()
    known = parse_align(args.align)

    if args.input:
        path = Path(args.input)
        if not path.exists():
            print("!! 文件不存在：%s" % path)
            sys.exit(2)
        print("--> 读取抓包 %s" % path)
        with open(path, "rb") as f:
            pair_raw, packets, with_dist, raw_min = collect_distances(f, True)
    elif args.port:
        stream = capture_live(args.port, args.seconds)
        pair_raw, packets, with_dist, raw_min = collect_distances(stream, True)
    else:
        ap.error("需要 --port 或 --input")
        return

    print("    解析 TDoA3 包 %d 个，其中带锚点间测距 %d 条" % (packets, with_dist))
    if not pair_raw:
        print("!! 没有提取到锚点间测距。可能原因：")
        print("   - 锚点之间还没建立时钟修正（刚上电需等几十秒）")
        print("   - 锚点互相听不到（距离太远 / 遮挡）")
        sys.exit(3)

    # 这个字段 = 真实 TOF + 天线延迟常量(约 32951 ticks ≈ 154.6 m 当量)。
    # 先扣掉地板值（观测到的最小值），剩下的才是可用信息。
    offset = args.tof_offset if args.tof_offset is not None else raw_min
    print("    原始字段范围: %d ~ %d ticks；扣除常量偏移 %d ticks（约 %.1f m 当量）"
          % (raw_min, max(max(v) for v in pair_raw.values()), offset,
             offset * M_PER_TICK))
    pair = {k: [d * M_PER_TICK for d in v] for k, v in pair_raw.items()}
    span = 0.0
    for k, v in pair.items():
        span = max(span, max(v) - min(v))
    allv = [d for v in pair.values() for d in v]
    spread = max(allv) - min(allv) if allv else 0.0
    print("    扣偏移后，各边数值范围: %.3f ~ %.3f m（跨度 %.3f m）"
          % (min(allv), max(allv), spread))
    if spread < 1.0:
        print()
        print("!! 【锚点间测距数据不可用】")
        print("   扣掉天线延迟常量后，各边长度的差异只有 %.2f m，而你四个锚点实际相距 5~7 m，" % spread)
        print("   说明锚点之间的两两测距在这个固件/环境下没有正常工作（数值被钳在 MIN_TOF 附近）。")
        print("   因此无法用这份数据做几何自动标定。")
        print()
        print("   可选路线：")
        print("   a) 继续用卷尺量坐标（本工具帮不上忙）；")
        print("   b) 用「已知点标定」：把 sniffer 放在 3~5 个已知坐标点采数据，")
        print("      反解每个锚点的等效偏置（不改固件、对现有硬件立刻生效）；")
        print("   c) 补到 6~8 个锚点后再做「几何 + 每节点延迟」联合标定（数学上才可分离）。")
        if not args.force:
            sys.exit(3)

    ids, D, info = build_matrix(pair, min_samples=args.min_samples)

    print("\n=== 锚点间实测距离（UWB 自测距）===")
    print("   %-8s %-9s %-9s %-9s %-9s %s"
          % ("边", "距离(m)", "样本i→j", "样本j→i", "标准差", "双向不一致"))
    for (i, j) in sorted(info):
        it = info[(i, j)]
        asym = "-" if np.isnan(it["asym"]) else "%.3f" % it["asym"]
        flag = "   <-- 双向差异大" if (not np.isnan(it["asym"]) and it["asym"] > 0.2) else ""
        print("   %-8s %-9.3f %-9d %-9d %-9.3f %s%s"
              % ("%d-%d" % (i, j), it["d"], it["n_fwd"], it["n_rev"], it["sd"], asym, flag))

    if len(ids) < 4:
        print("\n!! 只有 %d 个锚点有互相测距，不足以标定三维几何" % len(ids))
        sys.exit(3)

    X, w = classical_mds(ids, D)
    print("\n=== MDS 相对几何（未对齐，仅相对形状）===")
    for k, i in enumerate(ids):
        print("   anchor %-3d : %8.3f %8.3f %8.3f" % (i, X[k, 0], X[k, 1], X[k, 2]))
    wpos = np.clip(w[:4], 0, None)
    print("   特征值(前4)：%s" % np.round(wpos, 3))
    if wpos[2] > 1e-9:
        ratio = wpos[2] / max(wpos[0], 1e-9)
        print("   三维性指标 λ3/λ1 = %.3f  %s"
              % (ratio, "（偏共面！z 方向不稳，建议架高部分锚点）"
                 if ratio < 0.05 else "（几何良好）"))

    resid = []
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            if (ids[a], ids[b]) in D:
                resid.append(np.linalg.norm(X[a] - X[b]) - D[(ids[a], ids[b])])
    print("   由坐标反算边长的残差 rms = %.3f m（越小说明距离矩阵越自洽）"
          % float(np.sqrt(np.mean(np.array(resid) ** 2))))

    out_xyz = {i: X[k] for k, i in enumerate(ids)}
    if known:
        Xa, aln_rms, n_used = align_rigid(X, ids, known)
        if Xa is None:
            print("\n!! 已知锚点少于 2 个，无法对齐，下面输出相对几何")
        else:
            print("\n=== 对齐到已知坐标（用了 %d 个锚点，对齐残差 rms = %.3f m）==="
                  % (n_used, aln_rms))
            out_xyz = {i: Xa[k] for k, i in enumerate(ids)}
            for k, i in enumerate(ids):
                mark = "  (对齐用)" if i in known else ""
                print("   anchor %-3d : %8.3f %8.3f %8.3f%s"
                      % (i, Xa[k, 0], Xa[k, 1], Xa[k, 2], mark))

    print("\n=== 标定结果 ===")
    print("anchors:")
    for i in sorted(out_xyz):
        x, y, z = out_xyz[i]
        print("  %d: {x: %.3f, y: %.3f, z: %.3f}" % (i, x, y, z))

    if args.out:
        doc = {"anchors": {int(i): {"x": float(v[0]), "y": float(v[1]), "z": float(v[2])}
                           for i, v in out_xyz.items()}}
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("# 由 lps_calibrate.py 自动标定（MDS + 刚体对齐） %s\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
            if known:
                f.write("# 对齐用已知锚点: %s\n" % known)
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=True)
        print("\n--> 已写入 %s，可直接给 lps_tdoa3_solver.py --anchors 使用" % args.out)


if __name__ == "__main__":
    main()
