#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lps_fit_anchors.py - recover the REAL anchor coordinates from known tag positions.

Model:  TDoA(i,j) = |P - A_i| - |P - A_j|
If the tag is captured at a few points with KNOWN coordinates, the anchor
geometry A can be solved for:
    unknowns: 3N-6 shape parameters (N=4 -> 6)
    each known point gives N-1 independent TDoA

Empirically validated (synthetic test, nominal 5 m square vs true 6.2 m square):
    3 points at the SAME height  -> residual 0.02 m but the shape is NOT pinned
                                   down (coordinates still off by ~0.9 m)
    4 points with TWO heights    -> residual 0.0000 m, coordinates recovered
                                   to 8 mm
    6 points, spread + heights   -> residual 0.0000 m, exact

=> use at least 4 known points, and make sure the tag height differs between at
least two of them (e.g. once on the floor, once on a table). More is better.

This does NOT need the (broken) anchor-to-anchor distance field and does NOT
need 6-8 anchors. Systematic biases such as antenna delay are absorbed into the
fitted coordinates, which is exactly what positioning cares about.

Usage:
    python tools\\lps_fit_anchors.py --anchors tools\\anchors.yaml \\
        --point 2.5,2.5,1.0=captures\\p1.norm.bin \\
        --point 0.5,4.5,1.0=captures\\p2.norm.bin \\
        --point 4.5,0.5,1.0=captures\\p3.norm.bin \\
        --out tools\\anchors_fitted.yaml
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import yaml

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import lps_tdoa3_solver as S          # noqa: E402


def parse_point(s):
    try:
        xyz, path = s.split("=")
        p = [float(v) for v in xyz.split(",")]
        assert len(p) == 3
        return np.array(p), Path(path)
    except Exception:
        print("!! --point 格式应为 x,y,z=抓包路径，收到：%s" % s)
        sys.exit(2)


def measure_from_capture(path, anchor_ids):
    """Median TDoA per anchor pair from one capture (anchor positions not used)."""
    states = {}
    for aid in anchor_ids:
        st = S.AnchorState(aid)
        st.configured = True
        st.position_hint = np.zeros(3)
        states[aid] = st
    latest = {}
    with open(path, "rb") as f:
        for ts, src, dst, payload in S.read_frames(f, is_file=True):
            pkt = S.decode_tdoa3(payload)
            if not pkt or src not in states:
                continue
            now = ts / S.LOCODECK_TS_FREQ
            states[src].note_packet(ts, pkt["seq"], pkt["tx"], now)
            cc = states[src].cc.value
            if cc <= 0:
                continue
            for r in pkt["remotes"]:
                if not r["distance"]:
                    continue
                ar = states.get(r["id"])
                if ar is None:
                    continue
                rec = ar.lookup(r["seq"], now)
                if rec is None:
                    continue
                delta_tx = r["distance"] + ((pkt["tx"] - r["rx"]) & S.TS_MASK)
                tdoa = ((ts - rec[0]) & S.TS_MASK) - delta_tx * cc
                d = tdoa * S.M_PER_TICK
                if abs(d) > 30.0:
                    continue
                latest.setdefault((src, r["id"]), []).append(d)
    return {k: float(np.median(v)) for k, v in latest.items() if len(v) >= 50}


def residuals_from_params(p, ids, meas_sets, nom_vec, lam):
    """p = 12 个锚点坐标（展平，顺序同 ids），直接在名义坐标系里拟合。

    由于 TDoA 对整体刚体变换不变，这里加一个很弱的正则项把解"拴"在名义布局附近
    （既消除刚体简并，也符合"名义坐标大致是对的、只差几十厘米到一米"的实际情况）。
    """
    nA = 3 * len(ids)
    A = {ids[k]: np.array(p[3 * k:3 * k + 3]) for k in range(len(ids))}
    out = []
    for k, item in enumerate(meas_sets):
        P, meas = item[0], item[1]
        if P is None:
            # 自由点：只知道高度 z（item[2]），平面位置 (x, y) 也在解里
            P = np.array([p[nA + 2 * k], p[nA + 2 * k + 1], float(item[2])])
        for (i, j), d in meas.items():
            out.append((np.linalg.norm(P - A[i]) - np.linalg.norm(P - A[j])) - d)
    out.extend(list(lam * (np.array(p[:nA]) - nom_vec)))
    for k, item in enumerate(meas_sets):
        if item[0] is None:
            # 自由点的弱先验：待在场地中心附近
            out.append(lam * (p[nA + 2 * k] - 2.5))
            out.append(lam * (p[nA + 2 * k + 1] - 2.5))
    return np.array(out)


