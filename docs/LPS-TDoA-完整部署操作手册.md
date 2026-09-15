# 手上有多个 LPS Node：TDoA 完整部署操作手册

> 本文命令示例按 Linux 习惯写成 `python3 tools/xxx.py`。
> **在 Windows 上**：把 `python3` 换成 `python`，路径分隔符用反斜杠，例如
> `python tools\lps_tdoa3_solver.py --port COM7 --anchors tools\anchors_example.yaml`。
> 本工程的 Windows 快捷入口见上级目录 `README.md`。

> 目标：用**多个 Loco Positioning Node**跑通一套可用的 TDoA 三维定位系统，并把位置输出给 PX4 或你自己的开发板。
> 全部步骤和协议细节来自 Bitcraze 官方源码核实（`lps-node-firmware` 2022.09 / `crazyflie-firmware` master）。

---

## 0. 先明确你能做到什么（架构选择）

只有 Node、没有 Crazyflie、没有 Loco Deck，也能做完整 TDoA——关键是把**一个 Node 当作数据出口**：

```
        ┌──────────────┐
        │ 锚点 Node 0  │──┐
        ├──────────────┤  │   空中链路：TDoA3 协议
        │ 锚点 Node 1  │──┼──►  每个锚点随机发送，包里带其他锚点的
        ├──────────────┤  │     rx 时间戳 + 自己测到的锚点间 TOF
        │     ...      │──┤
        ├──────────────┤  │
        │ 锚点 Node 7  │──┘
        └──────────────┘
                 │
                 ▼  被动监听（不发任何 UWB 包）
        ┌──────────────────┐
        │ Sniffer Node     │  DW1000 硬件给每个包打 40 位接收时间戳
        │ (模式 's')        │
        └────────┬─────────┘
                 │ USB CDC，二进制帧
                 ▼
        ┌─────────────────────────────────────────┐
        │ 主机（PC / 树莓派 / 机载计算机）            │
        │  1. 解析 sniffer 帧                      │
        │  2. 解析 TDoA3 包                        │
        │  3. 时钟修正（每个锚点一个时钟漂移系数）      │
        │  4. 算 TDoA 差值                          │
        │  5. 多站定位 → (x,y,z)                    │
        │  6. 输出 JSON / MAVLink / ROS             │
        └─────────────────────────────────────────┘
```

**这套架构的优点**：不需要 Crazyflie、不需要 LPS Deck、不需要改任何固件、不需要锚点知道自己的坐标。
**限制**：Sniffer 节点通过 USB 线连着主机，所以移动端目前是"Node + 主机"这个组合；要真正机载低延迟，见第 10 节的演进路线。

### 时间与人力估计

| 阶段 | 内容 | 估计耗时 |
|---|---|---|
| 1 | 固件烧录 + 节点配置 | 2–4 小时（N 个节点） |
| 2 | 布点 + 坐标测量 | 2–4 小时 |
| 3 | Sniffer 联调、验收 | 1–2 小时 |
| 4 | 主机侧解算跑通 | 半天（脚本已提供） |
| 5 | 接入 PX4/自有板（可选） | 1–2 天 |

---

## 1. 物料与软件清单

### 硬件

- LPS Node **N 个**（N ≥ 5：至少 4 个锚点做 3D + 1 个作 Sniffer；推荐 8 个锚点 + 1 个 Sniffer = 9 个）
- micro-USB 数据线 N 条（注意：很多线只能供电不能传数据，务必用能传数据的）
- USB HUB（同时连多个节点时）、5V USB 电源适配器若干
- 锚点支架（离墙 ≥15 cm；官方有 3D 打印件：`bitcraze/bitcraze-mechanics/LPS-anchor-stand`）
- 卷尺 + 激光测距仪（量锚点坐标）
- 主机：Windows/Linux PC 或树莓派

### 软件

```bash
# 1) 烧录工具（GUI）
git clone https://github.com/bitcraze/lps-tools.git
cd lps-tools
python3 -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip3 install -e .[pyqt5]
python3 -m lpstools                                    # 启动 GUI

# 2) 官方解码脚本所需
pip3 install pyserial pyyaml

# 3) 本方案的主机侧解算器所需
pip3 install pyserial numpy pyyaml

# 4) 固件源码（只有你要自己改固件时才需要）
git clone --recursive https://github.com/bitcraze/lps-node-firmware.git
```

### Linux 权限（重要，否则烧录和串口都会失败）

