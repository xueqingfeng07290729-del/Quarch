# Quarch QTL2843：无桌面 Linux 测量环境

记录 QTL2843 在 Headless Ubuntu 上的安装准备、实际接线与 USB 使用注意事项。

当前方案为 **PoE 给仪器供电、USB 负责通信、OUTPUT L2 给被测电脑供电**。同一台 Linux 电脑既是被测设备，也是运行 QuarchPy / QIS 的控制端。接线由设备使用者确认，USB 通信于 **2026-11-07（UTC）**重新核实。

## 阅读顺序

1. [安装 QuarchPy、QIS 和配套 Java](docs/setup/installation.md)
2. [当前接线说明与接线简图](docs/setup/current-wiring.md)
3. [USB 权限、排障与测量注意事项](docs/setup/usb-notes.md)

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
│   └── diagrams/
│       └── current-wiring.mmd    # 接线简图 Mermaid 源文件
├── config/
│   └── udev/
│       └── 99-quarch-qtl2843.rules
└── tools/
    └── setup/
        ├── install_qis_runtime.py # 安装并校验 QIS 共享运行包
        └── check_connection.py    # 普通用户下检查 USB / headless QIS
```

虚拟环境、厂商 QuarchPy 源码、Java / QIS 二进制安装包和本地原始 CSV 不纳入本次文档提交。厂商依赖的获取方式见安装指南。

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
