# LPS Node 刷固件 + 配置 标准作业流程（SOP）

> 适用范围：Bitcraze Loco Positioning Node（STM32F072 + DWM1000）
> 固件版本：`lps-node-firmware-2022.09.dfu`
> 依据：官方产品页机械说明、`lps-node-firmware` 源码、`lps-tools` 源码核实

---

## 0. 一个必须先搞清楚的认知

**基站和标签刷的是同一份固件，区别只在"配置"（模式）里。**

| | 固件 | 模式配置 | 作用 |
|---|---|---|---|
| **基站（Anchor）** | 同一份 `lps-node-firmware-2022.09.dfu` | `TDoA Anchor V3` | 固定不动，互相测距并广播时间戳 |
| **标签（Tag / 数据出口）** | 同一份 | `Sniffer` | 纯被动监听，把带硬件时间戳的原始包吐给主机 |

所以刷写命令对两者是一样的，只有 `--anchor` / `--tag` 这一项不同。

> 补充：LPS Node 固件**没有** TDoA 标签模式。官方定义的"Tag"指的是 TWR 标签（`twr-tag`，一次只能一个、只输出距离不输出位置）。本方案里承担"标签"角色的是 **Sniffer 模式**的节点。

---

## 1. 硬件认识（官方机械说明）

### 接口与按键

| 编号 | 类型 | 说明 |
|---|---|---|
| 1 | micro-USB | 供电 + 通信（**刷机和配置都走这里**） |
| 2 | DC-jack | 5V 供电 |
| 3 | 接线端子 | 5–12V 供电 |
| **4** | **按键** | **Reset（复位）**——配置改完后按它即可生效 |
| 5 | ESP8266 焊盘 | 未贴装 |
| **6** | **按键** | **DFU 键**：**按住它在插 USB（或按住它再按 Reset）就进入刷机模式**；系统运行时此键无效 |
| 7 | SWD | 调试口 |
| 8 | FTDI 串口 | 未贴装，3V3 电平 |
| 9 | M3 螺孔 | 安装孔 |

> 板上右侧有两个按键。按官方工具里的图示：**上面那个是 DFU 键（6），下面那个是 Reset 键（4）**
> （不同硬件批次位置可能有差异）。分辨不了没关系，用这个办法确认：
> **按住其中一个不放，插 USB；若 `python tools\lps_flash.py --list` 显示 `0483:df11`，
> 它就是 DFU 键；没有就换另一个按键试。**

### LED 含义（现场判读全靠它）

| LED | 颜色 | 含义 |
|---|---|---|
| POWER | 蓝 | 板子已供电 |
| MODE | 黄 | **常亮 = Anchor 模式；熄灭 = Tag 模式；闪烁 = Sniffer 模式** |
| RANGING | 红 | 测距进行中（闪烁） |
| SYNC | 绿 | 同步指示 |
| TX | 红 | DWM1000 正在发送 |
| RX | 绿 | DWM1000 正在接收 |
| SFD | 黄 | 检测到 UWB 前导码 |
| RXOK | 红 | 收到无错包 |

**只用 LED 就能判断配置是否生效**：改完按 Reset，
看 MODE 灯——常亮就是基站、闪烁就是 Sniffer。

---

## 2. 进入刷机（DFU）模式的两种方法

### 方法 A：硬件按键（官方手动流程，推荐，不依赖软件）

```
1. 拔掉节点 USB 线
2. 按住板上的 DFU 键（编号 6，靠上的按键）不要松手
3. 保持按住的同时插入 USB 线
4. 插入后再松手
5. 确认：设备管理器出现 "STM32 BOOTLOADER"，或运行 python tools\lps_flash.py --list
         看到 0483:df11（此时节点不出串口，只有 POWER 灯亮）
```

> 已经插着 USB 时也可以：**按住 DFU 键 → 按一下 Reset 键 → 松开 DFU 键**，效果相同。

### 方法 B：软件命令（节点当前能出串口时）

节点正在运行、系统能识别到它的 COM 口时，发一个字符 `u` 即可让它重启进 DFU：

