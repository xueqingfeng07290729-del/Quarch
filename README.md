# Quarch：SSD、显卡、DRAM 与整机能耗测量

通过 **QTL2312 + PAM 夹具**测量设备电源轨，用 QuarchPy / headless QIS 采集并积分能耗。
SSD 使用 **QTL2980-01 M.2 夹具**，支持逐条测量 DuckDB TPC-H；显卡使用 **QTL2983-02 PCIe 夹具与 AUX 子板**，覆盖插槽和外接供电。保留此前 QTL2843 整机交流测量功能。

SSD 方案已于 **2026-11-09（UTC）**实测。夹具返回的 `QTL2621-04-006` 是内部编号，产品型号为 `QTL2980-01`。
**2026-11-11（UTC）**另完成 RTX 5050 与 QTL2983 的静息、矩阵负载及三次复测，
见[显卡实验结果](experiments/2026-11-11-rtx5050-gpu/README.md)和[接线指南](docs/setup/qtl2312-gpu.md)。
默认输出 **10 ms / 100 Hz**，已验证 **1 ms / 1,000 Hz**；模拟通道硬件基础采样率为 **250 kHz**。
SSD 的 `--channel 3.3V` 选择主供电轨，其 `Tot` 为 `3.3V + VIO_1V8`。
显卡的 `Tot` 为插槽三路与外接 `+12Vaux` 的合计。各夹具的 `Tot` 定义不同，读数均包含所测电源轨在采集窗口内的基础功耗。

DIMM1 的单块 DDR4 riser 还可使用 **QTL2312 + QTL2628 External-Shunt** 记录 CH1/R1、CH2/R5 两路压降和电流，
保留 nominal 阻值及原始 CSV，便于按实际阻值重新计算；CH3/CH4 未连接，见[DRAM 接线与电路原理](docs/setup/qtl2312-dram.md)
和[双路采集、能耗及测试指南](docs/usage/dram-shunts.md)。
该方案按用户提供的 R1/R5 标签保存，不推定 R5 是 VPP，也不将其改写为 R6。

## 当前不足与未解决问题

- **QTL2843 Ethernet 已恢复（2026-11-25）**：一次软重启并重配静态 IP 后，无 USB 的 1000 Hz / 30 秒采集和网线重连均通过。原始根因尚未完全确定，UDP 自动发现及断电后地址保持仍需验证，见[排查与恢复记录](docs/troubleshooting/ethernet-recovery-20261125.md)。
- **M.2 SSD 固定不稳**：目前无法把 SSD 可靠固定到夹具上，只能依靠插槽本身的固定能力。
- **PCIe 安装受限**：安装夹具后很难安装显卡及其他 PCIe 设备，新购夹具的 **20 cm PCIe 线**不足以满足现有布局。
- **测量结论仍有边界**：显卡 Quarch 与 NVIDIA 负载功率存在约 21–24 W 差异；SSD Q19/Q20 波动原因尚未完全厘清。成功采集和短时重复性不代表这些问题已解决。

完整现象、七项 Ethernet 排查记录、可能原因和待验证事项见[当前不足与未解决问题](docs/setup/known-limitations.md)。

## 接线与环境

- SSD 供电和 NVMe 数据：`主机 M.2 插槽 → QTL2980 夹具 → SSD`。
- 显卡插槽供电和数据：`主板 PCIe 插槽 → QTL2983 主夹具 → 显卡`。
- 显卡外接供电：`PSU 8-pin → AUX 子板 → 显卡 8-pin`；AUX 子板通过配套连接线接回主夹具。
- 测量数据：`夹具 → Quarch 专用 USB-C 夹具线 → QTL2312 → USB 数据线 → Linux 主机`。
- 控制器供电：`墙上插座 → 匹配的 12V DC 适配器 → QTL2312`。

本方案不需要 PoE Injector 或 QTL2843。夹具 USB-C 接口只能连接匹配的 PAM 控制器。
详细接线图、身份核对和通道含义见 [M.2 SSD 接线与使用](docs/setup/qtl2312-m2.md)和
[显卡 PCIe / 8-pin 接线与使用](docs/setup/qtl2312-gpu.md)；
依赖安装见 [安装指南](docs/setup/installation.md)。

## SSD 快速开始

以下命令在仓库根目录执行；先完成安装和接线。若已有空闲服务，不必重复启动。

```bash
# 终端 1：常驻采集服务，默认关闭逐点 CSV
./bin/Quarch serve --device USB::QTL2312-01-578 --channel 3.3V --resample 10ms --csv off

# 终端 2：实时显示；Ctrl+C 结束本段采集，服务保留
./bin/Quarch watch --channel 3.3V --10ms

# 分段测量：将 csv 改为 off 可关闭逐点记录；两种模式均保存能耗 JSON
./bin/Quarch start --channel 3.3V --label ssd_task --csv on
# 执行被测任务并等待完成
./bin/Quarch stop

# 状态与 USB 重插后的主动恢复；下一次 start/watch 也会自动尝试恢复
./bin/Quarch status
./bin/Quarch recover
```