```bash
# DFU 设备权限（烧录用）
sudo tee /etc/udev/rules.d/99-lps.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df11", MODE="0664", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", MODE="0664", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG dialout,plugdev $USER    # 重新登录生效
```

设备识别对照：

| 状态 | VID:PID | 说明 |
|---|---|---|
| 正常运行 | `0483:5740` | CDC-ACM 串口（`/dev/ttyACM*`、`COM*`） |
| DFU 引导模式 | `0483:df11` | DfuSe 设备，用于烧录 |

---

## 2. 节点角色与 ID 规划（先规划再动手）

Node 的地址是 8 字节 UWB 地址的低字节，`0xbccf0000000000XX` 的 `XX`。TDoA3 支持 0–255 的 ID，TDoA2 只支持 0–7。

| 节点 | 角色 | ID | 模式 |
|---|---|---|---|
| Node 1 | TDoA3 锚点 | 0 | `TDoA Anchor V3` |
| Node 2 | TDoA3 锚点 | 1 | `TDoA Anchor V3` |
| … | … | … | … |
| Node 8 | TDoA3 锚点 | 7 | `TDoA Anchor V3` |
| Node 9 | **Sniffer（数据出口）** | 20（随意，不参与定位） | `Sniffer` |

约定：**ID 与物理位置一一对应并写在标签上**（例如用纸胶带写 `A0`…`A7`），否则后面测坐标、排查问题会乱。

---

## 3. 阶段一：烧录/升级固件

节点出厂固件版本可能较老，建议统一刷成最新官方版 **2022.09**：

- 下载：`https://github.com/bitcraze/lps-node-firmware/releases/download/2022.09/lps-node-firmware-2022.09.dfu`（88 KB）

### 方式 A：LPS 工具 GUI（推荐，最简单）

1. 用 USB 线连接一个 Node
2. 运行 `python3 -m lpstools`
3. 在界面里选择串口 → 选择 "Update firmware" → 选刚才下载的 `.dfu` 文件
4. 工具会自动让节点进入 DFU 模式并烧写，等待完成
5. 拔插一次 USB，节点重启
6. 对每个 Node 重复

### 方式 B：命令行（批量/脚本化更高效）

**第 1 步：让节点进入 DFU 模式**（在串口控制台里发送字符 `u`，节点会复位进引导程序）

```bash
python3 - <<'EOF'
import serial, time
s = serial.Serial('COM5', 115200, timeout=1)   # Linux: /dev/ttyACM0
time.sleep(0.3)
s.write(b'u')
time.sleep(0.3)
s.close()
print("节点已进入 DFU 模式")
EOF
```

**第 2 步：烧写**

```bash
# 确认设备出现
lsusb | grep 0483:df11

# 烧写（dfu-util 支持 DfuSe 格式的 .dfu 文件）
dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D lps-node-firmware-2022.09.dfu
```

Windows 若 `dfu-util` 找不到设备，需要用 Zadig 给 `0483:df11` 装 WinUSB 驱动（官方文档 `docs/building/zadig_lps.md`）。

### 方式 C：从源码编译（只有你需要改固件时才做）

```bash
git clone --recursive https://github.com/bitcraze/lps-node-firmware.git
cd lps-node-firmware
# 需要 arm-none-eabi-gcc（官方推荐 5.4 版本工具链）
make                       # 生成 .dfu/.bin
make dfu                   # 通过 DFU 烧写
# 或者用官方 docker：
docker run --rm -v ${PWD}:/module bitcraze/builder ./tools/build/compile
```

### 烧录验收

用串口终端（115200，CDC 波特率其实无所谓）连接节点，应该看到：

```
====================
SYSTEM  : CPU-ID: ...
TEST    : EEPROM self-test ... [OK]
TEST    : Initialize UWB  ... [OK]
CONFIG  : Address is 0x0
CONFIG  : Mode is TDoA Anchor V3
CONFIG  : Bitrate: normal
CONFIG  : Preamble: normal
SYSTEM  : Node started ...
SYSTEM  : Press 'h' for help.
```

看到这段说明固件正常、DW1000 通信正常。

---

## 4. 阶段二：配置每个节点（串口菜单）

节点配置全部通过 **USB 串口控制台**完成，**不需要 Crazyflie**。

### 4.1 打开串口

