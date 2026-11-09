# 未压缩 SF30：逐查询 SSD 能耗实验

日期：**2026-11-09（UTC）**。Q1–Q22 各执行 3 次，共 **66 次完整采集**；另有 Q19 间隔 30 秒的 **3 次复测**。
测量的是 QTL2980 夹具的 **3.3V SSD 主供电轨**。能耗来自功率随时间的积分，包含采集窗口内的基础功耗、其他 SSD 活动及命令边界开销，未扣除空闲基线。

**Q19、Q20 在整轮中的波动明显，应结合各次数据解释。** 原始值保留不变；Q19 复测独立保存，不用于替换原始结果。
本实验是使用 DuckDB TPC-H 查询的能耗测试，不作为正式 TPC 认证成绩。

## 环境与方法

| 项目 | 本次设置 |
| --- | --- |
| 仪器与夹具 | QTL2312-01-578；QTL2980-01-225（Internal# QTL2621-04-006） |
| 被测 SSD | KINGSTON OM8TAP41024K1-A00，`/dev/nvme0n1`；数据库位于该盘的 ext4/LVM 文件系统 |
| 主机 | Intel Core i5-14400F，16 GiB 内存；本机同时运行数据库、QIS 和 Python |
| 主机 DMI | 归档时查询为 MouseComputer / IStDEi-M47M / B760M |
| 数据库 | `tpch_sf30_uncompressed.duckdb`，37,330,104,320 字节，只读打开 |
| 数据规模 | SF30，lineitem 179,998,372 行；全部表行数及存储检查见 run.json |
| 数据压缩 | 存储检查为 Uncompressed；空值段还包含 Constant |
| DuckDB | 源码编译 CLI v1.5.5，提交 d8cdaa3 |
| 执行资源 | 8 线程；内存上限 8GB；临时文件上限 8GB |
| 缓存 | 每次新进程；清理数据库文件页缓存；所有测量前 mincore 驻留量为 0 |
| 采集 | 3.3V，10 ms 输出（100 Hz）；逐点 CSV 关闭 |
| 重复与间隔 | 每条 3 次，每次执行查询一次；整轮间隔 2 秒；Q19 复测间隔 30 秒 |
| 整轮时间 | 10:45:46–10:55:33 UTC |

冷缓存指 Linux 文件页缓存和新的 DuckDB 进程，未清除 SSD 控制器内部缓存。
进程启动、配置、文件缓存清理、结果保存及间隔等待在采集窗口外；每条查询的 SQL 计时与采集时长分别保留。
所有查询都有实际存储读取；未观察到临时文件溢写；数据库大小、修改时间等只读检查通过。

## 结果摘要

下表为整轮三次执行各自的中位数；耗时中位数与能耗中位数不一定来自同一次执行。
完整字段见 [summary.csv](full-run/summary.csv)，各次实测值见 [runs.csv](full-run/runs.csv)，
分布和复测对照见 [review.csv](full-run/review.csv)。表格小数位不代表仪器校准精度。

| 查询 | SQL 中位耗时（s） | 能耗中位数（J） |
| --- | ---: | ---: |
| Q1 | 7.023 | 18.165471 |
| Q2 | 0.788 | 1.982808 |
| Q3 | 4.717 | 12.547490 |
| Q4 | 3.155 | 8.300604 |
| Q5 | 5.216 | 14.046874 |
| Q6 | 3.570 | 9.776857 |
| Q7 | 5.356 | 14.697620 |
| Q8 | 6.115 | 16.897084 |
| Q9 | 8.216 | 21.329603 |
| Q10 | 5.845 | 15.530731 |
| Q11 | 0.630 | 1.922102 |
| Q12 | 4.495 | 12.363198 |
| Q13 | 2.701 | 7.007910 |
| Q14 | 3.834 | 10.668500 |
| Q15 | 3.711 | 10.234915 |
| Q16 | 0.778 | 1.849134 |
| Q17 | 3.561 | 9.535568 |
| Q18 | 4.011 | 9.945301 |
| Q19 | 30.661 | 91.952883 |
| Q20 | 5.765 | 15.995617 |
| Q21 | 4.523 | 10.614557 |
| Q22 | 0.603 | 1.588690 |

Q19 原始三次能耗为约 **21.40、91.95、106.98 J**；SQL 分别约 **7.93、30.66、34.96 秒**。
Q20 原始三次能耗约 **45.57、16.00、12.08 J**，同样存在较大波动，未额外复测。
Q19 之后的一次温度读取中 Sensor 1 为 **95.85°C**。增大间隔到 30 秒后，
[Q19 复测](q19-cooldown/runs.csv)得到约 **21.12、20.82、20.82 J**，SQL 均约 7.8 秒，
能耗中位数 **20.823221 J**；复测每 2 秒记录一次温度，同一传感器最高为 **60.85°C**。

