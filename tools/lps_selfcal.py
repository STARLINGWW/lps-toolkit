#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_selfcal.py - self-calibrate anchor geometry from the anchor-to-anchor
distance field carried inside the TDoA3 packets (no tape measure, no tag).

Model
-----
    d_meas(i, j) = k * |A_i - A_j| + b_i + b_j

where A are the anchor coordinates, b_i the per-node offset (the firmware does
not compensate the antenna delay: `dwSetAntenaDelay(dwm, 0)`), and k a global
scale. The observed values sit ~154.6 m (33000 ticks) above the real distances,
which is exactly what the b_i terms absorb.

Degrees of freedom
------------------
    shape 3N-6  +  biases N-1  (+1 if k is fitted)  vs  N(N-1)/2 distances
    N=5 :  9 + 4 = 13   vs 10   -> not solvable
    N=6 : 12 + 5 = 17   vs 15   -> not solvable
    N=7 : 15 + 6 = 21   vs 21   -> exactly determined (no redundancy)
    N=8 : 18 + 7 = 25   vs 28   -> determined with redundancy  <- preferred

Usage:
    python tools\\lps_selfcal.py --anchors tools\\anchors.yaml --input captures\\cap_x.norm.bin
    python tools\\lps_selfcal.py --anchors tools\\anchors.yaml --port COM20 --seconds 30 --out tools\\anchors_cal.yaml
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import yaml

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import lps_tdoa3_solver as S          # noqa: E402

M_PER_TICK = S.M_PER_TICK


def collect(stream, is_file):
    """{(i, j): [raw distance field values]}"""
    pair = {}
    n = 0
    for ts, src, dst, payload in S.read_frames(stream, is_file=is_file):
        pkt = S.decode_tdoa3(payload)
        if not pkt:
            continue
        n += 1
        for r in pkt["remotes"]:
            d = r.get("distance")
            if d:
                pair.setdefault((src, r["id"]), []).append(float(d))
    return pair, n


def build(ids, pair, min_samples):
    D = {}
    for i in ids:
        for j in ids:
            if i == j:
                continue
            v = pair.get((i, j), [])
            if len(v) >= min_samples:
                D[(i, j)] = float(np.median(v))
    return D


def residuals(p, ids, D, nom_vec, lam, nb, k):
    """p = [anchor xyz (3N)] + [bias b_0..b_{N-1}]"""
    n = len(ids)
    A = {ids[i]: np.array(p[3 * i:3 * i + 3]) for i in range(n)}
    b = {ids[i]: p[3 * n + i] for i in range(n)}
    out = []
    for (i, j), d in D.items():
        out.append((k * np.linalg.norm(A[i] - A[j]) + b[i] + b[j]) - d)
    # 弱正则：几何贴近名义值、偏差均值贴近 0（同时消除刚体简并）
    out.extend(list(lam * (np.array(p[:3 * n]) - nom_vec)))
    out.append(lam * float(np.sum(list(b.values()))) * 0.1)
    return np.array(out)


def gn(p0, ids, D, nom_vec, lam, iters=400):
    p = np.array(p0, dtype=float)
    npar = len(p)
    for _ in range(iters):
        r = residuals(p, ids, D, nom_vec, lam, len(ids), 1.0)
        J = np.empty((len(r), npar))
        for k in range(npar):
            dp = np.zeros(npar)
            dp[k] = 1.0                      # 1 tick 的步长
            J[:, k] = (residuals(p + dp, ids, D, nom_vec, lam, len(ids), 1.0)
                       - residuals(p - dp, ids, D, nom_vec, lam, len(ids), 1.0)) / 2.0
        try:
            dx, *_ = np.linalg.lstsq(J, -r, rcond=None)
        except np.linalg.LinAlgError:
            break
        step = float(np.linalg.norm(dx))
        if step > 200.0:
            dx *= 200.0 / step
        p = p + dx
        if step < 1e-6:
            break
    r = residuals(p, ids, D, nom_vec, lam, len(ids), 1.0)
    r = r[:len(D)]
    return p, float(np.sqrt(np.mean(r ** 2))), len(D)


