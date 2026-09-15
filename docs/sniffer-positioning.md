# Positioning with a sniffer node (LPS Toolkit)

**English** | [中文说明](#中文说明)

How to turn a set of LPS Nodes into a working indoor positioning system, using
one node as a **passive sniffer** and a host computer to compute the position.

## 1. How it works

```
  anchors (TDoA Anchor V3)                sniffer node (Sniffer)         host
 ┌───────────────────────┐              ┌────────────────────┐      ┌──────────────────┐
 │ node 0 … node N       │  UWB packets │ passively listens  │ USB  │ decode           │
 │ • range each other    │ ───────────► │ DW1000 timestamps  │ ───► │ clock correction │
 │ • broadcast their own │              │ every packet       │      │ TDoA → position  │
 │   rx timestamps + TOF │              │ (never transmits)  │      │ JSON / MAVLink   │
 └───────────────────────┘              └────────────────────┘      └──────────────────┘
```

* The **anchors** perform two-way ranging with each other and embed, in every
  packet they send, the receive timestamps and time-of-flight they measured for
  their neighbours.
* The **sniffer** only listens. It never transmits, so it cannot disturb the
  system, and the number of listeners is unlimited.
* The **host** reconstructs the TDoA values (it needs the sniffer's own hardware
  receive timestamps plus the data carried inside the packets), compensates the
  clock drift of every anchor, and solves for the 3D position.

Because the sniffer's timestamps come from the DW1000 hardware, the host does not
have to be a real-time system: a Raspberry Pi or a laptop is enough.

## 2. What you need

| Item | Notes |
|---|---|
| 4–8 anchor nodes | TDoA Anchor V3; 8 nodes placed as a box gives the best geometry |
| 1 sniffer node | Sniffer mode; stays connected to the host by USB |
| host | PC / Raspberry Pi / companion computer, with this toolkit installed |
| firmware | `firmware/lps-node-firmware-2022.09.dfu` (same image for all nodes) |

## 3. Step by step

**Step 1 — set the roles** (a few seconds per node, no flashing needed if the
firmware is already usable):

```powershell
python tools\lps_wizard.py
#   [2] Configure only  →  mode [1] anchor (TDoA Anchor V3)  →  ID 0,1,2,…
#   [2] Configure only  →  mode [0] tag/data output          →  ID 20   (the sniffer)
```

Press `RESET` on each node afterwards (or replug) so the new configuration is
applied. Check with `python tools\lps_config.py --list` and option `[3]`.

**Step 2 — place the anchors and measure their coordinates.** Keep them ≥15 cm
away from walls/ceiling, ≥2 m apart, at different heights; use a right-handed
coordinate system with a room corner as origin. Put the numbers into a copy of
`tools\anchors_example.yaml`.

**Step 3 — verify the radio link** (this is the acceptance gate):

```powershell
python tools\lps_capture.py --port COM20 --seconds 30 --summary
```

Expect every anchor to appear with a stable packet rate, `TDoA3` packets to
dominate, and **at least 4 anchors** visible. Also cross-check the anchor-to-anchor
distances against a tape measure:

```powershell
cd clones\lps-node-firmware
python tools\sniffer\sniffer_binary.py COM20 yaml | python tools\sniffer\tdoa3_decoder.py | python tools\sniffer\tdoa3_tof.py m
```

**Step 4 — solve the position:**

```powershell
python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml
#   [   3.42s] pos=(  1.234,  -0.567,   0.891) m  meas= 14  rms=0.043 m

python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml --check   # clock correction view
python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml --json    # machine-readable
```

**Step 5 — accept the system.** Put the sniffer at a known point: expect
< 20 cm error, and < 5 cm standard deviation over one minute at a fixed spot.

**Step 6 — feed a flight controller or ROS.** The `--json` lines are one position
per line; bridge them to MAVLink `VISION_POSITION_ESTIMATE` (PX4: enable
`EKF2_EV_CTRL`; ArduPilot: `EK3_SRC1_POSXY=6`). Mind the frame conversion
(right-handed LPS frame → NED) and the timestamps.

## 4. What the numbers mean

| Field | Meaning | Healthy value |
|---|---|---|
| `meas` | number of TDoA measurements used in the solve | ≥ 4 anchors, ideally 20+ |
| `rms` | residual of the least-squares solve | < 0.05 m |
| `cc` (in `--check`) | clock ratio of tag clock vs anchor clock | within ±1e-5 of 1.0 |
| `last_seen` | time since the last packet from an anchor | < 0.1 s |

## 5. Limits and the next step

* Keep the sniffer inside the anchor hull — TDoA geometry degrades quickly outside it.
* Clock correction needs a few tens of seconds to converge after start-up.
* The sniffer is tethered to the host by USB. If the moving platform must be
  self-contained, replace it later with a custom tag running the same TDoA3
  algorithm on a DW1000 (see `docs/bitcraze-tdoa-原理与移植指南.md`).

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Capture gets no frames | node really in Sniffer mode? anchors powered? same bitrate/preamble on all nodes? |
| Capture shows `0 帧` while the file keeps growing | the node was still printing the **text** format (usually because the console was parked in a sub-menu and the `b` key was swallowed). Current versions reset the console before switching and parse **both** formats, writing a normalised `*.norm.bin` for replay — use that file with `--input` |
| Fewer than 4 anchors visible | move anchors closer / raise them / check line of sight |
| No position printed | < 4 measurements; ID mismatch with `anchors.yaml`; `distance` field empty (anchor-to-anchor ranging not established yet) |
| Position drifts over minutes | clock correction not converging — check `cc` in `--check` |
| Position offset by a constant | anchor coordinate measurement error; measure again or calibrate at a known point |
| Mirrored position | check the coordinate system handedness, or try `--sign -1` |

---

# 中文说明

怎样把一组 LPS Node 变成可用的室内定位系统：**一个节点当被动 Sniffer，主机负责解算**。

## 1. 原理

```
  锚点（TDoA Anchor V3）              Sniffer 节点（Sniffer）          主机
 ┌───────────────────────┐          ┌────────────────────┐      ┌──────────────────┐
 │ 节点 0 … 节点 N        │ UWB 报文 │ 纯被动收听          │ USB  │ 解析             │
 │ • 互相双向测距          │ ───────► │ DW1000 硬件打时间戳 │ ───► │ 时钟修正         │
 │ • 广播自己的接收时间戳  │          │ 从不发射            │      │ TDoA → 位置      │
 │   与相邻锚点间 TOF      │          │                    │      │ JSON / MAVLink   │
 └───────────────────────┘          └────────────────────┘      └──────────────────┘
```

* **锚点**之间互相做双向测距，并在每个包里带上"最近收到的邻居接收时间戳 + 锚点间飞行时间"
* **Sniffer** 只收听、从不发射——不会干扰系统，而且监听者数量不限
* **主机**用 Sniffer 自己的硬件接收时间戳 + 包内数据还原 TDoA，补偿各锚点时钟漂移，解出三维位置

因为时间戳来自 DW1000 硬件，主机不需要是实时系统，树莓派或笔记本就够。

## 2. 需要什么

| 项目 | 说明 |
|---|---|
| 4–8 个锚点节点 | TDoA Anchor V3，8 个摆成长方体几何最好 |
| 1 个 Sniffer 节点 | Sniffer 模式，用 USB 连着主机 |
| 主机 | PC / 树莓派 / 机载计算机，装好本工具 |
| 固件 | `firmware/lps-node-firmware-2022.09.dfu`（所有节点同一份） |

## 3. 步骤

**第 1 步 配置角色**（固件可用时每块只需几秒，不必刷固件）：

```powershell
python tools\lps_wizard.py
#   [2] 改配置  → 模式 [1] 基站 (TDoA Anchor V3)  → 编号 0,1,2,…
#   [2] 改配置  → 模式 [0] 标签 / 数据出口        → 编号 20  （这就是 Sniffer）
```

每块配置完按一下 `RESET`（或拔插 USB）生效。用 `python tools\lps_config.py --list` 和菜单 `[3]` 检查。

**第 2 步 布点并量坐标**：离墙/天花板 ≥15cm，间距 ≥2m，不同高度；右手系、角落做原点。
把坐标填进 `tools\anchors_example.yaml` 的副本。

**第 3 步 验证射频链路**（关键验收）：

```powershell
python tools\lps_capture.py --port COM20 --seconds 30 --summary
```

期望：每个锚点都出现且收包率稳定、`TDoA3` 包占多数、**可见锚点 ≥ 4 个**。
同时用卷尺核对锚点间距离：

```powershell
cd clones\lps-node-firmware
python tools\sniffer\sniffer_binary.py COM20 yaml | python tools\sniffer\tdoa3_decoder.py | python tools\sniffer\tdoa3_tof.py m
```

**第 4 步 解算位置**：

```powershell
python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml
#   [   3.42s] pos=(  1.234,  -0.567,   0.891) m  meas= 14  rms=0.043 m

python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml --check   # 看时钟修正
python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml --json    # 给程序用
```

**第 5 步 验收**：把 Sniffer 放在已知坐标点，误差应 < 20cm；固定位置 1 分钟标准差 < 5cm。

**第 6 步 接飞控 / ROS**：`--json` 每行一个位置，桥接成 MAVLink `VISION_POSITION_ESTIMATE`
（PX4 开 `EKF2_EV_CTRL`；ArduPilot 设 `EK3_SRC1_POSXY=6`）。注意坐标系转换（LPS 右手系 → NED）与时间戳。

## 4. 输出含义

| 字段 | 含义 | 健康值 |
|---|---|---|
| `meas` | 本次解算用到的 TDoA 数量 | ≥ 4 个锚点，理想 20+ |
| `rms` | 最小二乘残差 | < 0.05 m |
| `cc`（`--check`） | 标签时钟 / 锚点时钟 比值 | 偏离 1 不超过 ±1e-5 |
| `last_seen` | 距最近一次收到该锚点包的时间 | < 0.1 s |

## 5. 限制与下一步

* Sniffer 尽量待在锚点包络内——包络外 TDoA 几何精度下降很快
* 启动后时钟修正需要几十秒收敛
* Sniffer 通过 USB 连主机；若移动端必须自带算力，后续可换成自研 DW1000 标签跑同一套 TDoA3 算法
  （见 `docs/bitcraze-tdoa-原理与移植指南.md`）

## 6. 故障排查

| 现象 | 处理 |
|---|---|
| 抓不到包 | 节点是否真是 Sniffer 模式；锚点是否上电；所有节点比特率/前导码是否一致 |
| 文件在涨但显示 `0 帧` | 节点当时仍在输出**文本格式**（通常是控制台停在某个子菜单，`b` 被吃掉）。新版脚本会先复位控制台再切二进制，并且**两种格式都能解析**，同时写出 `*.norm.bin`——用这个文件传给 `--input` 即可 |
| 可见锚点少于 4 个 | 拉近/抬高锚点；检查遮挡 |
| 不出位置 | 测量不足 4 条；`anchors.yaml` 的 ID 与实际不符；`distance` 字段为空（锚点间测距尚未建立） |
| 位置随时间漂移 | 时钟修正未收敛，用 `--check` 看 `cc` |
| 位置整体偏移 | 锚点坐标量测误差；重新量或做已知点标定 |
| 位置镜像 | 检查坐标系手性，或试 `--sign -1` |
