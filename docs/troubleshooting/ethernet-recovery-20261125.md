# QTL2843 Ethernet 排查与恢复记录

记录日期：**2026-11-25（UTC）**。设备为 `QTL2843-03-002`，固件 `5.119`。
操作步骤见[Ethernet 使用指南](../setup/ethernet.md)，当前及备用 USB 接线见[接线说明](../setup/current-wiring.md)。

## 问题与厂商建议

此前 USB 可以控制仪器，Ethernet 一直失败。使用者曾观察到交换机上连接 Injector 的端口灯不亮；
2026-11-09 绕过交换机、电脑直连 Injector `DATA IN` 时，Linux 也曾报告 `NO-CARRIER`。
这些是此前的观察，不能当作 11 月 25 日整条链路状态的实时读数。

厂商建议通过 QuarchPy / Torridon Terminal 连接仪器，查询：

```text
*IDN?
FIX:IDN?
*MAC?
CONF:ETH:IP?
CONF:ETH:DHCP?
CONF:ETH:TCP?
CONF:ETH:LOCATE?
```

厂商还说明 LAN 自动发现使用 UDP 广播；不同子网可能阻断发现，但可以尝试指定 IP 直连。
本次没有发送邮件、升级固件或恢复出厂设置，也没有修改厂商 QuarchPy / QIS 代码。

## 1. 先确认电脑到交换机和 IP 路由

电脑 `eno1` 配置为 `192.168.1.10/24`，未设置有线网关。
`ip route get 192.168.1.99` 确认流量经 `eno1` 发出，源地址为 `192.168.1.10`。
`ethtool eno1` 显示电脑到交换机为 1000 Mb/s、全双工、`Link detected: yes`。
这一步只确认电脑到交换机这一段，不能证明 Injector / QTL2843 那一段也已建立链路。

2026-11-09 曾关闭 EEE、仅通告 100 Mb/s 后测试，仍未建立直连链路，随后恢复原设置。
本次没有继续反复更改网速或强制双工模式。

## 2. 用 USB 读取仪器实际返回值

`python -m quarchpy.run terminal` 在本机因导入 `tkinter` 失败。
改用未修改的厂商入口 `.venv/bin/python -m quarchpy.debug.simple_terminal` 后，USB 查询成功。
入口错误与仪器 Ethernet 是否正常是两件事。

| 查询 | 重启前的结果 |
| --- | --- |
| `*IDN?` / `FIX:IDN?` | `QTL2843-03`，序列号 `QTL2843-03-002` |
| 处理器 / FPGA | `QTL2657-01,5.119` / `QTL2848,1.1` |
| `*MAC?` | `9c:95:6e:59:ff:dd` |
| `CONF:ETH:IP?` / `CONF:ETH:MASK?` | `192.168.1.99` / `255.255.255.0` |
| `CONF:ETH:DHCP?` | `DISABLED` |
| `CONF:ETH:TCP?` / `CONF:ETH:TCP:PORT?` | `ENABLED` / `9760` |
| `CONF:ETH:LOCATE?` | `ENABLED` |
| `REC?` / `CONF:FAULT?` | `OFF` / `OK` |

按查询返回的地址，电脑与仪器属于同一 IPv4 子网，但这还不能证明网络配置已经正常生效。

## 3. 确认故障不局限于自动发现

重启前的测试结果：

- ping 没有回复，邻居表无法解析 `192.168.1.99` 的 MAC。
- 在 `eno1` 上用 tcpdump 确认电脑确实发出了 3 个 ARP 请求，没有收到回复，抓包无内核丢包。
- UDP 自动发现没有响应；直接连接 `192.168.1.99:9760` 也超时。
- UFW 未启用；未发现足以解释无 ARP 回复的本机过滤证据，没有关闭现有远程访问服务。

因此，最初的故障不能只用“UDP 广播没跨过子网”解释。
当时怀疑 Injector 数据通路或仪器网络接口异常，但没有凭这些现象直接认定硬件损坏。

## 4. 一次软重启，再恢复静态 IP

先保存身份、MAC、网络参数和录制状态。在使用者确认电脑仍接 L2、允许可能的供电中断后，
通过 USB 发送 **一次** `*RST`，不等待已经断开的连接返回回复，也不自动重发。
USB 消失后重新枚举，约 0.5 秒时已观察到新节点；电脑保持在线，USB 查询随后恢复。
这不构成复位时交流输出必然连续的保证。