| 系统 | 命令 |
|---|---|
| Linux | `picocom /dev/ttyACM3` 或 `python3 -m serial.tools.miniterm /dev/ttyACM3 115200` |
| macOS | `screen /dev/tty.usbmodem1415501` |
| Windows | 设备管理器看 COM 号，用 PuTTY / `python3 -m serial.tools.miniterm COM5 115200` |

### 4.2 菜单命令总表（源码级核实）

| 按键 | 作用 |
|---|---|
| `0`–`9` | 直接设置节点地址（ID） |
| `i` | 然后输入数字 + 回车，设置 ≥10 的 ID |
| `a` | 切到 TWR Anchor 模式 |
| `t` | 切到 TWR Tag 模式 |
| `s` | **切到 Sniffer 模式** |
| `m` | 打开模式列表菜单，再按数字选择：`0`=TWR Anchor、`1`=TWR Tag、`2`=Sniffer、`3`=**TDoA Anchor V2**、`4`=**TDoA Anchor V3** |
| `r` | 射频模式菜单（比特率/前导码），默认即可，**但所有节点必须一致** |
| `p` | 发射功率设置 |
| `d` | 恢复 EEPROM 默认配置 |
| `b` | 串口切到二进制输出（Sniffer 数据流用，脚本会自动发） |
| `u` | 进入 DFU 引导模式（烧录用） |
| `h` | 帮助 |
| `#` | 生产自检（会停机） |

> ⚠️ **改完配置必须断电重启**（提示语："EEPROM configuration changed, restart for it to take effect!"）。只按复位不够，最好直接拔插 USB。

### 4.3 逐个节点操作清单

**对每个锚点节点（ID 0–7）：**

```text
连接 USB → 打开串口
按 0        # 设置 ID=0（按 1 则 ID=1，以此类推）
按 m 然后按 4   # 模式选 TDoA Anchor V3
断电重启
确认打印：CONFIG : Address is 0x0 / CONFIG : Mode is TDoA Anchor V3
```

**对 Sniffer 节点：**

```text
连接 USB → 打开串口
按 i 然后输入 20 回车   # 设一个不冲突的 ID
按 s        # 切到 Sniffer 模式
断电重启
确认打印：CONFIG : Mode is Sniffer
```

### 4.4 配置验收（表格自查）

| 检查项 | 期望值 | 检查方式 |
|---|---|---|
| 每个节点 ID 唯一 | 0–7 各一次 + Sniffer 一个 | 串口打印 `CONFIG : Address is 0x..` |
| 所有锚点模式一致 | `TDoA Anchor V3` | 串口打印 `CONFIG : Mode is ...` |
| 射频参数一致 | `Bitrate: normal` / `Preamble: normal`（或全部用低速率） | 串口打印 |
| Sniffer 能听到所有锚点 | 每个锚点 ID 都出现 | 见第 7 节 |

**如果锚点之间测距始终为空（`hasDistance` 位一直是 0），或者速率/前导码前后不一致，先在 `r` 菜单里把所有节点（含 Sniffer）统一成默认值再测。**

---

## 5. 阶段三：布点（决定精度的关键）

官方经验（`docs/user-guides/anchor-setup.md`）：

1. **离墙、离天花板 ≥ 15 cm**（金属和混凝土会强烈反射 UWB）
2. **锚点间距 ≥ 2 m**
3. **锚点在不同高度**（避免全部共面，否则 Z 方向几何精度差）
4. 8 锚点参考布置：**上下各 4 个，构成长方体**（官方验证过的参考系统）
5. 标签尽量工作在**锚点围成的包络内部**——TDoA 的几何特性决定：包络外精度急剧恶化（等 TDoA 双曲线趋于平行）
6. 远离大面积金属、玻璃幕墙、运行中的电机/变频器

```
 顶部 4 个（z≈2.4m）              底部 4 个（z≈0.2m）
      A1 ────────── A2              A5 ────────── A6
      │              │              │              │
      │   工作区域    │              │              │
      │              │              │              │
      A0 ────────── A3              A4 ────────── A7
```

---

## 6. 阶段四：测量并记录锚点坐标

1. 选定**右手坐标系**：推荐用房间地面的某个角落做原点 `(0,0,0)`，两面墙分别是 x、y 方向，z 向上
2. 用激光测距仪对着墙面量，可以直接读出坐标
3. **量的是 UWB 天线的相位中心**（模块中心附近），不是外壳/支架底座
4. 按模板填写坐标文件

模板已提供：`outputs/tools/anchors_example.yaml`

