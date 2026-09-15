# Bitcraze LPS 的 TDoA 原理与「移植到自有平台」指南

> 依据：`bitcraze/lps-node-firmware`、`bitcraze/crazyflie-firmware` 源码 + Bitcraze 官方文档（2026-09 版本）

---

## 1. 核心机制：锚点互相测距，标签只做嗅探

这是理解 Bitcraze TDoA 的关键，它和大多数商用 TDOA 系统的实现思路一致，但**标签侧完全被动**：

```
锚点之间：互相做双向测距(TWR)，测量相互距离 TOF
          ↓
每个锚点在自己的广播包里塞进："我最近收到的其它锚点的 RX 时间戳 + 我测到的锚点间距离"
          ↓
标签(tag)：纯嗅探器，只收不发
          用「自己的本地接收时间戳」+「包里带来的锚点时间戳/距离」算出 TDoA
```

官方文档的说法很直白：*"One way of looking at the system is that the Anchors are doing Two Way Ranging with each other and that the possibility to calculate TDoA values for the Tags is merely a nice side effect."*

### TDoA 的计算公式

标签收到锚点 0 和锚点 1 的包，已知：

- `δrx`：两个包在**标签本地时钟**下的到达时间差（直接测量得到）
- `δtx`：两个包在**锚点 1 时钟**下的发送时间差
  - 锚点 1 在它自己的包里上报了"收到锚点 0 那个包的 RX 时间戳"
  - 再加上锚点 1 测到的"锚点 0 → 锚点 1 的飞行时间(TOF)"，就能反推锚点 0 的发送时刻
- `α₁`：标签时钟 vs 锚点 1 时钟的**频率比（时钟漂移系数）**，用锚点 1 连续两包估算：`α₁ = δrx₁ / δtx₁`

最终：

```
TDoA = δrx − (α₁ × δtx)
```

注意：**锚点之间不做时钟同步**，每个设备时钟都在漂，所谓"同步"是靠标签侧持续跟踪对方的时钟漂移系数来实现的（源码：`src/utils/src/clockCorrectionEngine.c`）。

### 时钟修正引擎（TDoA 能否工作的命门）

- 用连续两包估频率比（前提：两次测量间隔内标签与锚点距离近似不变，包率够高时误差可忽略）
- **离群值剔除**：偏离当前估计太远的值直接丢弃
- **低通滤波**平滑估计
- **leaky bucket 计数器**：连续太多离群 → 重置时钟修正机制
- 处理 32 位计数器回绕（wrapping）

### 四层错误处理

1. 时钟修正阶段的离群检测（可疑包的数据不进入系统）
2. 序号连续性检查（保证 TDoA 计算中各步骤用的是同一批包的戳）
3. 物理约束检查：TDoA 值不可能大于两锚点之间距离，超过即丢弃
4. 与当前估计位置的新息检验；若离群过多则自适应放宽门限，让滤波器重新收敛

---

## 2. TDoA2 vs TDoA3

| 维度 | TDoA 2 | TDoA 3 |
|---|---|---|
| 调度方式 | TDMA，8 个时隙 × 2 ms = **16 ms 帧**，锚点按地址轮转发送 | **随机发送**，容忍并处理碰撞 |
| 主从关系 | **锚点 0 是主时钟**，其余锚点同步到它 | 无主，所有锚点平等 |
| 单点故障 | **有**：锚点 0 挂了整个系统停摆；且所有锚点必须能听到锚点 0 | 无 |
| 锚点数量 | **最多 8 个**（地址 0–7），适合一个房间的盒状布点 | 不限（只要 ID 不同），可跨房间/跨楼层 |
| 标签数量 | 无限（标签只收） | 无限 |
| 精度 | 略优于 TDoA3（无碰撞丢包/时间戳退化） | 略低，但可用 Long Range 模式换距离 |
| 锚点位置来源 | 需在标签侧配置锚点地址；坐标由系统下发 | 锚点把自己的**绝对位置直接广播**在包里 |
| 包类型 | `type = 0x22` | `type = 0x30` |
| 官方建议 | 8 锚点放四角，标签尽量待在锚点包络内 | 任意点附近有 5–10 个锚点在范围内即可 |

---

## 3. 物理层与协议速查（自研标签必备）

来自 `lps-node-firmware/src/uwb.c` 与协议文档：

