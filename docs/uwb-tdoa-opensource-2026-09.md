# UWB TDOA 开源方案调研（2026-09-14）

## 0. 一句话结论

**没有"开箱即用、商业级成熟"的纯开源 TDOA UWB 全栈产品**，但有 3 条真正能落地的路径：

1. **要最成熟、还在维护、能直接买硬件跑起来的** → **Bitcraze Loco Positioning System（LPS）**，它有真正的 TDoA 2 / TDoA 3 模式，固件、上位机、ROS 驱动、数据集齐全，LGPL-3.0，仓库 2026-09 仍在提交。
2. **要学术级、代码干净、MIT 可商用修改的 TDOA 实现** → **TIERS（芬兰）`dynamic-uwb-firmware` + `active-passive-uwb-ros`**，DWM1001 上做"有源 ToF + 无源 TDoA 监听"，MIT 许可。
3. **要中文资料 + 后端解算服务器全链路** → **`shinetree/UWB_TDOA_STATION/TAG/GATEWAY` + `sunwu51/ihat_location`**，DW1000 + STM32F103 + 网关 + Java 后台，是国内最完整的一套，但**2018 年后停更**。

**避开一个误区**：GitHub 上 100+ 星的 UWB 项目绝大多数是 **TWR（双向测距）**，不是 TDOA。TDOA 相关的高星项目反而少，因为 TDOA 的难点不在测距，而在**锚点间时钟同步 + 锚点自定位**。

---

## 1. 第一梯队：可直接复用的 TDOA 全栈/半全栈