```powershell
python tools\lps_provision.py --anchor 0 --enter-dfu --port COM17     # 刷写前自动进入
# 或
python tools\lps_flash.py --port COM17                                # 同样会自动进 DFU
```

### 判断当前状态

```powershell
python tools\lps_flash.py --list
```

| 输出 | 含义 |
|---|---|
| `0483:df11  STM32 BOOTLOADER` | 已处于刷机模式 ✓ 可以刷 |
| `COM17 USB 串行设备` | 节点在运行，可直接配置（或加 `--enter-dfu` 刷写） |
| 两者都没有 + 提示"USB 上检测到 0483:5740 但无串口" | 驱动被 libusb 占用，见第 8 节 |

---

## 3. 刷写命令

**基站**（把 `<ID>` 换成 0–7；8 个基站就是 0…7）：

```powershell
python tools\lps_provision.py --anchor 0
python tools\lps_provision.py --anchor 1
# ...
python tools\lps_provision.py --anchor 7
```

**标签（数据出口）**：

```powershell
python tools\lps_provision.py --tag 20
```

> 如果节点还在运行、没手动进 DFU，加 `--enter-dfu --port COMx`：
> `python tools\lps_provision.py --anchor 0 --enter-dfu --port COM17`

### 刷写阶段你会看到什么

```
  [OK]   检测到 DFU 设备（0483:df11）
  [--]   固件：lps-node-firmware-2022.09.dfu（目标地址 0x08000000，90016 字节）
  [--]   刷写 lps-node-firmware-2022.09.dfu（90325 字节）
         [#########################] 100%   （用时约 7～15 秒）
  [OK]   节点已重启，串口 = COM17
```

> **关于刷写时间（重要）**：脚本默认采用**官方原速**——严格按 STM32 上报的
> `bwPollTimeout`（实测 5000 ms）等待，一次刷写 **约 11 分钟**。
> 这是最稳的策略：把等待压到 50ms 会在写到 14% 左右中途失败（已实测）。
> 想加速可以用 `--wait-cap 500`（约 1 分钟），但有中途失败风险；
> 中途失败不会变砖，按住 DFU 键重新上电即可重来。

---

## 4. 刷完之后的固定动作（每一步都别跳）

刷写完成的瞬间，DfuSe 会让芯片跳到新固件，但 **Windows 有时不会立刻重新枚举 USB**，
所以固定做下面这一套动作：

```
① 拔掉 USB 线
② 等 2 秒
③ 重新插上 USB（不要按 DFU 键！按了就又进刷机模式了）
④ 确认枚举成功：
      python tools\lps_config.py --list      → 应该列出 COMx
      或看 LED：MODE 灯按模式常亮/闪烁
```

替代方案（不想拔插）：**按一下 Reset 键（编号 4，靠下的按键）**，同样是复位重启，
效果等价、更快。固件已经烧进去了，按 Reset 不会回到刷机模式。

> 注意：**配置改完后也需要一次复位**（按 Reset 或拔插），否则 EEPROM 里的新配置不生效。

---

## 5. 配置命令与配置内容

配置和刷写在同一条命令里完成（`lps_provision.py` 会先刷、再配、再校验）。
**如果固件已经刷好，不想重复刷**，加 `--skip-flash`：

```powershell
# 基站：编号 0，模式 TDoA Anchor V3
python tools\lps_provision.py --anchor 0 --skip-flash --port COM17

# 标签：编号 20，模式 Sniffer
python tools\lps_provision.py --tag 20 --skip-flash --port COM17
```

### 配置清单

| 项目 | 基站 | 标签 | 说明 |
|---|---|---|---|
| 编号（ID） | 0–7 各不相同 | 20（不参与定位即可） | 写入 EEPROM，掉电保存 |
| 模式 | `TDoA Anchor V3` | `Sniffer` | `--mode` 可覆盖 |
| 比特率 | `normal` | `normal` | **所有节点必须一致** |
| 前导码 | `normal` | `normal` | **所有节点必须一致** |
| 锚点坐标 | 不用写进节点 | 不用写 | 坐标放在主机侧 `tools/anchors_example.yaml` |
| 发射功率 | 默认 SmartPower 开 | 默认 | 现场不够再加 `--radio` / `--power` 调 |