| 项目 | 值 |
|---|---|
| UWB 芯片 | **DW1000 / DWM1000**（LPS 是 DW1000 时代的产品，DW3000 **不兼容**其 PHY 配置） |
| 射频信道 | **Channel 2**（`dwSetChannel(dwm, CHANNEL_2)`） |
| 默认速率模式 | `MODE_SHORTDATA_FAST_ACCURACY`（短数据 / 6.8 Mbps / 快速高精度） |
| Long Range 模式 | 降比特率（`MODE_SHORTDATA_MID_ACCURACY`）或加长前导码 |
| 发射功率 | 默认开启 Smart Power；强行指定时为 `0x1F1F1F1F` |
| MAC 层 | **IEEE 802.15.4 帧**（`MAC802154_HEADER_LENGTH`） |
| PAN ID | **0xbccf** |
| 设备地址 | `0xbccf000000000000 \| ID`，锚点 ID = 地址低字节；**0xff 表示标签** |
| 加密 | 无，明文，可直接嗅探 |

### TDoA2 包结构（锚点每帧发一个）

```c
typedef struct rangePacket_s {
  uint8_t  type;           // 0x22
  uint8_t  seqs[8];        // seqs[i]: 本包序号(i==自己) 或 最近收到锚点 i 的包序号
  uint32_t timestamps[8];  // timestamps[i]: 本包发送时刻(i==自己) 或 收到锚点 i 的包的时刻（本单位时钟）
  uint16_t distances[8];   // distances[i]: 与锚点 i 的飞行时间 TOF（radio tick）
} __attribute__((packed)) rangePacket_t;
```

### TDoA3 包结构

```
+--------+----------------------------------+----------+
| Header | remoteCount x Remote anchor data | LPP data |
+--------+----------------------------------+----------+
Header:  type(0x30) | seq | txTimeStamp | remoteCount
Remote : id | hasDistance(1bit) | seq(7bit) | rxTimeStamp | distance(可选)
```

完整协议文档：
- `docs/functional-areas/tdoa_principles.md`（原理，含时序图）
- `docs/protocols/tdoa2_protocol.md` / `tdoa3_protocol.md`（包格式）
- `docs/functional-areas/tdoa3_implementation.md`（TDoA3 实现细节）

---

## 4. 代码地图（想改哪块看这里）

### 锚点侧：`bitcraze/lps-node-firmware`（LGPL-3.0）

| 文件 | 作用 |
|---|---|
| `src/main.c` | 主任务、串口菜单（改 ID / 模式 / 功率）、配置打印 |
| `src/uwb.c` | 算法注册表（5 种模式）、DW1000 初始化、中断回调分发、信道/速率/功率配置 |
| `src/uwb_tdoa_anchor2.c` | **TDoA2 锚点实现**（TDMA 时隙、包组装） |
| `src/uwb_tdoa_anchor3.c` | **TDoA3 锚点实现**（随机调度、邻居数据维护） |
| `src/uwb_twr_anchor.c` / `uwb_twr_tag.c` | TWR 锚点 / **TWR 标签**（标签侧直接 printf 输出距离） |
| `src/cfg.c`、`src/eeprom.c` | 节点配置（地址、模式、锚点坐标）存 EEPROM |
| `src/lpp.c` | LPP（Loco Positioning Protocol）处理 |
| `lib/libdw1000`（子模块） | DW1000 驱动，与硬件平台解耦 |
| `tools/lpp/*.py` | **通过 USB 配置节点的 Python 脚本**（设置锚点坐标、射频参数、功率） |
| `tools/`、`lps-tools` 仓库 | 固件烧写 + 模式/地址配置 GUI 工具 |

节点固件注册的算法只有这 5 种（原文见 `src/uwb.c` 的 `availableAlgorithms[]`）：

```
0: TWR Anchor
1: TWR Tag
2: Sniffer
3: TDoA Anchor V2
4: TDoA Anchor V3
```

### 标签侧：`bitcraze/crazyflie-firmware`（GPL-3.0）