| 项目 | 覆盖范围 | 硬件 | 是否真 TDOA | 许可 | 活跃度 | 成熟度评价 |
|---|---|---|---|---|---|---|
| [bitcraze/lps-node-firmware](https://github.com/bitcraze/lps-node-firmware) | 锚点固件（TWR / TDoA2 / TDoA3 / Long Range 四种模式） | DWM1000（Loco Positioning Node，STM32F072+nRF51） | ✅ TDoA2/TDoA3 | LGPL-3.0 | 2016 创建，**2026-09 仍在更新**，88★ | **最高**。唯一"有产品在卖、有教程、有 CI、有社区"的开源 TDOA 系统 |
| [bitcraze/crazyflie-firmware](https://github.com/bitcraze/crazyflie-firmware) | 标签(tag)侧 TDoA2/TDoA3 解算，机载实时定位 | Loco Positioning Deck (DWM1000) | ✅ | GPL-3.0 | 1547★，2026-09 活跃 | 机载端 TDOA 解算的**最佳参考实现**（含滤波器、锚点自动识别） |
| [TIERS/dynamic-uwb-firmware](https://github.com/TIERS/dynamic-uwb-firmware) | DWM1001 固件：有源 ToF + **无源 TDoA 监听** 混合 | Decawave DWM1001-DEV | ✅ 被动 TDoA | **MIT** | 2022，13★ | 代码小、可读、许可宽松，最适合**当参考实现读**再自研 |
| [TIERS/active-passive-uwb-ros](https://github.com/TIERS/active-passive-uwb-ros) | ROS 节点：解析 TDoA 差值、解算坐标、发布话题 | 同上一套 | ✅ | MIT | 2022 | 上位机解算 + ROS 集成，配合上一仓库使用 |
| [shinetree/UWB_TDOA_STATION_V1_1](https://github.com/shinetree/UWB_TDOA_STATION_V1_1) | 基站固件 | DW1000 + STM32F103 | ✅ | 无 license 声明 | 2018，停更 | 国内"三件套"之一 |
| [shinetree/UWB_TDOA_TAG_V1_1](https://github.com/shinetree/UWB_TDOA_TAG_V1_1) | 标签固件 | 同上 | ✅ | 无 | 2018，停更 | 同上 |
| [shinetree/UWB_TDOA_GATEWAY_V1_1](https://github.com/shinetree/UWB_TDOA_GATEWAY_V1_1) | 汇聚网关（多基站→服务器） | 同上 | ✅ | 无 | 2018，停更 | 有"有线同步/汇聚"的完整链路 |
| [sunwu51/ihat_location](https://github.com/sunwu51/ihat_location) | **后台解算服务**（SpringBoot，TDOA 定位引擎） | 服务器 | ✅ | 无 | 2018，54★ | 中文项目里唯一成体系的**服务端**，可直接改造 |
| [dy-dx-1/DW1000-TDoA3](https://github.com/dy-dx-1/DW1000-TDoA3) | 把 Bitcraze 的 TDoA3 算法搬到普通 DW1000 标签上 | 通用 DW1000 模块 | ✅ TDoA3 | GPL-3.0 | 2026-09 新仓库 | 很新、很小，但**方向对**：让 LPS 的 TDoA3 摆脱 Bitcraze 硬件 |

### Bitcraze LPS 为什么值得当基线

- **三种定位模式**官方明确区分（[官方文档](https://www.bitcraze.io/documentation/system/positioning/loco-positioning-system/)）：
  - **TWR**：标签依次 ping 锚点，最准，但同时只能定位 1 个标签、最多 8 锚点。
  - **TDoA 2**：**锚点系统持续广播同步包，标签只被动收听**，用到达时间差解算相对距离；扩展性极好（标签数量不受限），但锚点被**时隙化并同步**，上限 8 锚点，且标签最好待在锚点围成的空间内。推荐 8 个锚点放房间四角。
  - **TDoA 3**：把时隙方案换成**随机发送调度**，锚点数不再卡 8，可跨房间/无全连通，支持锚点动态增删；另有 **Long Range 模式**（降速率换距离，代价是位置噪声变大）。
- **精度**：基于 DWM1000，官方标称 **10 cm 量级**；TDoA2 在锚点包络内精度与 TWR 相当。
- **许可**：锚点固件 LGPL-3.0，Crazyflie 固件 GPL-3.0（**商用需注意 GPL/LGPL 传染性**，这是选型的硬约束）。
- **硬件在售**：Loco Positioning Node / Loco Positioning deck / Indoor Explorer bundle 仍可购买，不是"考古项目"。

---

## 2. 第二梯队：TDOA 的关键模块（驱动 / 同步 / 锚点自定位）

TDOA 的坑几乎全在这三块，而这些模块可以单独取用：

| 模块 | 仓库 | 作用 | 许可 |
|---|---|---|---|
| DW1000 Arduino 驱动（最流行） | [thotro/arduino-dw1000](https://github.com/thotro/arduino-dw1000) 574★ / [F-Army/arduino-dw1000-ng](https://github.com/F-Army/arduino-dw1000-ng) 133★ | 时间戳、TWR 原语、寄存器抽象；TDOA 需要自己往上加同步层 | Apache-2.0 / MIT |
| DW3000 现代驱动 | [br101/libdeca](https://github.com/br101/libdeca) / [br101/zephyr-dw3000-decadriver](https://github.com/br101/zephyr-dw3000-decadriver) / [br101/dw3000-decadriver-source](https://github.com/br101/dw3000-decadriver-source) | DW3000（DWM3000）多平台驱动，Zephyr 原生集成，比 DW1000 生态新且维护活跃 | LGPL-3.0 / ISC |
| DW3000 Arduino/ESP32 | [foldedtoad/dwm3000](https://github.com/foldedtoad/dwm3000) / [Fhilb/DW3000_Arduino](https://github.com/Fhilb/DW3000_Arduino) / [Makerfabs ESP32-UWB-DW3000](https://github.com/Makerfabs/Makerfabs-ESP32-UWB-DW3000) | 快速原型、示例工程（多为 TWR） | GPL-3.0 / MIT / 无 |
| **分布式时间同步（TDOA 核心）** | [Decawave/mynewt-timescale-lib](https://github.com/Decawave/mynewt-timescale-lib) | Decawave 官方 **Timescale** 库：锚点间建立统一时间尺度，正是 TDOA 需要的那一层。Apache-2.0，**2020 停更** | Apache-2.0 |
| **无线时间同步 + 泛洪协议（科研向）** | [d3s-trento/contiki-uwb](https://github.com/d3s-trento/contiki-uwb) | Trento 大学的 Contiki 移植，含 **Glossy / Crystal / Weaver** 协议 + 天线延迟、SS-TWR/DS-TWR 原语。想自研"无线同步 TDOA"这是最扎实的参考 | 见仓库 |
| **天线延迟自动标定** | [ds-kiel/aladin-uwb](https://github.com/ds-kiel/aladin-uwb) | ALADIn：资源受限 UWB 设备上的 all-to-all 线性天线延迟推断，直接消除 TDOA 的系统性偏差 | MIT |
| 锚点自定位（TDOA 系统装好后自动算锚点坐标） | [bitcraze/lps-anchor-pos-estimator](https://github.com/bitcraze/lps-anchor-pos-estimator) | 用测距样本反解锚点坐标（已在 2021 归档，但算法可用） | LGPL |
| 官方参考栈（Mynewt） | [Decawave/uwb-core](https://github.com/Decawave/uwb-core) / [Decawave/uwb-apps](https://github.com/Decawave/uwb-apps) / [Decawave/dwm1001-examples](https://github.com/Decawave/dwm1001-examples) | Qorvo/Decawave 官方 HAL/MAC/Ranging Services + 示例；示例以 TWR 为主，但 MAC 层可扩展 | Apache-2.0 / 无 |
| DWM3001CDK 干净固件 | [Uberi/DWM3001CDK-demo-firmware](https://github.com/Uberi/DWM3001CDK-demo-firmware) / [Uberi/DWM3001C-starter-firmware](https://github.com/Uberi/DWM3001C-starter-firmware) | 把官方 DWM3001CDK 固件重写干净、示例齐全（TWR/PDoA 为主），是 DW3000 平台最好的起点 | 无 license 声明 |

### 硬件侧值得一起看

- **Ai-Thinker BU01（DW1000）/ BU03（DW3000）**：国内最便宜的 UWB 模组，官方明确说明可用于 **TWR / TDOA / PDOA** 定位。示例仓库 [zhugezuanzuan/AiThinker_UWB_03](https://github.com/zhugezuanzuan/AiThinker_UWB_03)（GPL-3.0）。
- **KitSprout 系列**（[UWB-Node](https://github.com/KitSprout/UWB-Node) 162★、[UWB-Adapter](https://github.com/KitSprout/UWB-Adapter)、[KDWM1000](https://github.com/KitSprout/KDWM1000)）：完整的开源硬件 + 固件开发套件（STM32F411 + DWM1000），适合做自制锚点/标签底板。
- **Makerfabs ESP32-UWB-DW3000**（169★）：ESP32 + DW3000 现成板，社区示例最多。
- **Bitcraze Loco Positioning Node / Deck**：唯一"TDOA 开箱可用"的商业硬件。

---

## 3. 解算算法与数据集（不必自研的部分）

| 用途 | 资源 | 说明 |
|---|---|---|
| 定位算法全家桶 | [cliansang/positioning-algorithms-for-uwb-matlab](https://github.com/cliansang/positioning-algorithms-for-uwb-matlab) 134★, MIT | LS / WLS / NLS / SDP / ML 五类算法，TWR 与 TDOA 都覆盖，配套论文 |
| TDOA 专用算法 | [vineeths96/TDOA-Localization](https://github.com/vineeths96/TDOA-Localization)（IISc，含实测数据与 CRLB 对比）、[petrokn/LocalizationTDOA](https://github.com/petrokn/LocalizationTDOA)、[StevenJL/tdoa_localization](https://github.com/StevenJL/tdoa_localization) 137★ | Chan / Taylor / 泰勒展开 / 最小二乘 |
| 抗 NLOS 的凸优化解法 | [xmuszq/Semidefinite-Programming-SDP-optimization](https://github.com/xmuszq/Semidefinite-Programming-SDP-optimization)、[XenoHikari/MSCF-IRLS-UWB-Localization](https://github.com/XenoHikari/MSCF-IRLS-UWB-Localization) | SDP 抗 NLOS 误差、鲁棒 IRLS |
| **Bitcraze LPS 实测 TDOA 数据集** | [Williamwenda/UWB_TDOA_dataset](https://github.com/Williamwenda/UWB_TDOA_dataset)（43★） | 真机 TDOA 测量数据，可直接调算法 |
| 用 CIR 做 TDOA 校正 | [dietercoppens/UWB_DW1000_TDOA_CIR](https://github.com/dietercoppens/UWB_DW1000_TDOA_CIR)（2025） | 信道冲激响应指纹 / TDOA 修正数据集 |
| 多机器人 / 融合定位 | [TIERS/uwb-cooperative-mrs-localization](https://github.com/TIERS/uwb-cooperative-mrs-localization)、[TIERS/uwb-drone-dataset](https://github.com/TIERS/uwb-drone-dataset)、[KIT-ISAS/SFUISE](https://github.com/KIT-ISAS/SFUISE)（UWB-惯性紧耦合） | 蒙特卡洛多机器人定位、UWB+IMU 融合 |
| ROS 跟踪示例 | [cliansang/uwb-tracking-ros](https://github.com/cliansang/uwb-tracking-ros)（MIT，TREK1000/MDEK1001） | 标定 + 跟踪 + 可视化 |

---

## 4. TDOA 落地必须解决的 5 个工程问题

1. **锚点间时钟同步**（最核心，决定成败）
   - 有线同步：Sewio 等商用方案，精度最好，布线成本高。
   - 无线同步：Bitcraze TDoA2（时隙 TDMA + 同步包）/ TDoA3（随机调度 + 自组织）；Trento 的 Glossy/Crystal 泛洪同步。
   - 参考标签法：用固定参考标签做时钟偏差估计，成本低但精度略差。
2. **锚点自定位**：部署后不能靠卷尺量坐标，需要用 UWB 自身测距反解锚点位置（见 lps-anchor-pos-estimator）。
3. **天线延迟标定**：每个节点的 RF 延迟不一致会直接变成 TDOA 系统偏差，需要 all-to-all 标定（见 ALADIn）。
4. **时钟漂移与晶振温度漂移**：需要周期性重同步 + 漂移估计（线性拟合/卡尔曼），否则长时间会累积误差。
5. **多径与 NLOS + 扩展性**：室内金属反射、人体遮挡；TDOA3 的随机调度就是为了在更多锚点下保持鲁棒。算法侧配 SDP/鲁棒 M 估计 + EKF/UKF 融合。

---

## 5. 两条推荐的落地路线

### 路线 A：站在 Bitcraze 肩上（最快出成果，2~4 周）

买 Loco Positioning Node ×8 + 标签侧（LP Deck 或自制 DWM1000 标签）→ 刷 `lps-node-firmware` 开 TDoA3 → 用 `crazyflie-lib-python` 或 `dy-dx-1/DW1000-TDoA3` 拿位置。

- 优点：唯一"真·开箱可用"的 TDOA 开源栈，精度 10 cm 级，社区成熟。
- 缺点：LGPL/GPL 传染性要评估；锚点上限（TDoA2 为 8）；硬件是 DWM1000 一代。
- 适合：无人机编队、机器人室内定位、科研验证。

### 路线 B：自建 DW3000 + Zephyr（可控、可商用，2~4 个月）

`Makerfabs ESP32-UWB-DW3000` 或 `BU03` 硬件 + `br101/libdeca`（Zephyr，ISC/LGPL）→ 参考 `Decawave/mynewt-timescale-lib` 与 Trento `contiki-uwb` 设计同步层 → 解算用 `cliansang` 的算法移植到 C/Python → 后端上 MQTT + TimescaleDB/InfluxDB + Grafana（或直接改 `sunwu51/ihat_location`）。

- 优点：许可可控、DW3000 性能更好、可商用。
- 缺点：同步层和锚点自定位要自己写（这是 80% 的工作量）。
- 适合：要做产品的团队。

---

## 6. 时效性提醒

- 高星 ≠ 好用：`thotro/arduino-dw1000`(574★)、`KitSprout/UWB-Node`(162★) 都是 2019-2024 停更的库/硬件，且**只做 TWR**。
- `Decawave/*` 官方仓库大多 **2020-2021 停更**（公司已被 Qorvo 收购，最新栈转向 DWM3001CDK + Qorvo 官方 SDK，开源程度下降）。
- 唯一**今天仍在活跃维护**的 TDOA 开源系统是 **Bitcraze LPS**。
- 新变量：2025-2026 年出现 `br101` DW3000 驱动族、`dy-dx-1/DW1000-TDoA3`、`dietercoppens/UWB_DW1000_TDOA_CIR`，说明 TDOA 开源生态在 DW3000 平台上正在重新长起来，但目前都还不够"成熟"。
