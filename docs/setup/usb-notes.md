# USB 使用注意事项与排查

适用接线见 [当前接线简图](current-wiring.md)，准备环境见 [安装指南](installation.md)。以下注意事项针对 `QTL2843-03-002`、Ubuntu 用户 `ubuntu`，核实日期为 2026-11-07（UTC）。

## 1. USB 通信、仪器电源与被测电源是三件事

本方案使用 USB 数据线连接电脑和仪器 USB-B 接口，仪器由 PoE Injector 供电，电脑的交流电经过 `INPUT → OUTPUT L2`。不能用“插上 USB”代替检查后两条供电路径。[厂商接线说明，第 13–15 页](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf)

首次排查时优先使用可传数据的线缆、直连主机 USB 端口，并确认插头牢固。采集中避免插拔 USB、断开仪器供电或让电脑休眠。若需要更改电源接线，先按厂商手册断电操作。

## 2. 识别正确的设备

```bash
lsusb -d 16d0:0449
lsusb -t
```

本机可能显示：

```text
ID 16d0:0449 MCS 3-Phase PAM
```

USB 描述符名不是最终的型号判据。应继续通过 `*IDN?` 确认 `Part#: QTL2843-03` 和 `Serial#: QTL2843-03-002`。该原生 USB 设备使用 libusb 用户态访问；本机 `lsusb -t` 的 `Driver=[none]` 与成功通信同时存在，不应仅据此判断“缺少内核驱动”。它也不是 USB 转串口，不需要等待出现 `/dev/ttyUSB0`。

已安装环境下可执行：

```bash
cd /home/ubuntu/Quarch
source .venv/bin/activate
python tools/setup/check_connection.py --backend usb
```

这个检查读取身份及录制状态，不开始采集、不切换交流输出。

## 3. 普通用户必须拥有 USB 节点的读写权限

`lsusb` 能列出设备，不代表应用能打开设备。之前遇到的实际问题是 `/dev/bus/usb/...` 由 root 拥有，普通用户没有写权限；以 sudo 运行成功，也不能证明普通用户权限已经修好。[Quarch Linux USB 权限说明](https://quarch.com/support/faqs/usb/)

当前使用的规则文件是 [config/udev/99-quarch-qtl2843.rules](../../config/udev/99-quarch-qtl2843.rules)：

```udev
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="16d0", ATTR{idProduct}=="0449", ATTR{serial}=="QTL2843-03-002", OWNER="ubuntu", MODE="0660"
```

它只匹配这台仪器，并把设备交给 `ubuntu` 用户。更换设备或运行账户时，需要相应修改序列号或用户名。安装命令见 [安装指南](installation.md#5-配置普通用户-usb-权限)。

重插后再以普通用户运行 USB 检查。本机已经在设备号从 `006` 变为 `007` 后通过检查，因此不需要把动态节点号写进程序。单独 `chmod /dev/bus/usb/001/006` 既可能找错节点，也不能替代重插后自动应用的 udev 规则。

## 4. 避免多个进程直接争用同一 USB 接口

QIS 可以向多个客户端提供服务，但同一台仪器的 USB 接口应由确定的进程管理。正在由 QIS 采集时，不要同时让另一个直接 USB 程序抢占该接口。设备忙、锁定或 `LIBUSB_ERROR_BUSY` 时，先结束采集并释放原连接，再进行直接 USB 排查。

```bash
ps -eo user,pid,args | rg 'qis[.]jar|qps[.]jar|quarch_capture'
ss -ltn '( sport = :9722 )'
```

默认 TCP 端口 `9722` 是 QIS 服务端口。仅检查进程和端口不等于设备状态正常，最终仍需身份查询或采集结果验证。

## 5. 被测电脑也运行采集程序，会影响功耗口径

当前电脑同时运行 Python、Java/QIS 和 CSV 写入。这些程序的 CPU、内存和磁盘活动属于电脑当时的运行负载，会计入 `L2` 所测整机交流输入功率。比较不同实验时，应保持后台任务、采集设置、预热时间和输出文件方式一致。

既往两次采集都在起始阶段出现功耗升高；一次在约 `0.4s` 还出现原始电流瞬态。使用者确认没有刻意操作，但目前没有同步的 CPU / 进程 / 电源事件日志，**尚未确定原因**。不能只凭这段波形把它归因于 USB、采集程序或硬件故障。

如要区分测量软件带来的负载，可在后续实验中记录 CPU 与 I/O 活动，或使用独立控制电脑做对照。独立控制电脑的接法与本图不同，需要另行记录测量边界。

## 6. USB 也可能带来供电或接地方面的影响

对于待机、关机或极低功耗实验，USB 连接也应纳入实验边界：如果外设从电脑 USB 端口取电，这部分电能会反映在电脑的输入端测量中；USB 线也可能形成额外接地通路。

**本机没有测量 USB VBUS 电流，也没有验证仪器 USB 的隔离特性；目前没有证据证明本次存在反灌或地环路。** 不应据此声称“USB 完全不影响结果”，也不应直接宣称“峰值由 USB 引起”。是否需要隔离或改变控制方式，要结合设备资料及对照测量确认，保持设备规定的保护接地。

既往未作为主要负载的 `L1/L3` 小负功率均值也不能单独作为 USB 干扰证据；确认空载后，应以空载基线和适用校准资料复核。

## 7. 10 ms 输出间隔与硬件采样间隔

本机默认设置为：

| 层次 | 设置 |
| --- | --- |
| 仪器内部采样 | 8000 次/秒，125 µs |
| QIS 输出间隔 | 10 ms，即每秒 100 条记录 |
| 默认 AC RMS / 有功功率窗口 | 100 ms |

降低输出速率主要减少导出的数据量；AC 指标仍基于高速采样计算。输出重采样之后，不要用“平均电压 × 平均电流”代替 AC 有功功率。能耗应使用有功功率的时间积分，并说明是单路还是总计、是否保留小负值及覆盖的实际时间。

## 8. 快速排障表

| 现象 | 优先检查 |
| --- | --- |
| `lsusb` 中没有 `16d0:0449` | 仪器供电、USB 数据线、主机端口；重新插牢后观察枚举 |
| `lsusb` 有设备，但普通用户打不开 | 当前节点读写权限、规则序列号和用户名、udev 是否重载并应用 |
| sudo 可用、普通用户不可用 | 使用同一 Python 环境复核，修正 udev；不要把 sudo 成功当成权限已修复 |
| USB 节点号变化 | 使用序列号连接，不固定 `/dev/bus/usb` 编号 |
| 提示设备 busy / locked | 排查直接占用接口的进程，先正常停止采集并关闭连接 |
| `ModuleNotFoundError` | 是否激活 `.venv`、是否使用安装依赖的同一个解释器 |
| 传输中断、设备反复消失 | 结合内核日志检查线缆、Hub、仪器电源与设备电源管理设置 |
| 功率峰值变化 | 先核对功率窗口、后台任务、完整原始数据及事件时间，不凭单点判断原因 |

需要查看系统日志时，可由有权限的用户执行：

```bash
sudo journalctl -k --since '10 minutes ago' --no-pager
```

## 参考资料

- [QTL2843 IEC Mains PAM 技术手册](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf)
- [Quarch USB / Linux 权限 FAQ](https://quarch.com/support/faqs/usb/)
- [QIS 官方说明](https://quarch.com/products/quarch-instrument-server/)
- [QuarchPy 原生 USB 实现](https://github.com/QuarchTechnologyLtd/QuarchPy/blob/e4aa8a5ed0ee15b223504f8e254db6bb5bc123e5/quarchpy/connection_specific/connection_USB.py)