| 文件 | 作用 |
|---|---|
| `src/deck/drivers/src/locodeck.c` | Loco Deck 驱动、DW1000 初始化、算法调度 |
| `src/deck/drivers/src/lpsTdoa2Tag.c` | **TDoA2 标签实现**（解析 0x22 包、维护锚点历史） |
| `src/deck/drivers/src/lpsTdoa3Tag.c` | **TDoA3 标签实现**（解析 0x30 包、随机锚点匹配） |
| `src/utils/src/tdoa/tdoaEngine.c` | **TDoA 引擎**：时钟修正、锚点匹配、几何过滤、生成 TDoA 量测 |
| `src/utils/src/tdoa/tdoaStorage.c` | 锚点数据动态存储（id 为 key，限 15–20 个可见锚点） |
| `src/utils/src/tdoa/tdoaStats.c` | 统计与日志（收包率、丢包、上下文命中率） |
| `src/utils/src/clockCorrectionEngine.c` | 时钟漂移估计（离群剔除 / 低通 / leaky bucket） |
| `src/modules/src/kalman_core/mm_tdoa.c`、`mm_tdoa_robust.c` | **把 TDoA 差值直接作为卡尔曼量测**的观测模型 |

> **重要认知**：Bitcraze 的实现**不是**"先解算出 XYZ 再输出"，而是把 TDoA 差值直接送进机载卡尔曼滤波器的量测模型（`mm_tdoa.c`），由 EKF 融合出位置/速度。这意味着你在自研平台上也可以复用同样的思路：只要拿到 TDoA，就可以喂给任意 EKF。

---

## 5. 能不能移植到别的开发板上？分角色看

### 5.1 锚点（Anchor）——难，不推荐

`lps-node-firmware` 针对 Loco Positioning Node 硬件（STM32F072 + nRF51 + DWM1000 + I²C EEPROM）。移植需要：

- 移植 HAL / FreeRTOS / SPI 时序
- 移植 `libdw1000` 驱动（这部分本身是平台无关的，相对好办）
- 移植 EEPROM 配置存储与配置菜单
- 复刻 nRF51 侧的无线配置通道（可选）
- **最难的是亚毫秒级 TDMA 时序精度**：TDoA2 时隙 2 ms，`uwb_tdoa_anchor2.c` 里对收发时序和中断延迟的假设是硬绑定的

目前**没有公开的成功移植案例**（社区里的第三方项目基本都只做标签侧）。

### 5.2 标签（Tag）——容易，推荐

三条理由让它比锚点简单得多：

1. **纯被动收听**：不需要发送、不需要参与 TDMA 调度，不用考虑碰撞避免
2. **时间戳由 DW1000 硬件打**：接收时刻的精度由射频芯片保证，MCU 不需要硬实时响应
3. 计算量小：时钟修正 + 一次 EKF 更新

**实证**：第三方项目 `dy-dx-1/DW1000-TDoA3` 用 **Python + `spidev`** 在 Linux 主板上直接驱动 DW1000，实现了 TDoA3 标签——说明标签侧甚至可以用树莓派这类非实时平台，在用户态完成。

### 5.3 Loco Positioning Node 能不能当 TDoA 标签？

**不能。** 节点固件只有 `TWR Tag` 模式，官方商店页面写得很明确：

> *"Supports Anchor and Sniffer mode, as well as a limited TWR Tag mode (no position estimation)"*

TWR 标签模式会通过 USB CDC 串口直接打印距离，例如：

```
distance 0:  1234mm
distance 1:  2456mm
```

这是一个**零改动**的数据出口，但局限是：TWR 模式下系统同时只能有一个标签，且节点不做位置解算。

### 5.4 结论

> **保留 Bitcraze 的锚点（作为基础设施），自己造标签。** 这是成本、难度、许可三方面最优的组合。锚点 $180/个（8 个约 $1440），标签只要有 DW1000 模块 + 一个带 SPI 的 MCU 即可。

---

## 6. 三条落地路线

### 路线 A：零改动先用起来（0.5–1 天）

Loco Positioning Node 刷成 `TWR Tag` 模式 → USB 接机载计算机 → 解析串口文本拿距离 → 自己在主机上做最小二乘/卡尔曼解算。

- 优点：立刻能拿到真实数据，用来验证锚点布置、量程、精度
- 缺点：单标签、TWR 速度慢、没有位置输出
- 适合：先摸清系统特性、写解算算法

### 路线 B：移植 TDoA 标签（1–3 周，推荐）

硬件：`DWM1000` / `BU01`（Ai-Thinker）/ 甚至拆 Bitcraze 的 Loco Deck（它本质就是 SPI 接口的 DWM1000 + 天线）接自己的 MCU。

