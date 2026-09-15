# UWB TDoA Kit for Bitcraze Loco Positioning Nodes

**English** | [中文说明](#中文说明)

A self-contained toolkit to **flash, configure and put Bitcraze Loco Positioning
Nodes (LPS Node) into service as a TDoA positioning system** — without a
Crazyflie, without a Loco Positioning Deck, and without patching any firmware.

```
 ┌───────────────────────────┐
 │  Anchor nodes (TDoA3)     │  fix in place, measure each other,
 │  Node 0 .. Node 7         │  broadcast timestamps in every packet
 └─────────────┬─────────────┘
               │  UWB (Ch.2 / 6.8 Mbps / IEEE 802.15.4)
               ▼
 ┌───────────────────────────┐
 │  Sniffer node (passive)   │  never transmits; DW1000 hardware
 │  listens to everything    │  timestamps every received packet
 └─────────────┬─────────────┘
               │  USB CDC, binary frames
               ▼
 ┌───────────────────────────────────────────────┐
 │  Host (PC / Raspberry Pi)                     │
 │  decode → clock correction → TDoA → position  │
 └───────────────────────────────────────────────┘
```

## Quick start

```powershell
git clone https://github.com/STARLINGWW/uwb-tdoa-lps.git
cd uwb-tdoa-lps
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

`bootstrap.ps1` clones the required Bitcraze repositories into `clones\`,
installs the Python dependencies, and makes sure the firmware image is present.
It auto-detects a local HTTP proxy (common in mainland China) and falls back to a
GitHub mirror.

## No hardware required: offline self-test

```powershell
python tools\make_fake_capture.py
python tools\lps_tdoa3_solver.py --input captures\fake_tdoa3.bin --anchors captures\fake_anchors.yaml
```

Expected: position `(2.700, 2.199, 0.998)` against a ground truth of
`(2.700, 2.200, 1.000)` — sub-centimetre accuracy. This exercises the whole
software chain (frame parsing, TDoA3 decoding, clock correction, TDoA,
multilateration) before any radio is involved.

## The interactive wizard (recommended entry point)

```powershell
python tools\lps_wizard.py
```

It scans for COM ports and DFU devices, then offers a menu: flash as **anchor**,
flash as **data-output node**, configure only, inspect, diagnose, or recover a
stuck DFU device. **It returns to the menu after every step**, so you can walk
through a whole batch of nodes without typing any parameters.

## Three kinds of commands

**Flash** (put the node in DFU mode first: hold the `BM & DFU` button while
plugging in USB):

```powershell
python tools\lps_provision.py --anchor 2      # flash + set ID 2 + TDoA Anchor V3
python tools\lps_provision.py --tag 20        # flash + set ID 20 + Sniffer
python tools\lps_provision.py --anchors 0-7   # interactive batch, IDs 0..7
python tools\lps_flash.py --no-enter          # firmware only
```

**Configure** (takes effect after a reset — press the `RESET` button or replug):

```powershell
python tools\lps_config.py --port COM20 --id 2 --mode tdoa3
python tools\lps_config.py --port COM20 --read
```

**Inspect**:

```powershell
python tools\lps_config.py --list                        # node COM ports
python tools\lps_flash.py --list                         # DFU device
python tools\lps_capture.py --port COM20 --seconds 30 --summary
python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml
```

Full command reference: [`docs/命令速查.md`](docs/命令速查.md).

## Hardware notes

| Item | Detail |
|---|---|
| MCU / radio | STM32F072 + Decawave DWM1000 |
| Buttons | **`BM & DFU`** (hold while plugging USB → DFU mode), **`RESET`** (apply config) |
| LEDs | `POWER`, `RANGING`, `SYNC`, `MODE`, `TX`, `RX`, `SFD`, `RXOK`. `MODE` lights steadily for Anchor, blinks for Sniffer, is off for Tag |
| Power | micro-USB, DC jack or 5–12 V terminal. Powering the node separately makes flashing more reliable |
| Firmware | `firmware\lps-node-firmware-2022.09.dfu` (LGPL-3.0, from Bitcraze releases) |

### Windows driver caveat (read this before flashing)

Crazyflie, Crazyradio and LPS Node all share the same USB VID:PID (`0483:5740`).
If you previously installed the libusb driver with Zadig, Windows puts the LPS
Node under *libusb-win32 devices* and **never creates a COM port** — so you can
neither read nor flash it.

Fix it **one node at a time** (recommended, leaves the Crazyradio untouched):

> Device Manager → `libusb-win32 devices` → `Crazyflie 2.x (Interface 0)` →
> Update driver → Browse my computer → **Let me pick from a list** →
> **USB Serial Device** → Next → replug.

Automated inspection: `powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1`
(read-only by default). Step-by-step screenshots: [`docs/操作流程.md`](docs/操作流程.md).

### While flashing

- Do **not** unplug USB, press `RESET`, or open the official GUI (`lpstool.exe`) — it polls the device every 100 ms and will fight for it.
- Default timing is the official one (honouring the STM32 5 s poll timeout): **about 11 minutes per node**. This is the stable choice; compressing the wait causes mid-flash failures.
- An interrupted flash **cannot brick the node** — the DFU bootloader lives in ROM. Recover with: unplug → hold `BM & DFU` → plug in → release.

## Repository layout

```
├─ bootstrap.ps1             one-shot setup: clone deps, install, fetch firmware
├─ README.md                 this file (English) + 中文说明
├─ clones\                   third-party repos (pulled by bootstrap, git-ignored)
├─ tools\                    all scripts
│  ├─ lps_wizard.py          interactive wizard (recommended)
│  ├─ lps_provision.py       flash + configure in one command
│  ├─ lps_flash.py           firmware flashing only
│  ├─ lps_config.py          ID / mode / radio / power
│  ├─ lps_capture.py         capture raw sniffer data + per-anchor statistics
│  ├─ lps_tdoa3_solver.py    host-side TDoA3 solver → 3D position
│  ├─ make_fake_capture.py   generate a fake capture (offline self-test)
│  ├─ test_solver_sim.py     algorithm simulation
│  ├─ fix_serial_driver.ps1  diagnose/fix the libusb vs COM port conflict
│  └─ git_setup.ps1          initialise git and push to GitHub
├─ docs\                     manuals and flow charts (Chinese)
├─ firmware\                 2022.09 .dfu image
└─ captures\, logs\          runtime data (git-ignored)
```

## Documentation

| Document | Content |
|---|---|
| [`docs/操作流程.md`](docs/操作流程.md) | **Start here** — hardware → driver → flash → configure, with flow charts and screenshots |
| [`docs/刷写与配置SOP.md`](docs/刷写与配置SOP.md) | Step-by-step flashing SOP, DFU state table, troubleshooting |
| [`docs/命令速查.md`](docs/命令速查.md) | Command cheat sheet (flash / configure / inspect) |
| [`docs/LPS-TDoA-完整部署操作手册.md`](docs/LPS-TDoA-完整部署操作手册.md) | End-to-end deployment manual |
| [`docs/bitcraze-tdoa-原理与移植指南.md`](docs/bitcraze-tdoa-原理与移植指南.md) | How Bitcraze TDoA works, source map, porting notes |
| [`docs/uwb-tdoa-opensource-2026-09.md`](docs/uwb-tdoa-opensource-2026-09.md) | Survey of open-source TDoA UWB projects |

## Licensing

`tools\` scripts are provided as-is. The TDoA and clock-correction algorithms
are ported from Bitcraze firmware (`crazyflie-firmware`, `lps-node-firmware`),
which are GPL-3.0 / LGPL-3.0 — evaluate the licence impact before commercial use.
Everything under `clones\` remains under its original licence.

---

# 中文说明

一套**自包含的工具链**，用于刷写、配置 Bitcraze Loco Positioning Node（LPS Node），
并把它们组成一套 TDoA 定位系统 —— **不需要 Crazyflie、不需要 Loco Deck、不需要改固件**。

```
 锚点节点（TDoA3）：固定不动，互相测距，在每个包里广播时间戳
        │  UWB（信道 2 / 6.8Mbps / IEEE 802.15.4）
        ▼
 Sniffer 节点（纯被动）：从不发射，DW1000 硬件给每个收到的包打时间戳
        │  USB CDC 二进制帧
        ▼
 主机（PC / 树莓派）：解析 → 时钟修正 → TDoA → 三维位置
```

## 快速开始

```powershell
git clone https://github.com/STARLINGWW/uwb-tdoa-lps.git
cd uwb-tdoa-lps
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

`bootstrap.ps1` 会把需要的 Bitcraze 仓库 clone 到 `clones\`、安装 Python 依赖、
准备好固件；会自动探测本地 HTTP 代理，探测不到就回退到 GitHub 镜像。

## 没有硬件也能先自检

```powershell
python tools\make_fake_capture.py
python tools\lps_tdoa3_solver.py --input captures\fake_tdoa3.bin --anchors captures\fake_anchors.yaml
```

预期输出位置 `(2.700, 2.199, 0.998)`，真值 `(2.700, 2.200, 1.000)`，误差在毫米级。
这一步先把整条软件链路跑通（帧解析 → TDoA3 解包 → 时钟修正 → TDoA → 多站定位），再上硬件。

## 对话式向导（推荐入口）

```powershell
python tools\lps_wizard.py
```

自动扫描 COM 口与 DFU 设备，然后给你菜单：刷成基站 / 刷成数据出口 / 只配置 /
查看 / 诊断 / 恢复卡住的 DFU。**每做完一步都回到菜单**，所以整批节点可以一路点下去，
不用记参数、不用手打 COM 号。

## 三类命令

**刷写**（先把节点置于 DFU：按住 `BM & DFU` 键再插 USB）：

```powershell
python tools\lps_provision.py --anchor 2      # 刷 + 编号 2 + TDoA Anchor V3
python tools\lps_provision.py --tag 20        # 刷 + 编号 20 + Sniffer
python tools\lps_provision.py --anchors 0-7   # 交互式批量，编号 0~7
python tools\lps_flash.py --no-enter          # 只刷固件
```

**配置**（改完按 `RESET` 键或拔插 USB 才生效）：

```powershell
python tools\lps_config.py --port COM20 --id 2 --mode tdoa3
python tools\lps_config.py --port COM20 --read
```

**查看**：

```powershell
python tools\lps_config.py --list                        # 在线节点与 COM 口
python tools\lps_flash.py --list                         # DFU 设备
python tools\lps_capture.py --port COM20 --seconds 30 --summary
python tools\lps_tdoa3_solver.py --port COM20 --anchors tools\anchors_example.yaml
```

完整命令表见 [`docs/命令速查.md`](docs/命令速查.md)。

## 硬件要点

| 项目 | 说明 |
|---|---|
| MCU / 射频 | STM32F072 + Decawave DWM1000 |
| 按键 | **`BM & DFU`**（按住再插 USB → 刷机模式）、**`RESET`**（让配置生效） |
| LED | `POWER` `RANGING` `SYNC` `MODE` `TX` `RX` `SFD` `RXOK`；`MODE` 常亮=基站、闪烁=Sniffer、熄灭=Tag |
| 供电 | micro-USB / DC 座 / 5–12V 端子。**给节点单独供电会让刷写更稳** |
| 固件 | `firmware\lps-node-firmware-2022.09.dfu`（LGPL-3.0，来自 Bitcraze 官方发布） |

### Windows 驱动坑（刷写前必读）

Crazyflie、Crazyradio、LPS Node 的 USB VID:PID 完全相同（`0483:5740`）。
如果你以前用 Zadig 装过 libusb 驱动，LPS Node 会被归到 *libusb-win32 devices* 下，
Windows **不会创建 COM 口** —— 于是既读不到也刷不了。

解决方式：**逐节点修改**（推荐，不影响 Crazyradio）：

> 设备管理器 → `libusb-win32 devices` → `Crazyflie 2.x (Interface 0)` →
> 更新驱动程序 → 浏览我的电脑 → **让我从列表中选取** → **USB 串行设备** → 下一步 → 拔插 USB

自动检查：`powershell -ExecutionPolicy Bypass -File .\tools\fix_serial_driver.ps1`（默认只读）。
带截图的完整步骤见 [`docs/操作流程.md`](docs/操作流程.md)。

### 刷写期间

- **不要**拔 USB、不要按 `RESET`、不要打开官方 GUI（它每 100ms 抢一次设备）
- 默认官方原速（严格按 STM32 上报的 5 秒等待）：**一块约 11 分钟**，这是最稳的选择；压缩等待会在写入中途失败
- 刷写中断**不会变砖**（DFU 引导在 ROM 里）。恢复：拔掉 USB → 按住 `BM & DFU` → 插 USB → 松手

## 目录结构

```
├─ bootstrap.ps1             一键初始化：clone 依赖 + 装依赖 + 备好固件
├─ README.md                 本文件（英文 + 中文）
├─ clones\                   第三方仓库（bootstrap 拉取，已 gitignore）
├─ tools\                    全部脚本
│  ├─ lps_wizard.py          对话式向导（推荐入口）
│  ├─ lps_provision.py       一条命令完成 刷写 + 配置
│  ├─ lps_flash.py           只刷固件
│  ├─ lps_config.py          编号 / 模式 / 射频 / 功率
│  ├─ lps_capture.py         抓原始数据并统计各基站收包
│  ├─ lps_tdoa3_solver.py    主机侧 TDoA3 解算 → 三维位置
│  ├─ make_fake_capture.py   生成假抓包（无硬件自检）
│  ├─ test_solver_sim.py     算法仿真
│  ├─ fix_serial_driver.ps1  诊断/修复 libusb 与 COM 口冲突
│  └─ git_setup.ps1          初始化 git 并推送 GitHub
├─ docs\                     文档与流程图
├─ firmware\                 2022.09 固件
└─ captures\、logs\          运行数据（已 gitignore）
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/操作流程.md`](docs/操作流程.md) | **从这里开始** —— 硬件 → 驱动 → 刷写 → 配置，含流程图与截图 |
| [`docs/刷写与配置SOP.md`](docs/刷写与配置SOP.md) | 刷写标准流程、DFU 状态速查、故障处理 |
| [`docs/命令速查.md`](docs/命令速查.md) | 刷写 / 配置 / 查看 三类命令速查 |
| [`docs/LPS-TDoA-完整部署操作手册.md`](docs/LPS-TDoA-完整部署操作手册.md) | 端到端部署手册 |
| [`docs/bitcraze-tdoa-原理与移植指南.md`](docs/bitcraze-tdoa-原理与移植指南.md) | Bitcraze TDoA 原理、源码地图、移植说明 |
| [`docs/uwb-tdoa-opensource-2026-09.md`](docs/uwb-tdoa-opensource-2026-09.md) | 开源 TDoA UWB 方案调研 |

## 许可

`tools\` 下的脚本按原样提供。其中 TDoA 与时钟修正算法移植自 Bitcraze 固件
（`crazyflie-firmware`、`lps-node-firmware`，GPL-3.0 / LGPL-3.0），
**商用前请评估许可影响**。`clones\` 下的代码遵循各自原始许可。