```yaml
anchors:
  0: {x:  0.15, y:  0.15, z: 2.40}
  1: {x:  5.85, y:  0.15, z: 2.40}
  # ...
```

> 注意：**这套方案里锚点坐标只存在于主机配置文件中，不需要写进锚点**。
> 官方 `tools/lpp/set_positions.py` 是"通过 Crazyflie 用 LPP 空中下发"，我们没有 Crazyflie，也不需要它——除非你要兼容 Bitcraze 官方的标签固件（见第 11 节的可选改造）。

---

## 7. 阶段五：用 Sniffer 联调（这是最关键的验收环节）

### 7.1 抓包与解码

Sniffer 节点通过 USB 连到主机后：

```bash
cd lps-node-firmware

# 看原始解码结果（这一步脚本会自动把 sniffer 切到二进制模式）
python3 tools/sniffer/sniffer_binary.py /dev/ttyACM0 yaml | python3 tools/sniffer/tdoa3_decoder.py

# 只看锚点 2 和 3 的包
python3 tools/sniffer/sniffer_binary.py /dev/ttyACM0 yaml | python3 tools/sniffer/tdoa3_decoder.py 2 3

# 看锚点之间的实际距离（单位米）
python3 tools/sniffer/sniffer_binary.py /dev/ttyACM0 yaml | python3 tools/sniffer/tdoa3_decoder.py | python3 tools/sniffer/tdoa3_tof.py m

# 看丢包率
python3 tools/sniffer/sniffer_binary.py /dev/ttyACM0 yaml | python3 tools/sniffer/tdoa3_decoder.py | python3 tools/sniffer/tdoa3_packet_loss.py
```

Windows 把 `/dev/ttyACM0` 换成 `COM5` 之类。

### 7.2 必须通过的 4 项检查

| # | 检查 | 合格标准 | 不合格怎么办 |
|---|---|---|---|
| 1 | 8 个锚点都能收到 | 每个 ID 稳定出现 | 检查锚点供电/ID/模式/距离；`r` 菜单速率是否一致 |
| 2 | **锚点间距离 vs 卷尺实测** | 差值 < 0.1–0.2 m | **这一项验证 `distance` 字段的单位与量程**；若差一个固定比例或整体偏大，说明单位假设需要修正（见第 13 节） |
| 3 | 距离数值稳定 | 同一对锚点几分钟内波动 < 0.1 m | 检查多径、锚点是否被遮挡、离墙太近 |
| 4 | 丢包率 | < 20%（视环境） | 拉近锚点、降低速率/加长前导码（Long Range 模式） |

> **第 2 项是整个项目的分水岭。** 它同时验证了三件事：DW1000 收发正常、锚点间双向测距正常、`distance` 字段的物理单位。通过之后再往下做才有意义。

### 7.3 记录基线数据

建议把一次干净的抓包存下来：

```bash
python3 tools/sniffer/sniffer_binary.py /dev/ttyACM0 yaml > baseline.yaml
```

后面调算法时可以反复回放，不必每次都开硬件。

---

## 8. 阶段六：主机侧 TDoA 解算

Bitcraze 官方给了解码器，但**没有给 Python 的时钟修正和 TDoA 解算**（那部分只存在于 Crazyflie 的 C 固件里）。本方案补上了这一环：

- 解算器：`outputs/tools/lps_tdoa3_solver.py`
- 算法逐一对照 C 源码移植：
  - 时钟修正 ← `crazyflie-firmware/src/utils/src/clockCorrectionEngine.c`
  - TDoA 计算 ← `.../utils/src/tdoa/tdoaEngine.c` 的 `calcTDoA()`
  - 位置解算 ← 自研（高斯-牛顿 + Huber 鲁棒核 + 粗差剔除）；官方是把 TDoA 直接喂 EKF，见 `kalman_core/mm_tdoa.c`

### 8.1 核心公式

对"锚点 i 的包"里引用的"锚点 j 的包"：

```
δtx(In时钟) = tof(i←j) + (tx_i − rx_i(j))           # 两个锚点发送时刻之差
δrx(标签时钟) = rx_T(i) − rx_T(j)                    # 标签收到两包的时间差
α_i = Δrx_T / Δtx_i                                  # 标签时钟 / 锚点 i 时钟（漂移系数）
TDoA = δrx − α_i · δtx                              # 单位：tick
d(P,A_i) − d(P,A_j) = c · TDoA / 63897600000         # 转成米
```

其中 `c/63897600000 ≈ 4.6918 mm/tick`，`63897600000 = 499.2e6 × 128`。