这支持温度可能影响结果的判断，但单次高温观察不足以证明热降速，也不能证明它是波动的唯一原因。
`complete` 只表示测量流程与数据完整，不表示已达到温度稳定状态。
温度说明和完整性核对见 [validation.json](full-run/validation.json)，复测温度序列见 [temperatures.json](q19-cooldown/temperatures.json)。

## 文件组织与证据范围

```text
full-run/
  run.json                  原始运行配置、设备/数据库信息和完成状态
  runs.csv                  66 次独立测量
  summary.csv               22 条查询的统计
  review.csv                原始分布及 Q19 复测对照
  validation.json           完整性、只读、温度和代码哈希核对
  captures/                 66 份 Quarch 在线积分结果 JSON
  queries/                  实际执行的 22 条 SQL
q19-cooldown/
  run.json, runs.csv, summary.csv
  captures/, queries/       Q19 三次复测与对应 SQL
  temperatures.json         复测时的温度序列
hardware-check/
  controller-identity.txt, fixture-identity.txt
  fixture-idle.xml, fixture-recording.xml
  total-1ms.csv, total-1ms.all-channels.csv, total-1ms.energy.json
  physical-usb-replug.csv, physical-usb-replug.json
  qis-exit-recovery.json
manifest.json               原始来源、原始/发布文件哈希及路径整理说明
SHA256SUMS                  本目录发布文件的 SHA256 清单
```

69 次 TPC-H 测量均关闭了逐点 CSV，因此不能从这些摘要重新积分查询的原始波形。
保存的 JSON 可核对样本数、采样间隔、平均功率、能耗、会话身份和完整性，并与汇总逐项比较。
`hardware-check/total-1ms.csv` 是另一次 3 秒短测，可独立重新积分：**3,000 点、0.372680775 J**，平均功率约 0.124227 W。
该短测数值不代表查询负载功耗。真实 USB 拔插后记录有 **308 点、10 ms 连续间隔**；
另一次 QIS 退出恢复保留了 303 点的验证摘要，其逐点 CSV 未单独归档。

归档仅将 `runs.csv` 的 `capture_json` 改为相对路径、将结果 JSON 的自身/CSV 路径及验证目录改为可移植引用，
并移除了 `run.json` 启动状态中与本次实验无关的上一段 `last_result`。测量数值、会话标识和时间戳保持不变。
[manifest.json](manifest.json)逐文件记录变换及变换前后的哈希；本机原始文件未改动。
数据库、CLI、socket、挂载点等环境路径作为来源信息保留，不代表这些文件已打包。
数据库、厂商运行包、虚拟环境、空临时目录和调试日志不在归档中。

## 复现实验与离线核对

先按[接线说明](../../docs/setup/qtl2312-m2.md)与[安装指南](../../docs/setup/installation.md)准备硬件和软件，
自行准备同规模未压缩 DuckDB 数据库及 CLI，再按[完整测试步骤](../../docs/usage/tpch-energy.md)执行。
在仓库根目录：

```bash
# 终端 1；已有空闲服务时跳过
./bin/Quarch serve --device USB::QTL2312-01-578 --channel 3.3V --resample 10ms --csv off

# 终端 2：整轮；路径可通过 --database 和 --duckdb 修改
./bin/tpch-ssd-energy --cache cold --runs 3 --threads 8 --memory-limit 8GB --spill-limit 8GB --sleep 2 --csv off

# Q19 独立复测；结果自动放在新的时间戳目录中
./bin/tpch-ssd-energy --queries 19 --cache cold --runs 3 --sleep 30 --csv off
```

上述复测命令复现查询和采集参数；本次额外的温度轮询记录不是基准脚本默认输出。
脚本每次创建新结果目录，不覆盖本归档。SSD 型号、散热、后台 I/O 和软件版本变化都可能改变结果。

无需仪器、QuarchPy 或 DuckDB 即可验证本次发布的数据：

```bash
python3 tools/analysis/verify_published_results.py
```

验证包含全部文件哈希、66 + 3 次测量与统计一致性、冷缓存/I/O 记录、硬件 CSV 积分和 USB 时间戳连续性。
校验和用于发现文件变化，不能证明硬件测量精度或排除环境影响。
本机另运行了包含真实 DuckDB CLI 的 109 项自动测试，全部通过，无跳过。