def gn_fit(p0, ids, meas_sets, nom_vec, lam, iters=300):
    p = np.array(p0, dtype=float)
    for _ in range(iters):
        r = residuals_from_params(p, ids, meas_sets, nom_vec, lam)
        J = np.empty((len(r), len(p)))
        for k in range(len(p)):
            dp = np.zeros(len(p))
            dp[k] = 1e-3
            J[:, k] = (residuals_from_params(p + dp, ids, meas_sets, nom_vec, lam)
                       - residuals_from_params(p - dp, ids, meas_sets, nom_vec, lam)) / 2e-3
        try:
            dx, *_ = np.linalg.lstsq(J, -r, rcond=None)
        except np.linalg.LinAlgError:
            break
        n = float(np.linalg.norm(dx))
        if n > 1.0:
            dx *= 1.0 / n
        p = p + dx
        if n < 1e-7:
            break
    r = residuals_from_params(p, ids, meas_sets, nom_vec, lam)
    rT = r[:len(r) - len(p)]          # 只看测量残差
    return p, float(np.sqrt(np.mean(rT ** 2))), len(rT)


def kabsch_align(src, dst):
    pc, dc = src.mean(axis=0), dst.mean(axis=0)
    H = (src - pc).T @ (dst - dc)
    U, _s, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    return lambda X: (R @ X.T).T + (dc - R @ pc)


def main():
    ap = argparse.ArgumentParser(description="用已知标签位置反推锚点真实坐标")
    ap.add_argument("--anchors", required=True, help="名义锚点坐标 yaml（初值 + 对齐参考）")
    ap.add_argument("--point", action="append", required=True, metavar="x,y,z=cap.bin",
                    help="已知标签位置与该点抓包，可重复（至少 4 个，且高度要有差异）")
    ap.add_argument("--out", help="写出拟合后的锚点 yaml")
    args = ap.parse_args()

    nom = S.load_anchors(args.anchors)
    ids = sorted(nom)
    if len(ids) != 4:
        print("!! 本工具按 4 个锚点实现（当前 %d 个）" % len(ids))
        sys.exit(2)

    meas_sets = []
    print("=== 读入已知点数据 ===")
    for s in args.point:
        P, path = parse_point(s)
        if not path.exists():
            print("!! 抓包不存在：%s" % path)
            sys.exit(2)
        m = measure_from_capture(path, ids)
        print("   标签 (%5.2f, %5.2f, %5.2f)  %-28s → %d 个锚点对"
              % (P[0], P[1], P[2], path.name, len(m)))
        meas_sets.append((P, m))
    if len(meas_sets) < 3:
        print("!! 至少需要 3 个已知标签点；推荐 4~6 个，且至少两点高度不同")
    if len(meas_sets) >= 3:
        zs = {round(float(P[2]), 2) for P, _m in meas_sets}
        if len(zs) < 2:
            print("!! 警告：所有已知点高度相同（都在 z=%.2f），形状可能无法唯一确定——"
                  "建议其中至少一个点换个高度" % list(zs)[0])
    if len(meas_sets) < 3:
        sys.exit(2)

    p0 = np.concatenate([nom[i] for i in ids])      # 12 维：直接在名义坐标系里拟合
    lam = 0.05                                      # 很弱的正则，只用来消除刚体简并

    r0 = residuals_from_params(p0, ids, meas_sets, p0, lam)
    r0 = r0[:len(r0) - len(p0)]
    print("\n=== 名义坐标的残差 ===")
    print("   rms = %.3f m（%d 个方程）" % (float(np.sqrt(np.mean(r0 ** 2))), len(r0)))

    # 逐步放松正则（homotopy）：先靠名义坐标定住刚体姿态，再让数据主导
    p, rms, n = p0, None, 0
    for l in (0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002):
        p, rms, n = gn_fit(p, ids, meas_sets, p0, l, iters=200)
    print("\n=== 拟合后 ===")
    print("   rms = %.3f m（%d 个方程）" % (rms, n))

    fit_aligned = np.array([p[3 * k:3 * k + 3] for k in range(len(ids))])

    print("\n=== 锚点坐标（名义坐标系）===")
    print("   %-5s %-30s %s" % ("id", "拟合结果 (x, y, z)", "与名义的差"))
    out = {}
    for k, i in enumerate(ids):
        v = fit_aligned[k]
        out[i] = v
        print("   %-5d (%6.2f, %6.2f, %6.2f)        %s"
              % (i, v[0], v[1], v[2], np.round(v - nom[i], 2)))

    print("\n=== 锚点间距离：拟合 vs 名义（请和卷尺实测对照）===")
    for x in range(4):
        for y in range(x + 1, 4):
            i, j = ids[x], ids[y]
            df = float(np.linalg.norm(fit_aligned[x] - fit_aligned[y]))
            dn = float(np.linalg.norm(nom[i] - nom[j]))
            print("   %d-%d : 拟合 %5.2f m   名义 %5.2f m   差 %+5.2f m"
                  % (i, j, df, dn, df - dn))

    if args.out:
        doc = {"anchors": {int(i): {"x": float(v[0]), "y": float(v[1]), "z": float(v[2])}
                           for i, v in out.items()}}
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("# 由 lps_fit_anchors.py 用 %d 个已知标签点拟合\n" % len(meas_sets))
            fh.write("# 拟合残差 rms = %.3f m\n" % rms)
            yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True)
        print("\n--> 已写入 %s" % args.out)


if __name__ == "__main__":
    main()