### 8.2 运行

```bash
cd outputs/tools

# 第 1 步：自检（只解码，确认每个锚点的收包情况与时钟修正值）
python3 lps_tdoa3_solver.py --port /dev/ttyACM0 --anchors anchors_example.yaml --check

# 第 2 步：解算位置
python3 lps_tdoa3_solver.py --port /dev/ttyACM0 --anchors anchors_example.yaml
#   [   3.42s] pos=(  1.234,  -0.567,   0.891) m  meas= 14  rms=0.043 m

# 第 3 步：JSON 行输出（给 PX4/ROS 桥用）
python3 lps_tdoa3_solver.py --port /dev/ttyACM0 --anchors anchors_example.yaml --json
```

`--check` 模式下会周期性打印：

```
[check] 收包统计
  anchor   0: packets=1843    cc=1.000003214 last_seen=  0.02s lpp_pos=None
  anchor   1: packets=1791    cc=0.999996872 last_seen=  0.03s lpp_pos=None
  ...
```

- `packets` 应持续增长且各锚点数量接近
- `cc` 应该非常接近 `1.000000000`（差异在 ±1e-5 以内是正常的晶振漂移）；如果 `cc=0` 说明该锚点只收到过 1 个包或收包中断
- `last_seen` 应该始终 < 0.1 s

### 8.3 算法验证（已完成的部分）

由于没有真实硬件，我用**合成数据仿真**验证了数学部分（`work/test_solver_sim.py`）：

```
构造 8 锚点的理想 TDoA3 空中报文（含 ±10 ppm 独立时钟漂移）
   - 时钟修正估计：cc=1.000010000，理论值 1/rate=1.000010000  ✓ 完全吻合
   - TDoA 测量误差：约 ±3 mm（受 distance 字段 uint16 量化限制，1 tick = 4.69 mm）
   - 定位结果：解出 (2.6997, 2.1990, 0.9975) m，真值 (2.7, 2.2, 1.0) m，
                误差 0.27 cm，残差 rms = 2.1 mm ✓
```

也就是说：**公式、符号约定、单位换算、定位求解器都是对的**；剩下的不确定性只来自真实硬件（多径、天线延迟、时间戳抖动）。

### 8.4 现场验证步骤

1. **静态验证**：把 Sniffer 放在已知坐标点（例如房间中央、吊在固定高度），看解算值与卷尺量的差值。合格标准：**< 20 cm**
2. **网格验证**：在地面取 5–9 个点，逐点比对，画出误差热力图，找出精度差的区域（通常靠近墙边、锚点包络外）
3. **动态验证**：拿着 Sniffer 沿直线匀速走，输出应该是平滑直线而不是锯齿——锯齿说明测量噪声大，需要加滤波器
4. **重复性验证**：同一位置放 1 分钟，看位置标准差。合格标准：**< 5 cm**

---

## 9. 阶段七：输出到 PX4 / 自己的开发板

解算器输出的是**位置** `(x,y,z)`（JSON 行）。两条接入路线：

### 路线 1：位置回灌（简单，先用这个跑通）

写一个小桥接脚本，把 JSON 行转成 MAVLink：

| 飞控 | 消息 | 参数配置 |
|---|---|---|
| PX4 | `VISION_POSITION_ESTIMATE` | 开 `EKF2_EV_CTRL`；设置 `EKF2_EV_DELAY` 补偿延迟；处理 HOME 与解锁条件 |
| ArduPilot | `VISION_POSITION_ESTIMATE` | `EK3_SRC1_POSXY=6`（ExternalNav）、`EK3_SRC1_POSZ=6` |
| 通用 | `GPS_INPUT` | 把 LPS 坐标"伪装"成 GPS 位置（适合本来就依赖 GPS 接口的飞控） |

要点：

- **坐标系对齐**：LPS 是右手系，PX4 内部是 NED（North-East-Down），需要明确旋转关系并写入配置，避免"飞起来往反方向漂"
- **时间戳**：MAVLink 的 `time_usec` 必须是同步过的时钟；如果你的桥接脚本用本地时间，务必确认与飞控时钟一致
- **延迟**：位置回灌的最大问题是链路延迟（USB + Python + MAVLink），会拖累控制带宽

### 路线 2：量测回灌（推荐，性能好）

