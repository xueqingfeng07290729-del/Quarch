# 通过网线控制 QTL2843

本机于 **2026-11-25（UTC）**验证：USB 拔掉后，QuarchPy / headless QIS 可以经交换机控制
QTL2843，并完成 L2、1000 Hz、30 秒的功耗采集。无需启动 QPS。

USB 方案继续保留，见[安装与 USB 验证](installation.md)、[USB 注意事项](usb-notes.md)和
[两种接线图](current-wiring.md)。本指南说明在同一套软件环境中改用 Ethernet。
本次故障的证据和恢复过程另见[排查记录](../troubleshooting/ethernet-recovery-20261125.md)。

## 1. 接线和地址

```text
被测电脑 eno1 ── 网线 ── 交换机 ── 网线 ── Injector DATA IN
Injector PoE & Data OUT ── 网线（数据＋仪器供电）── QTL2843 LAN

插座 ── QTL2843 INPUT ── QTL2843 OUTPUT L2 ── 被测电脑交流电源
插座 ── Injector 电源输入
```

Injector 是 **Tenda PoE30G-AT**，交换机是 **BUFFALO LSW6-GT-8NS**。
Injector 的两个网口都要接：`DATA IN` 接交换机，`PoE & Data OUT` 接仪器。
PoE 给仪器供电，电脑的交流电仍走 `INPUT → OUTPUT L2`。

| 项目 | 本机已验证配置 |
| --- | --- |
| 电脑有线接口 / IPv4 | `eno1` / `192.168.1.10/24` |
| QTL2843 IPv4 / 掩码 | `192.168.1.99` / `255.255.255.0` |
| 仪器 DHCP | `DISABLED` |
| 仪器 TCP 命令端口 | `9760`，`ENABLED` |
| 仪器 MAC | `9c:95:6e:59:ff:dd` |
| 仪器型号 / 序列号 | `QTL2843-03` / `QTL2843-03-002` |
| 仪器固件 / QuarchPy / QIS | `5.119` / `2.2.24` / `1.54` |

这台交换机没有连接路由器或 DHCP 服务器，本方案使用静态地址。
电脑有线接口不设置网关；上网仍走原来的 Wi-Fi 默认路由。
仪器曾保存的网关字段是 `192.168.1.1`，它不代表交换机上存在该路由器，同网段控制不依赖这个网关。

## 2. 给电脑有线口配置同网段地址

先确认接口名及已有地址：

```bash
ip -br address
ip -4 address show dev eno1
```

仅在 `eno1` 尚未有 `192.168.1.10/24`，且确认该地址未被其他设备占用时执行：

```bash
sudo ip link set dev eno1 up
sudo ip address add 192.168.1.10/24 dev eno1
```

本机已经有这个地址时跳过 `ip address add`。这些是临时配置，系统重启后可能丢失；
本次没有修改 Netplan，也没有改动默认网关。以后持久化时应在本机实际使用的网络管理配置中设置。

```bash
ip route get 192.168.1.99
```

本机应显示 `dev eno1 src 192.168.1.10`，确认访问仪器走有线网卡。

## 3. 首次设置或网络失联时，通过 USB 查询仪器