CSV 开关只控制逐点文件，输出频率默认仍为 100 Hz。更多操作见 [watch 指南](docs/usage/live-watch.md)
及 [开始／停止与 CSV 开关](docs/usage/command-controlled-capture.md)。

## 显卡测量入口

先按[显卡接线图](docs/setup/qtl2312-gpu.md#接线示意图)接入 PCIe 主夹具和 AUX 子板。
切换夹具后，结束原测量并重启空闲服务以重新发现通道。以下命令在两个终端分别运行：

```bash
./bin/Quarch serve --device USB::QTL2312-01-578 --channel Tot --resample 100ms --csv off
./bin/Quarch watch --channel Tot --100ms
```

`Tot` 查看所测供电轨合计；改为 `--channel +12Vaux` 可单独查看 8-pin。
当前矩阵负载测试中 Quarch 与 NVIDIA 自报功率存在差异，见[测量边界与待核查项](docs/setup/qtl2312-gpu.md#测量边界与待核查项)。

## DRAM 测量入口：DIMM1 / R1 + R5

按 [DRAM 接线图与电阻说明](docs/setup/qtl2312-dram.md)连接单块 riser；仅 CH1/R1、CH2/R5 有效，各为 5 mΩ。
在没有常驻采集服务占用仪器时，用下面命令采集两路压降、电流和功率，并汇总两路合计能耗：

```bash
.venv/bin/python -m quarch_capture.dram \
  --device USB::QTL2312-01-578 --channel Tot \
  --seconds 30 --resample 1ms --shunt-mohm 5 5
```

输出在 `results/dram/<UTC时间>/`，保留原始数据、R1/R5 独立 CSV、时间戳和所选通道能耗 JSON。
不写 `--resample` 默认 10 ms，`--channel` 必填；电源轨名称未确认，`Tot` 只表示已接两路的功率合计。
[完整操作与测试步骤](docs/usage/dram-shunts.md)包括 Ubuntu USB 权限、实时 watch、按任务 start/stop、离线复核和代码职责。
已有 SSD、显卡和 QTL2843 的 USB / Ethernet 方法继续保留，切换夹具前先结束当前测量并关闭服务。

## 测量 TPC-H 每条查询的 SSD 能耗

已有空闲采集服务时，在另一个终端运行：

```bash
./bin/tpch-ssd-energy

# 可选：先测单条、单次，保存逐点 CSV
./bin/tpch-ssd-energy --queries 6 --runs 1 --csv on
```

默认使用 `~/experiment_dbs/duckdb/tpch_sf30_uncompressed.duckdb` 和同目录下源码编译的
`duckdb-v1.5.5/build/release/duckdb`，可通过 `--database`、`--duckdb` 指定其他路径。
默认 Q1–Q22 每条 **3 次冷缓存**、8 线程、8GB 内存上限、8GB 临时文件上限、间隔 2 秒，测量 `3.3V`，逐点 CSV 关闭。
每次创建新的只读 DuckDB 进程，清理数据库文件页缓存并检查驻留比例，保留各次结果。
完整步骤、输出字段和测量边界见 [SSD TPC-H 测试指南](docs/usage/tpch-energy.md)。

## 已发布的结果

[2026-11-09 未压缩 SF30 实验](experiments/2026-11-09-tpch-sf30-ssd/README.md)包含：

- Q1–Q22 × 3，共 **66 次有效采集**，附每次能耗 JSON、SQL、汇总和运行配置。
- Q19 间隔 30 秒的 **3 次单独复测**，与整轮结果分别保存。
- 硬件身份、1 ms / 3,000 点 CSV，以及真实 USB 拔插后恢复的 308 点记录。
- 文件校验和与来源清单；可离线核对，无需仪器或数据库。

**Q19、Q20 在整轮中波动明显。** 原始值全部保留，Q19 复测不能替代原始值；温度可能影响结果，但未证明降速原因。
69 次 TPC-H 采集关闭了逐点 CSV，能耗 JSON 保存在线积分结果；短时硬件验证另有 CSV 可离线重新积分。

[2026-11-11 RTX 5050 实验](experiments/2026-11-11-rtx5050-gpu/README.md)保存 **6 段完整采集、1,820 点**：
60 秒静息测试平均 **12.755 W**；首次 30 秒矩阵负载平均 **105.639 W**；
另三次 30 秒、100 ms 间隔复测平均 **108.652 W、3259.55 J**，轮间变异系数 **0.0821%**。
原始 CSV、各分路能耗、温度/利用率遥测、矩阵源码及方法脚本均已归档。
首次负载与三次复测的起始温度不同，分别保存；Quarch 与 NVIDIA 负载功率差异仍未解释，重复性不代表绝对准确性。

```bash
# 离线核对发布文件、69 次采集及汇总、硬件 CSV 积分
python3 tools/analysis/verify_published_results.py
# 离线核对显卡 6 段采集、分路积分、遥测和三次复测统计
python3 tools/analysis/verify_gpu_results.py

# 自动测试不连接仪器；真实 DuckDB 测试需要 CLI 在 PATH 上
PATH="$HOME/experiment_dbs/duckdb/duckdb-v1.5.5/build/release:$PATH" \
  .venv/bin/python -m unittest discover -s tests -v
```

本机包含真实 DuckDB CLI 的 **119 项测试全部通过，无跳过**，包括 DRAM 外部分流转换和有效双路合计。

## 目录结构

```text
bin/                  Quarch 和 tpch-ssd-energy 命令入口
quarch_capture/       通道发现、QIS 通信、USB 恢复、采集及积分
config/udev/          按仪器序列号匹配的 USB 权限规则
tools/                安装、基准测试和分析工具
  setup/              安装与连接检查
  benchmark/          SSD 冷缓存、连续批次和仅计时的 TPC-H 脚本
  analysis/           CSV 分析和发布结果离线校验
tests/                自动测试与夹具 XML 样本
docs/setup/           安装、Ethernet / USB 接线、排障及型号规格
docs/troubleshooting/ 故障证据、恢复过程和待验证事项
docs/usage/           实时查看、分段采集、TPC-H 操作说明
docs/diagrams/        接线图 Mermaid 源文件
experiments/          有说明、来源和校验和的正式实验归档
results/              本机新测量输出（Git 忽略）
validation/           本机调试记录（Git 忽略）
```

`.venv/`、厂商源码 `QuarchPy/`、运行包 `downloads/`、数据库和运行日志不随仓库发布。
新实验先写入 `results/`，核对后再归档到 `experiments/`，保持代码与结果分开。

## QTL2843 整机交流测量：Ethernet 与 USB

QTL2843 的 `L1/L2/L3` 对应交流输出端，本机以 `L2` 测整机能耗。
**2026-11-25 起，本机使用 Ethernet，USB 已拔掉**，接线如下：

```text
被测电脑 eno1 ── 交换机 ── Injector DATA IN
Injector PoE & Data OUT ── QTL2843 LAN（数据＋仪器供电）
插座 ── QTL2843 INPUT ── OUTPUT L2 ── 被测电脑交流电源
插座 ── Injector 电源输入
```

电脑有线地址为 `192.168.1.10/24`，仪器为 `192.168.1.99/24`、DHCP 关闭，TCP 命令端口 `9760`。
两种通信方式都可继续使用，以下服务命令选择一条运行：

```bash
cd /home/ubuntu/Quarch
# Ethernet：当前使用，网络正常后不需要 USB
.venv/bin/python -m quarch_capture serve --device TCP::192.168.1.99 --channel L2

# USB：原方法保留，需要接回 USB 并具有设备访问权限
.venv/bin/python -m quarch_capture serve --device USB::QTL2843-03-002 --channel L2
```

看到 `"ready": true` 后，另一个终端执行 `Quarch start --channel L2`、`Quarch stop` 或
`Quarch watch --L2 --10ms`；`--channel` 不可省略，watch 可使用 `--L2` 简写。
切换通信方式时正常退出原服务，再用新目标启动。
本机已验证无 USB 的 1000 Hz、30 秒采集，获得 30,000 个连续采样点；
仪器底层采样率为 8 kHz，与 QTL2312 的直流采样规格不同。

- [Ethernet：静态地址、身份查询、1000 Hz 采集和重连](docs/setup/ethernet.md)
- [当前 Ethernet 与保留的 USB 接线图](docs/setup/current-wiring.md)
- [故障如何排查和恢复，哪些原因尚未确认](docs/troubleshooting/ethernet-recovery-20261125.md)
- [官方自检与状态查询](docs/setup/ethernet.md#自检和状态查询有什么用)、[如何执行一次软重启](docs/setup/ethernet.md#如何执行一次软重启)
- [USB 安装与权限配置（保留）](docs/setup/installation.md#qtl2843-原-ubuntu-usb-配置保留)、[USB 排障](docs/setup/usb-notes.md)

型号精度与旧环境基线见 [QTL2843 精度资料](docs/setup/qtl2843-accuracy.md)。
原 [连续批次能耗测量](docs/usage/tpch-energy.md#原有连续执行批次测量)和
[仅测 DuckDB 耗时](docs/usage/tpch-timing.md)入口继续保留。