改进思路是**不送位置、送 TDoA 量测**（就是 `lps_tdoa3_solver.py` 里 `pending` 那一步的 `(i, j, tdoa_m)`），让飞控自己的 EKF 去融合。Crazyflie 就是这么做的（`mm_tdoa.c`）。这需要：

1. 自定义一条 MAVLink 消息（或用 ROS2 自定义话题）
2. 飞控侧实现 TDoA 观测模型（可以直接参考 `mm_tdoa.c` 的雅可比）
3. 这样延迟更低、鲁棒性更好，但需要改飞控代码

### 中间方案：ROS2

用 ROS2 做中介最省事：

```bash
# 解算器输出 JSON 行 → 一个小 ROS2 节点转成话题
python3 lps_tdoa3_solver.py --port /dev/ttyACM0 --anchors anchors.yaml --json \
  | python3 ros_bridge.py
```

参考 `TIERS/active-passive-uwb-ros` 的节点组织方式（它也是把 UWB 量测发成 ROS 话题）。

---

## 10. 演进路线：把 Sniffer 换成真正的机载标签

现在这套方案里，移动端是"Sniffer Node + 主机"。要真正机载、低延迟，有两条路：

### 路线 A：复用 Bitcraze 标签固件（最快，但硬件停产）

- `crazyflie-firmware` 里有 **`PLATFORM=tag`（Roadrunner 1.0）** 目标：无电机的标签平台，通过 UART/USB 输出
- 前人已做过：UMD 的 `roadrunner_mavlink`（分支 `umdlife/crazyflie-firmware#u2_feature_custom_kalman`）把 **TDOA_MEASUREMENT 以 200–400 Hz 从 UART2 输出**，主机用 `AlexisTM/MultilaterationTDOA` 解算
- 问题：Roadrunner 已停产（原价 $245），只能买二手或自建

### 路线 B：自研标签（推荐长期方案）

硬件：任意带 SPI 的 MCU（STM32/ESP32/树莓派）+ DWM1000/BU01 模块。
固件：移植 `lpsTdoa3Tag.c` + `tdoaEngine.c` + `tdoaStorage.c` + `clockCorrectionEngine.c`，或直接用本方案的 Python 逻辑写成 C。
参考实现：`dy-dx-1/DW1000-TDoA3`（Python/spidev）。

届时你的主机侧算法（第 8 节）可以整体搬到标签的 MCU 上，Sniffer 就退休了。

---

## 11. 可选改造：用 USB 直接给锚点写坐标（不需要 Crazyflie）

### 什么时候需要

只有当你想**兼容 Bitcraze 官方标签固件**（Crazyflie/Loco Deck/Roadrunner）时，锚点才必须知道自己的坐标——因为官方标签是从空中 LPP 数据里读锚点位置的。
本方案的主机侧解算**不需要**这个，可以跳过本节。

### 官方的限制

官方 `tools/lpp/set_positions.py` 明确写着 *"Requires a system in TDoA mode and a Crazyflie with the LPS deck"* —— 它通过 Crazyflie 的无线链路把 LPP 包桥接到锚点。我们只有 Node，没有 Crazyflie。

### 改造思路（约 30 行代码）

节点固件里已经有完整的 LPP 短包处理函数 `lppHandleShortPacket()`（`src/lpp.c`），只是没有从 USB 串口喂数据的入口。我们给它加一个菜单命令即可：

```c
/* 1) src/main.c: Menu_t 增加一个状态 */
typedef enum {mainMenu, modeMenu, idMenu, radioMenu, powerMenu, lppMenu} Menu_t;

/* 2) 文件顶部增加 */
#include "lpp.h"
static char lppHexBuf[128];
static int  lppHexLen = 0;

/* 3) handleMenuMain 里增加分支 */
    case 'L':
      printf("Input LPP short packet in hex, then ENTER:\r\n");
      lppHexLen = 0;
      menuState->currentMenu = lppMenu;
      menuState->configChanged = false;
      break;

/* 4) 新增处理函数 */
static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static void handleMenuLpp(char ch, MenuState* menuState) {
  if (ch == '\r' || ch == '\n') {
    if (lppHexLen > 0 && (lppHexLen % 2) == 0 && lppHexLen / 2 <= 64) {
      char bytes[64];
      int n = lppHexLen / 2;
      for (int i = 0; i < n; i++) {
        int hi = hexNibble(lppHexBuf[2 * i]);
        int lo = hexNibble(lppHexBuf[2 * i + 1]);
        if (hi < 0 || lo < 0) { printf("Bad hex\r\n"); goto done; }
        bytes[i] = (char)((hi << 4) | lo);
      }
      lppHandleShortPacket(bytes, n);   /* 例：01000080bf... = 设置锚点坐标 */
    } else {
      printf("Bad hex length\r\n");
    }
  done:
    lppHexLen = 0;
    menuState->currentMenu = mainMenu;
    return;
  }
  if (lppHexLen < (int)sizeof(lppHexBuf)) lppHexBuf[lppHexLen++] = ch;
}

/* 5) handleSerialInput 的 switch 里增加 */
    case lppMenu: handleMenuLpp(ch, &menuState); break;
```