关键变化：重启后查询显示 DHCP 为 `ENABLED`，IP、掩码、网关均为 `0.0.0.0`。
当前交换机没有 DHCP 服务器，因此这一状态下未获得可用地址。

接着通过 USB 执行以下命令，恢复重启前记录的配置：

```text
CONFIG:ETHERNET:DHCP OFF
CONFIG:ETHERNET:IP 192.168.1.99
CONFIG:ETHERNET:MASK 255.255.255.0
CONFIG:ETHERNET:GATE 192.168.1.1
```

每条设置回复 `Reset Required` 后跟 `OK`，但**未执行第二次复位就已恢复 TCP 通信**。
恢复网关字段是还原原配置；同网段通信本身不需要网关，也没有给电脑新增默认路由。

[厂商 LAN 指南](https://quarch.com/news/installing-quarch-products-on-your-lan/)介绍了静态 IP 设置；
[技术手册第 27 页](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf#page=27)
说明部分设置需要复位／重新上电生效。这里记录的是本机本次实际行为，两者不应混为一项通用保证。

## 5. 拔掉 USB，验证通信和真实采集

| 验证阶段 | 结果 |
| --- | --- |
| 软重启、恢复静态地址后 | ARP 解析为 `9c:95:6e:59:ff:dd`；TCP 返回正确身份 |
| USB 拔掉后 | `lsusb` 无仪器；5 次 ping 全部成功，平均 0.171 ms；TCP 查询成功 |
| 无 USB 的 QIS 采集 | 指定 IP，经交换机测量 L2，输出 1000 Hz，采集 30 秒 |
| 数据完整性 | 30,000 点，间隔 1 ms、时间戳连续，覆盖 30 秒 |
| 该次整机能耗 | 2491.162202 J，约 0.691989501 Wh；平均功率 83.038740 W |
| 采集结束 | `REC?` 为 `OFF`，故障状态 `OK`，本次创建的 QIS 已退出 |
| 随后拔插 DATA IN 网线 | 保持仪器供电，拔线约 5 秒后接回；无需复位或改 IP，5 次 ping 全部成功，平均 0.196 ms，TCP 身份查询正常 |

能耗值来自当时负载下的一次通信与采集验证，不是精度校准或固定的电脑待机功耗。
测量软件运行在同一台被测电脑上，其开销包含在结果中。

本次 QIS 使用 `--ip 192.168.1.99` 及 `--device TCP::192.168.1.99` 成功。
QIS 返回的目标名为 `TCP::QTL2843-03-002`。
独立 UDP 测试向 `255.255.255.255`、`192.168.1.255` 和仪器 IP 的 `30303` 端口发送发现请求仍无回复；
指定 IP 可正常控制和采集，不能再把自动扫描无结果等同于 Ethernet 完全不可用。

## 能确定什么，仍不知道什么

已确定：**一次软重启加静态 IP 重配后，同一套交换机、Injector 和网线恢复了通信，并完成无 USB 采集与网线重连验证。**

更可能是仪器网络配置或初始化状态异常，但这只是根据恢复过程的推断。
软重启和配置重写连续进行，未做单变量对照；没有仪器 PHY 日志，因此不能确认究竟是哪一步消除了最初的故障，
也不能据此断言固件存在某个 bug，或确定 11 月 9 日的 `NO-CARRIER` 与本次故障完全同源。

尚未验证：仪器再次软重启或断电后，静态地址是否保留；长时间持续采集的稳定性；UDP 自动发现不响应的原因。
本次没有新增“保存配置”的私有命令，也没有为验证持久性再复位正常工作的仪器。

## 本机证据位置

原始日志与 CSV 保留在本机，按仓库规则由 Git 忽略；本页上传经过整理的命令、结果和结论。

```text
validation/network/
  20261125_quarch_vendor_terminal.txt
  20261125_vendor_usb_diagnostics.json
  20261125_arp_diagnostic.json
  20261125_soft_reset.json
  20261125_restore_static_ip.json
  20261125_lan_recovered.json
  20261125_lan_without_usb.json
  20261125_cable_replug.json
results/network/20261125T061439_652553Z/
  command.json
  L2_1000Hz_30s.csv
  L2_1000Hz_30s.all-channels.csv
  L2_1000Hz_30s.energy.json
  verification.json
```