两种实现基准：

1. **复用 Bitcraze C 代码**：`lpsTdoa2Tag.c` / `lpsTdoa3Tag.c` + `tdoaEngine.c` + `tdoaStorage.c` + `clockCorrectionEngine.c` + `libdw1000`。
   - 需要剥离 FreeRTOS、`param`/`log`、CRTP 依赖；保留 TDoA 计算逻辑
   - 许可：LGPL-3.0（`tdoaEngine` 等在 crazyflie 固件里是 GPL-3.0），**商用需评估**
2. **自己写解析层**：用 `thotro/arduino-dw1000`（Apache-2.0）或 `br101` 驱动拿到原始 RX 时间戳，按 `tdoa2_protocol.md` / `tdoa3_protocol.md` 解析 0x22 / 0x30 包，再移植时钟修正逻辑。
   - 参考：`dy-dx-1/DW1000-TDoA3`（Python/spidev 实现，可直接跑在树莓派上，**许可 GPL-3.0**）

### 路线 C：完全自研（1–2 个月）

协议文档已公开（原理、包格式、调度方式都写清楚了），可以做一套不依赖任何 Bitcraze 代码的实现。工作量主要在时钟修正的鲁棒性和多径处理上。

---

## 7. 接到自己的无人机上（PX4 / ArduPilot）

目前**没有**现成的 "LPS → PX4/ArduPilot" 开源桥接项目（已检索确认），需要自己搭，但很直接：

### 数据链路

```
DW1000 标签(自研 MCU 或 树莓派) 
   → UART / USB / UDP → 机载计算机(或飞控直连 UART)
   → 位置或 TDoA 量测 → 飞控 EKF
```

### 两种接入方式

| 方式 | 做法 | 评价 |
|---|---|---|
| 位置回灌（简单） | 标签解算出 XYZ → 通过 MAVLink `VISION_POSITION_ESTIMATE`（或 `GPS_INPUT` 伪装成 GPS）发给飞控 | 实现快；但外部解算 + 通信延迟会拖累控制带宽 |
| 量测回灌（推荐） | 标签只输出 TDoA 差值 → 通过自定义 MAVLink/ROS2 话题送进飞控，由飞控 EKF 融合 | 延迟低、鲁棒性好，和 Crazyflie 的做法一致（对应 `mm_tdoa.c`） |

### 关键配置

- **PX4**：`EKF2_EV_CTRL` 打开外部视觉/位置融合；坐标系要对齐（LPS 为右手系，需转换到 NED）；设置 `EKF2_EV_DELAY` 补偿延迟；室内需处理 HOME 点与解锁条件
- **ArduPilot**：`EK3_SRC1_POSXY = 6 (ExternalNav)`，同样用 `VISION_POSITION_ESTIMATE` 或 `GPS_INPUT`
- **ROS 2 中介**：`px4_ros_com`（micro XRCE-DDS）或 MAVROS；参考 `TIERS/active-passive-uwb-ros` 的 ROS 节点组织方式
- **坐标系对齐**：LPS 锚点坐标系 ↔ 机体系 ↔ NED；建议把锚点坐标按 NED 直接配置，避免运行时旋转误差
- **时间戳**：MAVLink 消息里的 `time_usec` 必须是同步过的时间基准，否则 EKF 会拒绝或抖动量测

---

## 8. 踩坑清单

1. **必须用 DW1000**：LPS 是 DW1000 的 PHY（Channel 2 / 6.8 Mbps / 短数据），DW3000 不能直接嗅探这些包
2. **锚点 ID 与地址**：UWB 地址 = `0xbccf0000000000XX`，XX 是锚点 ID；过滤包时按这个前缀 + PAN `0xbccf`
3. **锚点坐标要提前配置**：TDoA2 依赖标签已知锚点位置（或用 LPP 下发）；TDoA3 由锚点广播
4. **TDoA2 的锚点 0 是主时钟**：布点时它必须能被所有锚点听到，否则系统停摆；跨房间请直接用 TDoA3
5. **时钟修正不能省**：跳过漂移跟踪，位置会在几十秒内漂掉
6. **天线延迟（Antenna Delay）**：每个节点 RF 延迟不同，会变成系统性偏差，需要标定（参考 `ds-kiel/aladin-uwb`）
7. **许可**：标签侧代码 LGPL/GPL；若产品要闭源，走"自己按协议文档实现"的路线（路线 B-2 或 C）
8. **不要在包络外飞**：TDoA 的几何特性决定标签越靠近锚点包络中心精度越好，跑到远处几何精度急剧恶化（官方文档的几何章节有图示说明）