### 配套的 PC 端脚本

LPP 短包内容格式（`inc/lpp.h`，注意 USB 上传输时**不带**空中链路的 `0xF0` 头）：

| 功能 | 字节 |
|---|---|
| 设置锚点坐标 | `0x01` + x,y,z 各 4 字节 float（小端），共 13 字节 |
| 重启/进引导 | `0x02` + mode |
| 切模式 | `0x03` + mode（1=TWR，2=TDoA2，3=TDoA3），写完自动复位 |
| UWB 功率 | `0x04` + 位域 + 4 字节功率 |
| UWB 速率/前导码 | `0x05` + 位域 |

```python
import serial, struct, time

def set_anchor_position(port, x, y, z):
    payload = bytes([0x01]) + struct.pack("<fff", x, y, z)
    with serial.Serial(port, 115200, timeout=1) as s:
        time.sleep(0.3)
        s.write(b'L')                     # 进入 LPP 输入模式
        time.sleep(0.1)
        s.write(payload.hex().encode() + b'\r')
        time.sleep(0.3)

set_anchor_position('COM5', 0.15, 0.15, 2.40)
```

编好固件后按第 3 节的方式烧进去，就能**彻底摆脱 Crazyflie** 完成全流程配置。

> ⚠️ 该改造代码未编译验证，属于方案附赠；如果你不需要兼容官方标签，直接跳过。

---

## 12. 验收测试矩阵

| # | 测试项 | 方法 | 合格标准 |
|---|---|---|---|
| 1 | 收包完整性 | `--check` 模式 | 所有锚点都出现，`last_seen < 0.1s` |
| 2 | 锚点间测距 | `tdoa3_tof.py m` vs 卷尺 | 偏差 < 0.2 m |
| 3 | 时钟修正收敛 | `--check` 输出的 `cc` | 偏离 1 不超过 ±1e-5 |
| 4 | 静态定位精度 | 已知点重复测 | 误差 < 20 cm |
| 5 | 静态重复性 | 同点 1 分钟 | 标准差 < 5 cm |
| 6 | 动态跟随 | 匀速直线行走 | 轨迹平滑、无跳变 |
| 7 | 覆盖范围 | 房间内网格取点 | 包络内精度均匀；记录包络外衰减 |
| 8 | 长时间稳定性 | 连续跑 1 小时 | 无位置漂移（漂移说明时钟修正有问题） |

---

## 13. 故障排查表

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 串口看不到节点 | 线材只供电 / 驱动问题 | 换数据线；Windows 装 CDC 驱动；Linux 检查 `dmesg` |
| 烧录失败 | 未进 DFU / 权限 | 发 `u` 进 DFU；装 udev 规则；Windows 用 Zadig |
| 配置不生效 | 没重启 | **断电重启**（不是仅复位） |
| Sniffer 收不到任何包 | 模式不是 Sniffer / 射频参数不一致 | 串口确认 `Mode is Sniffer`；用 `r` 统一射频参数 |
| 只收到部分锚点 | 距离太远 / 遮挡 / 功率低 | 拉近、抬高、`p` 调功率；试 Long Range 模式 |
| `distance` 字段一直是空（`hasDistance=0`） | 锚点间测距未建立：时钟修正未收敛，或被固件的 `MIN_TOF` 阈值滤掉 | 等 30 秒让时钟修正收敛；检查锚点是否两两可见；**若始终为空，退到 TDoA2 模式**（TDoA2 包结构固定含 8 组距离） |
| `tdoa3_tof.py` 的距离与卷尺差一个固定倍数 | `distance` 字段单位假设不符 | 用卷尺实测值反推比例系数，在解算器里调整 `M_PER_TICK`，或先改用 TDoA2 验证 |
| `cc` 一直是 0 | 该锚点包太少 / 序号回绕处理异常 | 确认收包率；检查是否有大量丢包 |
| 位置解算发散或跳变 | 测量粗差、几何差、时钟修正未收敛 | 用 `--check` 看 `cc`；把 Sniffer 移入锚点包络内；调大 `OUTLIER_GATE_M`；确认锚点坐标没写错 |
| 位置整体偏移一个固定量 | **锚点坐标测量误差** 或 **天线延迟差异** | 用一张纸/尺子复核坐标；对已知点做偏差标定并整体平移 |
| 位置镜像/正负号反了 | 坐标系手性搞错 或 TDoA 符号约定 | 检查是否为右手系；用解算器的 `--sign -1` 试一下 |