### 配置阶段你会看到什么

```
  [OK]   已发送：编号 = 0，模式 = tdoa3

  >> 请拔插一次该节点的 USB（配置需要重启才生效），然后按回车继续校验
     按回车继续（Ctrl+C 放弃校验）...
```

**这个提示也可以直接按节点上的 Reset 键**（编号 4），不必真的拔插。
按完复位再回车，脚本会回读校验：

```
  [OK]   地址(ID) = 0
  [OK]   模式 = TDoA Anchor V3
  [OK]   开机自检全部通过（6 个 [OK]）
  [--]   比特率 normal / 前导码 normal
```

---

## 6. 验证：这块板子到底成不成

### 层次 1：LED + 串口（每块板子都做）

```powershell
python tools\lps_config.py --port COM17 --read
```

| 检查项 | 期望 |
|---|---|
| 开机自检 | 全部 `[OK]`，无 `[FAIL]` / `[ERROR]` |
| 地址 | 等于你设定的编号 |
| 模式 | 基站 `TDoA Anchor V3`；标签 `Sniffer` |
| MODE LED | 基站常亮 / Sniffer 闪烁 |

### 层次 2：射频收发（基站要借标签验证）

```powershell
# 标签自己监听空口，统计能看到几个基站
python tools\lps_provision.py --tag 20 --skip-flash --port COM17 --test

# 或直接抓包
python tools\lps_capture.py --port COM17 --seconds 30 --summary
```

判定：每个基站 ID 都出现、收包率稳定、`TDoA3 包(0x30)` 占多数、**可见基站 ≥ 4 个**。

基站自己不上报数据，要借标签旁听来验证它在发包：

```powershell
python tools\lps_provision.py --anchor 0 --skip-flash --port COM9 --test --with-sniffer COM17
```

### 层次 3：定位精度（系统级）

```powershell
python tools\lps_tdoa3_solver.py --port COM17 --anchors tools\anchors_example.yaml
```

合格标准：静态已知点误差 < 20 cm；同一位置 1 分钟标准差 < 5 cm；
锚点间距离（`tdoa3_tof.py m`）与卷尺差 < 0.2 m。

---

## 7. 批量流程（8 基站 + 1 标签）

推荐"每次只插一块"的交互式批量，编号自动分配：

```powershell
python tools\lps_provision.py --anchors 0-7 --mode tdoa3
```

它的节奏是：

```
把要配置为【基站 0】的板子置于 DFU 状态并插好 → 回车
   ↓ 刷写 + 配置
提示拔插/按 Reset → 回车 → 自动校验
   ↓
把要配置为【基站 1】的板子……（循环）
```

标签单独刷一次：

```powershell
python tools\lps_provision.py --tag 20
```

### 单块板子的手工清单（照着打勾）

```
[ ] 1. 拔掉 USB
[ ] 2. 按住 DFU 键(6) 插 USB，松手
[ ] 3. python tools\lps_flash.py --list      → 看到 0483:df11
[ ] 4. python tools\lps_provision.py --anchor <ID>        （基站）
        或 python tools\lps_provision.py --tag <ID>       （标签）
[ ] 5. 刷写 100%，脚本自动等到新串口
[ ] 6. 按 Reset 键(4) 或拔插 USB
[ ] 7. 回车让脚本校验：地址/模式/自检全 OK
[ ] 8. 看 MODE 灯：基站常亮 / Sniffer 闪烁
[ ] 9. 贴标签纸写上编号（! 别省，后面量坐标全靠它）
[ ] 10. 换下一块
```

---