---

## 9. 把 LPS 数据引出来给 PX4 / 自有板子：三条现成路线

> 结论：**有先例，而且 Bitcraze 自己就做过这个产品**。以下三条路线都是被验证过的。

### 路线 1：LPS Node 的 Sniffer 模式（官方数据出口，零固件开发）

`lps-node-firmware/src/uwb_sniffer.c` 把节点变成**纯粹的 UWB 嗅探器**：收到任何空口包就输出
**DW1000 硬件打出的 40 位原始接收时间戳**（`dwGetRawReceiveTimestamp`）+ 源/目的地址 + 完整 payload。

输出有两种模式（默认文本，发一个字符 `b` 切到二进制）：

```
二进制帧格式：
0xBC | ts(5B, 小端, 扩展为8B) | src(1B) | dest(1B) | len(2B) | payload | len(2B, 重复用于同步检测)
文本格式：
From 00 to ff @0x00000000: 2222...
```

**Bitcraze 官方还配了完整的 Python 解码链**（`lps-node-firmware/tools/sniffer/`）：

```
sniffer_binary.py  →  tdoa2_decoder.py / tdoa3_decoder.py  →  tdoa3_tof.py
   USB 二进制流          解出 seqs/timestamps/distances        算出锚点间 TOF
                              以及 TDoA3 的 remoteAnchorData 和锚点坐标(x,y,z)
```

- `tdoa3_decoder.py` 甚至直接解出 **LPP 数据里的锚点坐标**（3 个 float，x/y/z）
- `tdoa3_tof.py` 里给出了官方常数：`ANTENNA_OFFSET = 154.6`、`LOCODECK_TS_FREQ = 499.2e6*128`、tick→米换算

**这条路的含义**：买 N 个 Node 当锚点 + 1 个 Node 刷 Sniffer 模式，你就在**任意主机（树莓派、机载计算机、你自己的板子）**上拿到了计算 TDoA 所需的全部原始数据。

| 优点 | 缺点 |
|---|---|
| 零 UWB 固件开发，官方解码器可直接用 | 需要一个额外的 Node 专门当嗅探器（$180） |
| 时间戳由 DW1000 硬件保证，主机不是实时系统也没关系 | **时钟修正 + 定位解算要自己实现**（官方只给解码，不给时钟修正和求解器） |
| 可直接挂在机器人上，随机器人移动 | USB 线缆；高速率时 Python 侧要留意吞吐 |

### 路线 2：`PLATFORM=tag`（Roadrunner）+ UART2 输出 MAVLink（最接近"完整产品"）

**Bitcraze 官方做过这个产品：Roadrunner**（$245，**已停产**）——Loco 兼容的**标签**，定位就是"装在第三方机器人/无人机上"：

- 基于 Crazyflie 硬件同一个代码库（STM32F405 + nRF51822 + DWM1000）
- **5–12V 供电**或 micro-USB，4 针电源+锁定连接器
- 数据接口：**USB / UART RX-TX（绿色 UART 口）/ 2.4 GHz 低延迟无线 / BLE**
- 支持 Crazyflie 扩展 deck

关键：**今天上游 crazyflie-firmware 里仍然保留这个平台目标**：

```
src/platform/src/platform_tag.c          → deviceType "RR10" / "Roadrunner 1.0"，motorMapNoMotors
src/platform/interface/platform_defaults_tag.h
make PLATFORM=tag
```

**社区已经做过"Roadrunner → MAVLink → 主机解算"的完整实现**（马里兰大学 UMD LIFE 实验室，用于他们的无人机）：

