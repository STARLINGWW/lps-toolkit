# UWB TDoA 工作目录

用多个 Bitcraze **Loco Positioning Node** 搭一套完整的 TDoA 三维定位系统：
不需要 Crazyflie、不需要 Loco Positioning Deck、不需要改任何固件。

核心思路：**N 个节点当 TDoA3 锚点，1 个节点当 Sniffer（纯被动嗅探），
主机侧做时钟修正 + TDoA 解算 + 定位**。

---

## 目录结构

```
D:\_AAA_CODE_ALL\UWB\
├─ bootstrap.ps1            一键初始化：clone 仓库 + 装依赖 + 下载固件
├─ README.md                本文件
├─ clones\                  第三方仓库（bootstrap 自动 clone，勿手动改）
│  ├─ lps-node-firmware\    节点固件源码 + 官方 sniffer 解码脚本（tools/sniffer/）
│  ├─ lps-tools\            官方配置/烧录工具（含 DfuSe 烧写实现，被本工程复用）
│  └─ crazyflie-firmware\   参考：TDoA 引擎 / 时钟修正 / EKF 观测模型的权威实现
├─ tools\                   本工程的工具脚本
│  ├─ lps_provision.py      一键：刷固件 + 配置基站/标签 + 编号 + 自检   ← 最常用
│  ├─ fix_serial_driver.ps1 修复"节点没有 COM 口"（libusb 抢占 CDC 接口）
│  ├─ git_setup.ps1         把本工程初始化成 git 仓库并推送 GitHub
│  ├─ lps_flash.py          刷固件（串口进 DFU + DfuSe 烧写）
│  ├─ lps_config.py         配置节点（ID / 模式 / 射频 / 功率）
│  ├─ lps_capture.py        从 Sniffer 节点抓原始数据
│  ├─ lps_tdoa3_solver.py   主机侧解算：sniffer 数据 → 三维位置
│  ├─ make_fake_capture.py  生成假抓包（无硬件自检用）
│  ├─ test_solver_sim.py    算法仿真验证
│  └─ anchors_example.yaml  锚点坐标模板
├─ docs\                    原理与操作手册
│  ├─ 命令速查.md             ★ 刷写/配置/查看 三类命令速查
│  ├─ 刷写与配置SOP.md        ★ 刷机+配置的标准流程（含按键/复位/LED 判读）
├─ firmware\                官方 .dfu 固件
├─ captures\                抓包数据（*.bin 可被解算器 --input 回放）
└─ logs\
```

---

## 一次性初始化

```powershell
cd D:\_AAA_CODE_ALL\UWB
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1

# 想额外拉取第三方参考实现（DW1000-TDoA3 / MultilaterationTDOA / libdw1000）：
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -WithRefs
```

脚本做的事：`git clone` 三个仓库 → `pip install pyserial pyusb numpy pyyaml` →
editable 安装 lps-tools（提供 GUI 和 DfuSe 实现）→ 确保固件在 `firmware\`。
可反复运行，已存在会跳过。

---

## 无硬件先自检（强烈建议第一步）

```powershell
python tools\make_fake_capture.py
python tools\lps_tdoa3_solver.py --input captures\fake_tdoa3.bin --anchors captures\fake_anchors.yaml
```

预期：解算位置 ≈ `(2.700, 2.199, 0.998)`，真值 `(2.700, 2.200, 1.000)`，误差 < 1 cm。

这一步验证的是整条软件链路：sniffer 帧解析 → TDoA3 解包 → 时钟修正 → TDoA → 三维解算。
如果这步不过，硬件接上也白搭，先查 Python 环境。

---

## 实际操作顺序

### 1) 看硬件

```powershell
python tools\lps_config.py --list     # 正常模式节点（0483:5740）
python tools\lps_flash.py --list      # 顺便看有没有设备停在 DFU 模式（0483:df11）
```

### 2) 一键刷写 + 配置（推荐，最常用）

**先把板子置于 DFU 刷机状态**：按住节点上的 **DFU 键（右边靠上那个）** 再插 USB；
或让脚本自己发 `u`（`--enter-dfu --port COMx`）。然后在命令行一条命令完成
"刷固件 → 配编号/角色 → 校验"：

```powershell
python tools\lps_provision.py --anchor 0        # 刷成基站，编号 0
python tools\lps_provision.py --anchor 3        # 刷成基站，编号 3
python tools\lps_provision.py --tag 20          # 刷成标签(数据出口/Sniffer)，编号 20

# 加 --test 做功能自检（标签会真的听 10 秒空口包，统计能看到几个基站）
python tools\lps_provision.py --tag 20 --test

# 基站自检：借另一块 sniffer 节点旁听，验证这块基站是否在发包
python tools\lps_provision.py --anchor 0 --test --with-sniffer COM7

# 只预检不写入
python tools\lps_provision.py --anchor 0 --dry-run

