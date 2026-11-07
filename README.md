# Quarch QTL2843：无桌面 Linux 测量环境

记录 QTL2843 在 Headless Ubuntu 上的安装准备、实际接线、USB 使用注意事项和测量方法。

当前方案为 **PoE 给仪器供电、USB 负责通信、OUTPUT L2 给被测电脑供电**。同一台 Linux 电脑既是被测设备，也是运行 QuarchPy / QIS 的控制端。接线由设备使用者确认，USB 通信于 **2026-11-07（UTC）**重新核实。

## 阅读顺序

1. [安装 QuarchPy、QIS 和配套 Java](docs/setup/installation.md)
2. [当前接线说明与接线简图](docs/setup/current-wiring.md)
3. [USB 权限、排障与测量注意事项](docs/setup/usb-notes.md)
4. [用开始／结束命令分别测量 SQL，可选保存 CSV](docs/usage/command-controlled-capture.md)

## 目录结构

```text
Quarch/
├── README.md
├── .gitignore
├── docs/
│   ├── setup/
│   │   ├── installation.md       # 下载、虚拟环境、QIS 运行包及验证
│   │   ├── current-wiring.md     # 已确认的实物接线及测量边界
│   │   └── usb-notes.md          # USB 权限、连接问题与结果解释
│   ├── usage/
│   │   └── command-controlled-capture.md # 常驻采集、逐条 SQL、CSV 开关
│   └── diagrams/
│       └── current-wiring.mmd    # 接线简图 Mermaid 源文件
├── config/
│   └── udev/
│       └── 99-quarch-qtl2843.rules
├── quarch_capture/               # 测量程序与内部模块
│   ├── __init__.py               # Python 包标记
│   ├── __main__.py               # python -m quarch_capture 统一入口
│   ├── capture.py                # 命令分发、固定时长采集
│   ├── recorder.py               # 常驻采集、内存积分与本地命令接口
│   └── energy.py                 # CSV 离线能耗积分
├── tools/
│   ├── analysis/
│   │   └── analyze_quarch_csv.py  # 完整通道 CSV 的统计与绘图
│   └── setup/
│       ├── install_qis_runtime.py # 安装并校验 QIS 共享运行包
│       ├── check_connection.py    # 普通用户下检查 USB / headless QIS
│       └── qtl2843_usb_check.py   # 基于厂商源码的低层 USB 诊断
├── tests/
│   └── test_command_recorder.py   # 命令控制与能耗统计测试
├── results/                      # 本机结果与日志，Git 忽略
└── validation/                   # 本机验证与性能记录，Git 忽略
```

虚拟环境、厂商 QuarchPy 源码、Java / QIS 二进制安装包和本地测量结果均由 Git 忽略。厂商依赖的获取方式见安装指南。

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

`tools/setup/` 用于安装和排障，`tools/analysis/` 用于已有完整通道 CSV 的统计与绘图；
日常开始／停止测量只使用 `python -m quarch_capture`。完整操作见[使用指南](docs/usage/command-controlled-capture.md)。

常驻采集默认关闭 CSV，仅计算所选一路的瞬时功率，并以批量读取减少 CPU 开销；
输出间隔仍为 10ms。开启 CSV 时保留所选一路原有的完整字段。

完整通道绘图工具另需 Matplotlib，依赖和输入要求见使用指南。无仪器自动测试可执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

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