网络正常时无需保留 USB。首次设置或失联排查时，可以临时接回 USB，使用原来的
[普通用户 USB 权限配置](installation.md#5-配置普通用户-usb-权限)。直接 USB 查询前，
先正常停止采集并关闭占用同一仪器的 QIS 连接，避免争用接口。

厂商建议的终端入口是：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m quarchpy.run terminal
```

本机这个入口因导入 `tkinter` 失败；未安装桌面，也未修改厂商代码。
本次实际使用厂商自带的另一个入口成功完成查询：

```bash
.venv/bin/python -m quarchpy.debug.simple_terminal
```

选择 USB 设备 `QTL2843-03-002`，在仪器终端中输入以下命令。
它们是仪器命令，不是 Linux shell 命令：

```text
*IDN?
FIX:IDN?
*MAC?
CONF:ETH:IP?
CONF:ETH:MASK?
CONF:ETH:DHCP?
CONF:ETH:TCP?
CONF:ETH:TCP:PORT?
CONF:ETH:LOCATE?
REC?
```

本方案需要 IP 为 `192.168.1.99`、掩码为 `255.255.255.0`、DHCP 关闭且 TCP 开启。
确需恢复静态地址时，在仪器终端输入：

```text
CONFIG:ETHERNET:DHCP OFF
CONFIG:ETHERNET:IP 192.168.1.99
CONFIG:ETHERNET:MASK 255.255.255.0
```

若查询确认 TCP 被关闭，可以用 `CONFIG:ETHERNET:TCP ON` 开启。
当前设备已配置好，日常使用无需重复写这些参数。
静态地址设置方法见[官方 LAN 指南](https://quarch.com/news/installing-quarch-products-on-your-lan/)。

设备可能回复 `Reset Required` 后跟 `OK`。[技术手册第 27 页](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf#page=27)
说明部分网络设置需要复位或重新上电才能生效。应在允许中断的窗口安排复位，复位后重新查询配置并验证通信。
本次恢复静态地址后，未执行第二次复位就已连通，这是本次观察，不能推广到所有设备或固件。
`*RST` 会中断仪器 USB / LAN 连接；电脑由 L2 供电时，未经确认不要假定交流输出在复位期间必然连续。

退出终端时可输入 `$close connection`，再输入 `$shutdown` 结束终端进程并释放 USB。

## 4. 分层确认网络通信

```bash
sudo ethtool eno1
ping -I eno1 -c 5 -W 1 192.168.1.99
ip neigh show to 192.168.1.99 dev eno1
```

电脑与交换机的链路正常时应看到 `Link detected: yes`。
这只证明电脑到交换机这一段；仍需检查交换机到 Injector 一段及仪器是否响应。
邻居表应解析到仪器 MAC `9c:95:6e:59:ff:dd`。
如果缺少 `ethtool`，可先用 `sudo apt-get install ethtool` 安装。

用厂商原生 TCP 实现读取仪器身份，可进一步确认连接到了正确设备：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python - <<'PY'
import socket
from quarchpy.connection_specific.connection_TCP import TCPConn

socket.setdefaulttimeout(4)
device = TCPConn("192.168.1.99")  # 本版本默认端口 9760
try:
    for command in ("*IDN?", "*MAC?", "CONF:ETH:IP?", "REC?"):
        print(command)
        print(device.sendCommand(command).strip())
finally:
    device.close()
PY
```

预期身份含 `Part#: QTL2843-03`、`Serial#: QTL2843-03-002`；空闲时 `REC?` 返回 `OFF`。
这个命令只查询，不开始采集。正常通信后可以拔掉 USB，再运行一次确认。

**自动扫描没有结果，不等于 IP 直连失败。** 厂商说明 LAN 自动发现使用 UDP 广播。
本次对 UDP `30303` 的广播和单播测试仍未收到回复，但 TCP `9760` 直连及 QIS 指定 IP 采集均成功。
QIS 的本机服务端口 `9722` 与仪器端口 `9760` 不同。

## 5. 采集 30 秒、1000 Hz 的整机功耗

先按[安装指南](installation.md)准备 QuarchPy / QIS。若已有常驻服务，先停止当前测量、保存结果并
正常退出服务，再使用固定时长模式。同一台仪器不要同时运行两种采集模式。

```bash
cd /home/ubuntu/Quarch
capture_dir="results/network/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$capture_dir"
.venv/bin/python -m quarch_capture \
  --device TCP::192.168.1.99 --ip 192.168.1.99 \
  --channel L2 --seconds 30 --resample 1ms \
  --output "$capture_dir/L2_1000Hz_30s.csv"
```

`--channel` 不能省略。`--resample 1ms` 是 QIS 输出每秒 1000 条记录；仪器底层仍保留
8 kHz 数据用于计算瞬时功率。测量完成后保存所选一路 CSV、全部通道 CSV 和 `.energy.json`。
总能耗按区间平均瞬时功率积分为 J 和 Wh，不能用平均电压乘平均电流替代。

本机已验证这条命令获得 30,000 个连续时间戳，完整覆盖 30 秒。
固定时长命令会关闭由它自己启动的 QIS；本次结束后 `REC?` 为 `OFF`。

## 6. 常驻测量、SQL 和 watch

网线模式下，在一个终端启动服务：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m quarch_capture serve \
  --device TCP::192.168.1.99 --channel L2 --resample 10ms --csv off
```

看到 `"ready": true` 后，在另一个终端使用原来的命令：

```bash
Quarch start --channel L2 --label sql_001 --csv off
# 执行 SQL 并等待完成
Quarch stop
Quarch watch --L2 --10ms
# 按 Ctrl+C 停止 watch，然后可正常退出服务
Quarch shutdown
```

若未安装 `Quarch` 命令入口，可换成 `.venv/bin/python -m quarch_capture`。
开始／停止、CSV 开关、SQL 脚本与 watch 的接口不因换成 Ethernet 而改变。
USB 模式仍使用 `--device USB::QTL2843-03-002`，操作完整保留在
[命令控制采集指南](../usage/command-controlled-capture.md)。换通信方式时正常退出原服务，再用新目标启动。

## 7. 重连与仍需验证的事项

- **拔插网线已通过**：空闲时拔下交换机到 Injector `DATA IN` 的网线，约 5 秒后接回；无需复位或重设 IP，TCP 身份查询恢复正常。
- 重连测试保持 Injector 电源和 `PoE & Data OUT → QTL2843` 不动，仪器持续供电。
- 采集中断线会影响数据完整性；应停止本段并检查结果，再重新测量。
- **仪器再次断电／复位后静态地址能否保留尚未验证**；电脑的临时有线地址也需在系统重启后检查。
- 同机运行 QIS / Python 的开销会计入 L2 整机能耗；Injector 从测量回路外取电，其功耗不计入 L2。

## 官方参考

- [Quarch LAN 设置说明](https://quarch.com/news/installing-quarch-products-on-your-lan/)
- [QTL2843 技术手册](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf)
- [本机使用的官方 TCP 实现](https://github.com/QuarchTechnologyLtd/QuarchPy/blob/e4aa8a5ed0ee15b223504f8e254db6bb5bc123e5/quarchpy/connection_specific/connection_TCP.py)