# 交互式批量刷 8 个基站（每次插一块，自动分配 0-7）
python tools\lps_provision.py --anchors 0-7
```

> **刷写耗时**：默认走**官方原速**（严格遵守 STM32 上报的 5 秒等待），一块约 **11 分钟**。
> 这是实测最稳的策略；把等待压短会在写入中途失败。
> 想提速可加 `--wait-cap 500`（约 1 分钟，有风险，失败后按住 DFU 键重新上电即可重来）。

脚本会在配置后提示重启（**按一下 Reset 键（右边靠下那个）或拔插 USB**，两者等价），
随后自动回读开机信息校验：地址是否为设定值、模式是否正确、开机自检有没有 `[FAIL]`。

> 完整的按键、复位、LED 判读、批量清单、故障处理见 **`docs\刷写与配置SOP.md`**。

> 角色与模式的对应：`--anchor` → `TDoA Anchor V3`；`--tag` → `Sniffer`
> （LPS Node 固件没有 TDoA 标签模式，本方案里"标签"就是那个被动监听的数据出口节点）。
> 需要别的角色时用 `--mode`：`tdoa2` / `twr-anchor` / `twr-tag` / `sniffer`。

### 2-备选) 只刷固件（不做配置）

```powershell
# 预检（解析固件 + 检查 DFU 设备状态，不写入）
python tools\lps_flash.py --dry-run

# 单个节点
python tools\lps_flash.py --port COM5

# 交互式批量（每次插一个）
python tools\lps_flash.py --all
```

`lps_flash.py` 会自动让节点进入 DFU 模式并烧写，用的就是官方 GUI 的实现。
若报 `No DfuSe compatible device`，说明 `0483:df11` 没绑定驱动，用 Zadig 装
WinUSB/libusb 驱动，或直接双击官方 GUI `lpstool.exe` 刷。

### 3-备选) 只配置节点角色（不刷固件）

```powershell
# 查看当前配置
python tools\lps_config.py --port COM5 --read

# 8 个锚点逐个配置
python tools\lps_config.py --port COM5 --id 0 --mode tdoa3
python tools\lps_config.py --port COM6 --id 1 --mode tdoa3
# ...

# 数据出口节点配成 Sniffer
python tools\lps_config.py --port COM7 --id 20 --mode sniffer

# 或者用交互式批量编号（每次插一个节点，自动分配 ID）
python tools\lps_config.py --assign 0-7 --mode tdoa3
```

⚠️ **配置改完必须断电重启**（拔插 USB），否则不生效。

可用模式：`twr-anchor` / `twr-tag` / `sniffer` / `tdoa2` / `tdoa3`。
所有锚点与 Sniffer 的射频参数（比特率、前导码）必须一致——用 `--radio` 或串口菜单 `r` 设置。

### 4) 布点并量坐标

- 锚点离墙/天花板 ≥ 15 cm，间距 ≥ 2 m，不同高度
- 8 锚点推荐"上下各 4 个"构成长方体包络
- 右手坐标系、角落做原点、量 **UWB 天线相位中心**
- 把坐标填进 `tools\anchors_example.yaml`（复制一份改成自己的）

### 5) 抓包联调（关键验收）

```powershell
# 抓 30 秒并打印每个锚点的收包统计
python tools\lps_capture.py --port COM7 --seconds 30 --summary

# 回放抓包做自检
python tools\lps_tdoa3_solver.py --input captures\cap_xxxx.bin ^
       --anchors tools\anchors_example.yaml --check
```

验收要点：

1. 每个锚点都出现，收包率稳定，没有节点长期"最近出现"时间很大
2. **锚点间距离要和卷尺实测吻合**——用官方解码脚本验证：

```powershell
cd clones\lps-node-firmware
python tools\sniffer\sniffer_binary.py COM7 yaml | python tools\sniffer\tdoa3_decoder.py
python tools\sniffer\sniffer_binary.py COM7 yaml | python tools\sniffer\tdoa3_decoder.py | python tools\sniffer\tdoa3_tof.py m
```

3. 距离稳定不跳变，丢包率可接受

### 6) 实时解算位置

```powershell
# 自检模式（看时钟修正是否收敛、各锚点收包情况）
python tools\lps_tdoa3_solver.py --port COM7 --anchors tools\anchors_example.yaml --check

# 实时输出位置
python tools\lps_tdoa3_solver.py --port COM7 --anchors tools\anchors_example.yaml
#   [   3.42s] pos=(  1.234,  -0.567,   0.891) m  meas= 14  rms=0.043 m