def main():
    ap = argparse.ArgumentParser(description="用锚点间测距自标定锚点几何")
    ap.add_argument("--anchors", required=True, help="名义锚点坐标 yaml")
    ap.add_argument("--input", help="抓包文件")
    ap.add_argument("--port", help="或现场采集")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--min-samples", type=int, default=50)
    ap.add_argument("--out", help="写出标定后的锚点 yaml")
    args = ap.parse_args()

    a = S.load_anchors(args.anchors)
    ids = sorted(a)
    n = len(ids)
    neq = n * (n - 1) // 2
    nunk = (3 * n - 6) + (n - 1)
    print("锚点 %d 个：方程 %d 条（边长），未知量 %d 个（形状 %d + 偏差 %d）→ %s"
          % (n, neq, nunk, 3 * n - 6, n - 1,
             "刚好可解（无冗余）" if neq == nunk else
             ("有冗余 ✓" if neq > nunk else "欠定 ✗ 解不出来")))
    if neq < nunk:
        print("!! 锚点太少，无法自标定（至少要 %d 个，推荐 %d 个）" % (7, 8))

    if args.input:
        with open(args.input, "rb") as f:
            pair, npk = collect(f, True)
    elif args.port:
        import serial
        import lps_config as C
        ser = serial.Serial(args.port, 115200, timeout=0.05)
        time.sleep(0.3)
        try:
            C.reset_console(ser)
        except Exception:
            pass
        ser.reset_input_buffer()
        ser.write(b"b")
        ser.flush()
        time.sleep(0.3)
        import io
        buf = io.BytesIO()
        end = time.time() + args.seconds
        while time.time() < end:
            c = ser.read(8192)
            if c:
                buf.write(c)
            else:
                time.sleep(0.005)
        ser.close()
        buf.seek(0)
        pair, npk = collect(buf, True)
    else:
        ap.error("需要 --input 或 --port")
        return

    D = build(ids, pair, args.min_samples)
    print("解析 TDoA3 包 %d 个，得到 %d 条有效边长" % (npk, len(D)))
    if len(D) < neq:
        print("!! 只有 %d 条边长（需要 %d 条）——检查是否所有锚点都能互相听到" % (len(D), neq))
        if len(D) < 10:
            sys.exit(3)

    nom_vec = np.concatenate([a[i] for i in ids])
    p0 = np.concatenate([nom_vec, np.zeros(n)])
    r0 = residuals(p0, ids, D, nom_vec, 1e9, n, 1.0)[:len(D)]
    print("\n名义坐标(偏差=0)的残差 rms = %.1f ticks = %.3f m"
          % (float(np.sqrt(np.mean(r0 ** 2))), float(np.sqrt(np.mean(r0 ** 2))) * M_PER_TICK))

    p, rms, m = p0, None, 0
    for lam in (1e6, 1e5, 1e4, 1e3, 1e2, 1e1, 1.0):
        p, rms, m = gn(p, ids, D, nom_vec, lam)
    print("拟合后残差 rms = %.1f ticks = %.3f m（%d 条方程）" % (rms, rms * M_PER_TICK, m))

    fit = np.array([p[3 * i:3 * i + 3] for i in range(n)])
    bias = p[3 * n:]
    print("\n=== 每个节点的延迟偏差（tick，1 tick=4.69mm）===")
    for i in range(n):
        print("   anchor %-3d : %+9.1f ticks = %+7.3f m" % (ids[i], bias[i], bias[i] * M_PER_TICK))

    print("\n=== 锚点坐标：拟合 vs 名义 ===")
    for i in range(n):
        print("   %-3d  拟合 (%6.2f, %6.2f, %6.2f)   名义 (%6.2f, %6.2f, %6.2f)   差 %s"
              % (ids[i], fit[i][0], fit[i][1], fit[i][2],
                 a[ids[i]][0], a[ids[i]][1], a[ids[i]][2], np.round(fit[i] - a[ids[i]], 2)))

    print("\n=== 锚点间距离：拟合 vs 名义（可与卷尺对照）===")
    for x in range(n):
        for y in range(x + 1, n):
            df = float(np.linalg.norm(fit[x] - fit[y]))
            dn = float(np.linalg.norm(a[ids[x]] - a[ids[y]]))
            flag = "   <-- 差得多" if abs(df - dn) > 0.5 else ""
            print("   %d-%-3d 拟合 %5.2f   名义 %5.2f   差 %+5.2f%s"
                  % (ids[x], ids[y], df, dn, df - dn, flag))

    if args.out:
        doc = {"anchors": {int(ids[i]): {"x": float(fit[i][0]), "y": float(fit[i][1]),
                                         "z": float(fit[i][2])} for i in range(n)}}
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("# 由 lps_selfcal.py 自标定（用锚点间测距字段）\n")
            fh.write("# 拟合残差 rms = %.1f ticks (%.3f m)\n" % (rms, rms * M_PER_TICK))
            yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True)
        print("\n--> 已写入 %s" % args.out)


if __name__ == "__main__":
    main()