仓库 [`justinleeyang/roadrunner_mavlink`](https://github.com/justinleeyang/roadrunner_mavlink)（`umdlife/roadrunner_mavlink` 的 fork，原仓库已删除）在其分支 `umdlife/crazyflie-firmware#u2_feature_custom_kalman` 上：

```
make PLATFORM=tag            # 构建标签固件
  - 禁用 controller (NO_CONTROLLER)
  - 用互补滤波代替卡尔曼，IMU 更稳
  - UART2 默认 1,000,000 baud
  - 通过 UART2 发送 MAVLink：
      TDOA_MEASUREMENT  ← 原始 TDoA 量测，最高 400 Hz（典型 200 Hz）
      GYRO_ACC          ← 原始 IMU，100 Hz
      QUATERNION        ← 姿态四元数，20 Hz
```

主机侧再用 [`AlexisTM/MultilaterationTDOA`](https://github.com/AlexisTM/MultilaterationTDOA)（专为 Bitcraze LPS 写的 TDoA 多站定位库，含给卡尔曼用的雅可比）解算位置。

> 这个方案的精髓：**标签只把 TDoA 量测原样吐出来（而不是位置），位置解算放在外部板子/PX4 伴侣计算机上**——这正是 TDoA 最合理的分工。

注意：这套代码是 2019 年 Python 2 时代的，`umdlife/crazyflie-firmware` 原仓库已删除，只能作为**架构参考**，不能直接拿来跑。

### 路线 3：把 Loco Deck 的 SPI 引到你自己的板子

Loco Positioning Deck 本质上就是**一块 DWM1000 + 天线 + 4 个状态 LED + 1-wire 存储（用于 deck 自动识别）**，通过 Crazyflie 的扩展口以 SPI 与主机通信。原理图公开：

```
hardware/src/products/loco-deck/electronics/loco_deck_revd.pdf
hardware/src/products/loco-deck/datasheet/index.md
```

所以要"从 deck 引出数据"到自己的开发板，要做的是：

1. 按原理图把 DWM1000 的 **SPI（SCK/MISO/MOSI）+ CS + IRQ** 接到你 MCU 的 SPI 上
2. 用 [`bitcraze/libdw1000`](https://github.com/bitcraze/libdw1000) 驱动（平台无关，只需实现 SPI 读写 + 延时回调）
3. 移植标签侧协议逻辑（见第 4 节代码地图），或按协议文档自己写

参考实现（都不依赖 Bitcraze 硬件）：

- [`dy-dx-1/DW1000-TDoA3`](https://github.com/dy-dx-1/DW1000-TDoA3)：**Python + spidev** 在 Linux 板子上实现 TDoA3 标签
- [`Kuipman/uwb_tdoa`](https://github.com/Kuipman/uwb_tdoa)：UCSC 本科论文项目，reverse-TDoA 被动标签实时 3D 定位，含论文

注意官方商店明确写着 deck **"can not be used standalone"**——这是产品定位（必须配 Crazyflie 主机），电气上接到你自己的 MCU 完全可行，只是属于自己做的硬件集成，Bitcraze 不背书。

### 三条路线对比

| | 工作量 | 延迟 | 额外成本 | 许可风险 | 适合 |
|---|---|---|---|---|---|
| 路线 1 Sniffer | **最低**（几小时出数据，解算要自己做） | 中（USB + 主机解算） | +1 个 Node $180 | 无（读自己的固件输出） | 快速验证、先跑通 TDoA 数学 |
| 路线 2 `PLATFORM=tag` + UART | 中（需要 Roadrunner/兼容硬件） | **低**（200–400 Hz UART） | 硬件难买（停产） | 高（GPL 固件） | 有 Roadrunner 或想直接抄架构 |
| 路线 3 自研 deck/模块标签 | 高（1–3 周固件） | **最低**（量测直出） | deck $95 或 DWM1000 模块 | 自己写可控 | 要产品化、要商用 |

### 无论哪条路都要处理的四件事

1. **时钟修正必须自己实现**（Sniffer 和自研标签都是）：官方文档给了方法（δrx/δtx 估频率比 + 离群剔除 + 低通 + leaky bucket），但要你自己写代码
2. **TDoA 量测 vs 位置**：能把 **TDoA 差值**送进飞控 EKF 就别送位置，延迟和鲁棒性都更好（Crazyflie 的 `mm_tdoa.c` 就是这个思路，可以直接照抄观测模型）
3. **坐标系与时间戳**：LPS 是右手系，PX4 用 NED；MAVLink `time_usec` 必须是同步过的时间基准
4. **天线延迟标定**：`ANTENNA_OFFSET = 154.6` 是 Bitcraze 硬件的经验值，换了模块要重新标（参考 `ds-kiel/aladin-uwb`）