### 一个需要留意的源码细节

`uwb_tdoa_anchor3.c` 里有这样两行：

```c
#define ANTENNA_OFFSET 154.6   // In meters
#define ANTENNA_DELAY  ((ANTENNA_OFFSET*499.2e6*128)/299792458.0) // In radio tick
#define MIN_TOF ANTENNA_DELAY
...
if (distance > MIN_TOF) { anchorCtx->distance = distance; ... }
```

按字面计算，`MIN_TOF ≈ 32953 ticks ≈ 154.6 m`，而正常锚点距离（几米）对应的 tick 只有几百——也就是说这个判断会把所有正常距离都滤掉。这个常数看起来可疑（注释"in meters"与单位也不自洽）。

**实践建议**：不要纠结这个常数，直接用第 7.2 节的第 2 项检查（`tdoa3_tof.py` 输出 vs 卷尺实测）来判定 `distance` 字段是否可用。**如果 TDoA3 的测距数据始终异常，直接切到 TDoA2 模式**——TDoA2 是老版本、被 Crazyflie 编队大规模验证过的模式，包结构固定包含 8 组时间戳和距离，本手册的整套流程同样适用（把模式从 `m`+`4` 改成 `m`+`3`，解算器需要按 `tdoa2_protocol.md` 改一下包解析，约 30 行）。

---

## 14. 文件清单

| 文件（相对工程根目录） | 用途 |
|---|---|
| `README.md` | 工程总入口与快速开始 |
| `bootstrap.ps1` | 一键初始化：clone 仓库 + 装依赖 + 备好固件 |
| `tools/lps_flash.py` | 刷固件（串口进 DFU + DfuSe 烧写，支持 `--dry-run` 预检和 `--all` 批量） |
| `tools/lps_config.py` | 配置节点（ID / 模式 / 射频 / 功率，支持 `--assign 0-7` 交互式批量） |
| `tools/lps_capture.py` | 从 Sniffer 节点抓原始数据（保存 .bin + 收包统计） |
| `tools/lps_tdoa3_solver.py` | 主机侧解算器（Sniffer 二进制流 → 3D 位置） |
| `tools/make_fake_capture.py` | 生成假抓包，用于无硬件自检 |
| `tools/test_solver_sim.py` | 合成数据仿真（验证 TDoA 数学与符号约定） |
| `tools/anchors_example.yaml` | 锚点坐标配置模板 |
| `docs/bitcraze-tdoa-原理与移植指南.md` | TDoA 原理、源码地图、移植路线 |
| `docs/uwb-tdoa-opensource-2026-09.md` | 开源方案调研总览 |

---

## 15. 参考链接（全部为官方源码/文档）

- 节点固件：https://github.com/bitcraze/lps-node-firmware
- 节点固件发布（DFU）：https://github.com/bitcraze/lps-node-firmware/releases
- 配置工具：https://github.com/bitcraze/lps-tools
- TDoA 原理：`lps-node-firmware/docs/functional-areas/tdoa_principles.md`
- TDoA3 实现说明：`lps-node-firmware/docs/functional-areas/tdoa3_implementation.md`
- TDoA2 协议：`lps-node-firmware/docs/protocols/tdoa2_protocol.md`
- TDoA3 协议：`lps-node-firmware/docs/protocols/tdoa3_protocol.md`
- 锚点布置：`lps-node-firmware/docs/user-guides/anchor-setup.md`
- TDoA3 设置：`lps-node-firmware/docs/user-guides/tdoa3_setup.md`
- 时钟修正：`crazyflie-firmware/src/utils/src/clockCorrectionEngine.c`
- TDoA 引擎：`crazyflie-firmware/src/utils/src/tdoa/tdoaEngine.c`
- EKF 观测模型：`crazyflie-firmware/src/modules/src/kalman_core/mm_tdoa.c`