## 8. 常见故障与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| `刷完后没等到新的节点串口` | Windows 没及时重新枚举 | 按 Reset 键(4) 或拔插 USB；再跑 `--list` |
| `刷写报 win error: 连接到系统的设备没有发挥作用`（0% 就失败） | 上次刷写被中断，STM32 引导停在了残留状态（`bState=5 DFU_DOWNLOAD_IDLE` / `7 DFU_MANIFEST` / `10 DFU_ERROR`）。这种状态下它拒绝新命令，libusb 就报成这个错误 | **拔掉 USB → 按住 DFU 键(6) → 插 USB → 松手**（让引导重新开始），再重跑命令。脚本会检测并提示，加 `--recover` 会先尝试软件恢复（多数情况仍需这次物理复位） |
| 换另一块板子插上后又不出现 COM 口 | 驱动绑定是**按设备实例**的：上次只给那块板子换了驱动 | 一次性全局修复：管理员运行 `tools\fix_serial_driver.ps1 -Apply` |
| `--list` 提示"USB 上有 0483:5740 但无串口" | CDC 接口被 libusb-win32/Zadig 驱动抢占（与 Crazyflie VID:PID 冲突） | 设备管理器 → libusb-win32 devices → "Crazyflie 2.x (Interface 0)" → 更新驱动程序 → 从列表选 "USB 串行设备"；或管理员执行 `pnputil /delete-driver oem35.inf /uninstall /force` 后拔插 |
| `找不到 DFU 设备` | 没进刷机模式 | 按住 DFU 键(6) 插 USB；或运行 `--enter-dfu --port COMx` |
| 串口能开但读不到配置 | 官方 GUI 正占用该串口 | 关掉 `python -m lpstools` 再试 |
| 配置读回来还是旧值 | 没复位 | 按 Reset 键(4) 或拔插 USB，再 `--read` |
| 刷写耗十几分钟 | 这是默认的官方原速策略（按 STM32 上报的 5 秒等待） | 正常现象。想提速用 `--wait-cap 500`，但有中途失败风险 |
| 刷写写到中途失败（进度不是 0%） | 等待时间压得太短，或 USB 供电/链路抖动 | 按住 DFU 键重新上电，改用默认原速；同时换后置 USB 口/短线，或用 DC 座单独供电 |
| 多个节点混着模式 | 有的 TDoA2 有的 TDoA3 | 全部用 `--read` 查一遍，统一成一种模式（官方明确：混合模式系统不工作） |
| MODE 灯不亮/板子无反应 | 供电或固件问题 | 换 USB 口/线；重新刷一次固件 |

### 刷写中断了会不会变砖？

**不会。** STM32 的 DFU 引导在芯片 ROM 里，擦不掉也改不了。
中断刷写最坏的结果是"应用被擦了一半"，表现为节点既不出串口、也不出 DFU——
这时**按住 DFU 键(6) 上电**就能回到引导模式，重新刷一遍即可恢复。

### DFU 状态速查（`python tools\lps_flash.py --dry-run` 会打印 bState）

| bState | 含义 | 能不能直接刷 |
|---|---|---|
| 2 | DFU_IDLE | ✅ 正常，直接刷 |
| 10 | DFU_ERROR | ✅ 脚本会自动清错误状态后继续 |
| 5 | DFU_DOWNLOAD_IDLE | ❌ 残留状态，需按住 DFU 键重新上电 |
| 6/7/8 | DFU_MANIFEST* | ❌ 同上 |

---

## 9. 一页速查

```
进刷机：   按住 DFU 键(6) 插 USB
刷基站：   python tools\lps_provision.py --anchor <ID>          # 0-7
刷标签：   python tools\lps_provision.py --tag <ID>             # 建议 20
只配置：   加 --skip-flash --port COMx
生效：     按 Reset(4) 或拔插 USB
校验：     python tools\lps_config.py --port COMx --read
看状态：   MODE 灯：常亮=基站  闪烁=Sniffer  熄灭=Tag
看板子：   python tools\lps_flash.py --list
批量：     python tools\lps_provision.py --anchors 0-7 --mode tdoa3
功能自检： python tools\lps_provision.py --tag 20 --skip-flash --port COMx --test
解算位置： python tools\lps_tdoa3_solver.py --port COMx --anchors tools\anchors_example.yaml
```