# JSON 行输出（对接 PX4 / ROS）
python tools\lps_tdoa3_solver.py --port COM7 --anchors tools\anchors_example.yaml --json
```

### 7) 怎么验证节点是否正常工作（三个层次）

**层次 1：单板自检（刚刷完就能做，不需要别的板子）**

```powershell
python tools\lps_config.py --port COM5 --read
# 或
python tools\lps_provision.py --tag 20 --skip-flash --port COM5 --test
```

合格标准：

| 检查项 | 期望 |
|---|---|
| 开机自检 | 压力传感器 / EEPROM / UWB 全是 `[OK]`，没有 `[FAIL]` / `[ERROR]` |
| 地址 | `Address is 0x..` 等于你设定的编号 |
| 模式 | `Mode is TDoA Anchor V3`（基站）或 `Mode is Sniffer`（标签） |
| 射频参数 | `Bitrate: normal` / `Preamble: normal`，**所有节点必须一致** |

**层次 2：射频收发自检（验证它真的在收/发 UWB）**

标签/sniper 节点：

```powershell
python tools\lps_capture.py --port COM7 --seconds 30 --summary
```

- 应该看到每个基站 ID 的收包数与收包率；`TDoA3 包(0x30)` 应该占绝大多数
- 可见基站 ≥ 4 个才可能做三维定位；有个别基站 `最近出现` 很大说明它没发包或被遮挡

基站节点（自己不上报，需要一块 sniffer 旁听）：

```powershell
python tools\lps_provision.py --anchor 0 --skip-flash --port COM5 --test --with-sniffer COM7
```

在 sniffer 的输出里应该能看到该基站的 ID。看不到就说明这块基站没在发包
（没上电 / 模式不对 / 射频参数不一致）。

**层次 3：定位精度验收（系统级）**

```powershell
# 1) 锚点间距离 vs 卷尺实测（验证测距链路）
cd clones\lps-node-firmware
python tools\sniffer\sniffer_binary.py COM7 yaml | python tools\sniffer\tdoa3_decoder.py | python tools\sniffer\tdoa3_tof.py m

# 2) 把 sniffer 放在已知坐标点，看解算值
cd D:\_AAA_CODE_ALL\UWB
python tools\lps_tdoa3_solver.py --port COM7 --anchors tools\anchors_example.yaml
```

| 检查项 | 合格标准 |
|---|---|
| 锚点间距离 | 与卷尺实测差 < 0.2 m，且几分钟内稳定 |
| 时钟修正 | `--check` 输出里每个基站 `cc` 偏离 1 不超过 ±1e-5 |
| 静态定位误差 | 已知点 < 20 cm |
| 静态重复性 | 同一点 1 分钟，标准差 < 5 cm |
| 动态跟随 | 拿着走直线，轨迹平滑无锯齿 |

---

## 原理速查

锚点之间互相做双向测距，每个锚点在包里带上"最近收到的其它锚点的接收时间戳 + 锚点间距离"；
Sniffer 只收听，用自己本地的接收时间戳算出 TDoA。

```
δtx(锚点i时钟) = tof(i←j) + (tx_i − rx_i(j))
δrx(标签时钟)  = rx_T(i) − rx_T(j)
TDoA = δrx − α_i · δtx                            # α_i 是标签时钟/锚点i时钟的漂移系数
d(P,A_i) − d(P,A_j) = c · TDoA / 63897600000      # ≈ 4.6918 mm/tick
```

解算器里的时钟修正逐行对照 `clones\crazyflie-firmware\src\utils\src\clockCorrectionEngine.c`，
TDoA 计算对照 `...\utils\src\tdoa\tdoaEngine.c` 的 `calcTDoA()`。

---

## 排查思路

| 现象 | 处理 |
|---|---|
| `python`/`python3` 找不到 | Windows 上没有 `python3`，用 `python` |
| 脚本报缺 `serial`/`usb` | 重跑 `bootstrap.ps1`，或 `python -m pip install pyserial pyusb numpy pyyaml` |
| 抓不到包 | 节点是否真是 Sniffer 模式；锚点是否上电；射频参数是否一致 |
| 位置解不出来 | 锚点数/测量数不足（< 4）；`anchors.yaml` 的 ID 与实际不符；TDoA3 的 `distance` 字段为空 |
| `distance` 字段一直为空 | 锚点间测距没建立（等几十秒让时钟修正收敛）；仍不行就切 TDoA2 模式 |
| 位置整体偏移 | 锚点坐标量测误差、天线延迟差异，做一次已知点标定 |
| 位置镜像/反号 | 检查右手系；用解算器 `--sign -1` 试 |
| 刷写报找不到 DFU | 用 Zadig 给 `0483:df11` 装驱动，或用官方 GUI |

更详细的内容见 `docs\LPS-TDoA-完整部署操作手册.md`（含验收矩阵、固件改造方案、源码细节）。

---

## 许可与来源

- `clones\` 下所有代码版权归 Bitcraze AB 及各自作者，遵循其原始许可
  （lps-node-firmware: LGPL-3.0，lps-tools: MIT，crazyflie-firmware: GPL-3.0）
- `tools\` 下的脚本为本工程自研，可自由使用；其中 TDoA/时钟修正算法移植自
  Bitcraze 的 GPL/LGPL 代码，**商用前请评估许可影响**
