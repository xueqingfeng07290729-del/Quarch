# Quarch QTL2843：无桌面 Linux 测量环境

记录 QTL2843 在 Headless Ubuntu 上的安装准备、实际接线、USB 使用注意事项和测量方法。

当前方案为 **PoE 给仪器供电、USB 负责通信、OUTPUT L2 给被测电脑供电**。同一台 Linux 电脑既是被测设备，也是运行 QuarchPy / QIS 的控制端。接线由设备使用者确认，USB 通信于 **2026-11-07（UTC）**重新核实。

## 阅读顺序

1. [安装 QuarchPy、QIS 和配套 Java](docs/setup/installation.md)
2. [当前接线说明与接线简图](docs/setup/current-wiring.md)
3. [USB 权限、排障与测量注意事项](docs/setup/usb-notes.md)
4. [用开始／结束命令分别测量 SQL，可选保存 CSV](docs/usage/command-controlled-capture.md)
5. [DuckDB TPC-H 逐查询计时与 SF10／20／30 选择](docs/usage/tpch-timing.md)
6. [DuckDB TPC-H 逐查询耗时与能耗](docs/usage/tpch-energy.md)
7. [Quarch watch：终端逐点查看功率](docs/usage/live-watch.md)
8. [测量精度、误差限制与官方资料](#测量精度与误差)

## 目录结构

```text
Quarch/
├── README.md
├── .gitignore
├── bin/
│   └── Quarch                   # 命令入口，自动使用项目虚拟环境
├── docs/
│   ├── setup/
│   │   ├── installation.md       # 下载、虚拟环境、QIS 运行包及验证
│   │   ├── current-wiring.md     # 已确认的实物接线及测量边界
│   │   └── usb-notes.md          # USB 权限、连接问题与结果解释
│   ├── usage/
│   │   ├── command-controlled-capture.md # 常驻采集、逐条 SQL、CSV 开关
│   │   ├── tpch-timing.md        # DuckDB 耗时脚本、规模选择与实测
│   │   ├── tpch-energy.md        # 批量重复查询，测耗时和整机能耗
│   │   └── live-watch.md         # 终端实时功率、间隔参数与停止行为
│   └── diagrams/
│       └── current-wiring.mmd    # 接线简图 Mermaid 源文件
├── config/
│   └── udev/
│       └── 99-quarch-qtl2843.rules
├── quarch_capture/               # 测量程序与内部模块
│   ├── __init__.py               # Python 包标记
│   ├── __main__.py               # python -m quarch_capture 统一入口
│   ├── capture.py                # 命令分发、固定时长采集
│   ├── stream_data.py            # 数据解析、在线能耗积分、可选 CSV
│   ├── qis_backend.py            # QIS 通信、采集配置、缓冲读取
│   ├── measurement.py            # 测量会话、状态与结果管理
│   ├── commands.py               # 本地命令服务、客户端与命令行参数
│   ├── client.py                 # 自动测量窗口、会话归属与异常恢复
│   ├── watch.py                  # 逐点功率输出与断连自动停止
│   ├── common.py                 # 原子保存、设备锁、退出及收尾处理
│   ├── recorder.py               # 兼容旧导入与旧模块入口
│   └── energy.py                 # CSV 离线能耗积分
├── tools/
│   ├── analysis/
│   │   └── analyze_quarch_csv.py  # 完整通道 CSV 的统计与绘图
│   ├── benchmark/
│   │   ├── tpch_timing.py         # 复用现有 DuckDB CLI，逐条计时
│   │   └── tpch_energy.py         # 复用常驻采集服务，测查询耗时与能耗
│   └── setup/
│       ├── install_qis_runtime.py # 安装并校验 QIS 共享运行包
│       ├── check_connection.py    # 普通用户下检查 USB / headless QIS
│       └── qtl2843_usb_check.py   # 基于厂商源码的低层 USB 诊断
├── tests/
│   ├── test_command_recorder.py   # 命令控制与能耗统计测试
│   ├── test_measurement_failures.py # 回复丢失、会话竞争和写盘失败的回归测试
│   ├── test_benchmark_recovery.py # 基准测试收尾与完成计数
│   ├── test_capture_entry.py     # 固定时长入口的设备保护
│   ├── test_energy.py            # 流式积分、报告保存与内存验证
│   ├── test_watch.py             # 功率输出、退出停止与间隔恢复
│   ├── test_tpch_timing.py        # TPC-H 计时与 CLI 只读测试
│   └── test_tpch_energy.py        # 能耗边界、失败处理与批量统计测试
├── results/                      # 本机结果与日志，Git 忽略
└── validation/                   # 本机验证与性能记录，Git 忽略
```

虚拟环境、厂商 QuarchPy 源码、Java / QIS 二进制安装包和本地测量结果均由 Git 忽略。厂商依赖的获取方式见安装指南。

常驻采集代码按四类职责拆分：`commands.py` 接收命令，调用 `measurement.py` 管理每段测量；
`qis_backend.py` 通过 QIS 控制仪器并读取数据，交给 `stream_data.py` 解析、积分和按需保存 CSV。
`recorder.py` 保留旧导入路径兼容性；新增 Python 调用可从 `quarch_capture.commands` 导入
`DEFAULT_SOCKET`、`send_command`。命令行用法保持一致。

自动化脚本优先使用 `client.capture_window`：在上下文内运行被测任务，退出时停止本段测量并保存结果，
统一处理 SIGTERM、启动回复丢失和停止通信恢复。TPC-H 脚本也复用这个入口。
离线 CSV 分析复用 `stream_data.EnergySink` 流式积分，避免同时维护两套计算逻辑。

TPC-H 自动测量使用协议版本 2：启动请求支持超时取消，停止时校验会话编号，提前停止的测量会被拒绝。
更新代码后，已有的常驻服务需要在空闲时重启一次；`status` 中应显示 `protocol_version: 2`。
能耗 JSON 采用原子替换保存；若写盘失败，服务保留结果并阻止下一次采集，可修复磁盘问题后再次 `stop` 补写。
有未保存结果时 `shutdown` 默认拒绝退出；只有显式 `shutdown --force` 才允许放弃保存后退出。
文件名使用固定长度会话标识，完整标签保存在 JSON 中，支持中文标签。

## 日常使用入口

在项目根目录运行。原根目录的 `qtl2843_capture.py` 和 `quarch_energy.py` 已分别移入
`quarch_capture/capture.py` 与 `quarch_capture/energy.py`，统一通过模块命令调用：

```bash
cd /home/ubuntu/Quarch

# 启动一次常驻程序；默认输出间隔 10ms，默认不保存 CSV
.venv/bin/python -m quarch_capture serve --device USB::QTL2843-03-002 --channel L2

# 在另一个终端按段控制；每次 start 都必须指定通道
.venv/bin/python -m quarch_capture start --channel L2 --label sql_001 --csv off
# 执行 SQL 并等待完成
.venv/bin/python -m quarch_capture stop

# 查询状态／退出常驻程序
.venv/bin/python -m quarch_capture status
.venv/bin/python -m quarch_capture shutdown

# 可选：对保存的 CSV 离线复核能耗
.venv/bin/python -m quarch_capture.energy results/sessions/实际文件名.csv --channel L2
```

安装[命令入口](docs/setup/installation.md#8-安装-quarch-命令)后，可在任意目录运行：

```bash
Quarch watch --L2 --10ms
Quarch watch --L2          # 默认 10ms；按 Ctrl+C 停止采集并退出
```

watch 复用空闲的常驻服务，逐行输出设备时间戳和功率 W；通道必须指定。
详见 [watch 使用说明](docs/usage/live-watch.md)。

`tools/setup/` 用于安装和排障，`tools/analysis/` 用于已有完整通道 CSV 的统计与绘图；
日常开始／停止测量只使用 `python -m quarch_capture`。完整操作见[使用指南](docs/usage/command-controlled-capture.md)。

常驻采集默认关闭 CSV，仅计算所选一路的瞬时功率，并以批量读取减少 CPU 开销；
输出间隔仍为 10ms。开启 CSV 时保留所选一路原有的完整字段。

完整通道绘图工具另需 Matplotlib，依赖和输入要求见使用指南。无仪器自动测试可执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

TPC-H 只测耗时的独立脚本使用 Python 标准库，无需安装 Python 的 `duckdb` 包：

```bash
python3 tools/benchmark/tpch_timing.py --database ~/duckdb/tpch_sf30.duckdb
```

该命令默认每条查询预热 1 次、计时 3 次、执行之间等待 5 秒，不启动 Quarch 采集。
参数、结果文件和本机规模选择见 [TPC-H 指南](docs/usage/tpch-timing.md)。

已有空闲的采集服务时，可同时测每条查询的耗时与能耗，通道必须指定：

```bash
.venv/bin/python -m tools.benchmark.tpch_energy \
  --database ~/duckdb/tpch_sf30.duckdb --channel L2
```

该命令默认每条查询预热三次，然后测三组，每组连续重复约五秒；组间等待五秒且停止采集。
结果包含整组实测值和折算单次平均值，能耗口径为 L2 整机耗电，含基础功耗与测量开销。
细节见 [TPC-H 能耗指南](docs/usage/tpch-energy.md)。

## 测量精度与误差

以下是厂商针对 **QTL2843** 型号公布的指标，资料于 **2026-11-07** 核对。
它们是型号规格；本仓库此前的通信和采集验证，不代表已用标准仪器校验本机的实际误差。

[官方 AC Power Analysis Modules 数据表第 6 页](https://quarch.com/wp-content/uploads/2022/12/AC-Power-Analysis-Modules.pdf#page=6)
的 `Measurement Information` 表中，QTL2843 一列列出：

| 项目 | 官方数值 / 本次查阅结果 | 说明 |
| --- | --- | --- |
| 电压校准精度 | **±(1% + 250mV)** | 既有百分比项，也有固定电压项，不能只保留 1% |
| 电流校准精度 | 原表写作 **±(1% 20mA)** | 原样保留；两项之间的运算符不明确，完整公式、百分比基准及适用条件需向厂商确认 |
| 底层采样率 | **8kHz** | 相邻原始采样点间隔为 **125μs**；这是时间分辨能力 |
| 功率、能耗精度 | 查阅的该数据表没有单独给出统一的百分比误差规格 | 不能据电压、电流指标宣称功率或能耗误差为 ±1% |

当前默认的 **10ms** 是 QIS 软件重采样后的输出间隔，对应约 80 个底层采样周期。
官方技术手册说明，AC 设备保留全速数据，并使用 QIS / QPS 的 `stream mode resample`
进行软件平均，见[技术手册第 31 页](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf#page=31)。

脚本按 `能耗 J = Σ(区间平均瞬时功率 W × 区间时长 s)` 积分。
实际 SQL 测量还需考虑起止区间量化、命令通信延迟，以及同机控制程序计入整机功耗的影响。
输出的小数位数和代码测试通过，均不能作为硬件精度的校准证明。

无需打开 PDF 时，可直接查看[官方 QTL2843 产品网页](https://quarch.com/product/ac-power-analyser/)，
其中介绍了 8,000 次/秒采样、三个设备端口及 Python 自动化；该网页没有列出上述具体精度公式。
需要确认电流公式或取得本机校准说明时，可通过[官方支持页面](https://quarch.com/support/)联系厂商。

## 已验证基线

| 项目 | 已验证值 |
| --- | --- |
| 仪器 | QTL2843-03，IEC Mains PAM |
| 序列号 | `QTL2843-03-002` |
| 仪器固件 | `5.119` |
| Python | `3.12.3` |
| QuarchPy | `2.2.24` |
| QIS | `1.54`，使用 headless 模式 |
| 配套 Java | `25.0.1`，由 `quarchpy-binaries 0.0.4` 提供 |
| USB 权限 | `ubuntu` 用户，无需 sudo 即可进行设备查询 |

本方案没有启动 QPS。安装包中包含 QPS / QIS 的共享组件，但安装文件不等于启动 QPS；QPS 原生 headless 运行能力尚未在此环境验证。
