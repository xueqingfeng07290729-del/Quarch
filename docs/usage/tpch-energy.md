# DuckDB TPC-H 逐查询耗时与能耗

## 当前 QTL2312：未压缩 SF30 的 SSD 冷缓存测量

使用 [`bin/tpch-ssd-energy`](../../bin/tpch-ssd-energy)，实现位于
[`tools/benchmark/tpch_ssd_energy.py`](../../tools/benchmark/tpch_ssd_energy.py)。
先按[安装与接线说明](../setup/qtl2312-m2.md)准备环境。命令自动使用项目虚拟环境，复用已经启动的空闲 Quarch 服务：

```bash
cd /home/xueqing/Quarch/Quarch

# 仅在服务未运行时，在终端 1 启动一次
./bin/Quarch serve --device USB::QTL2312-01-578 --channel 3.3V --resample 10ms --csv off

# 在终端 2 检查 state=idle、device_record_state=OFF、protocol_version=2
./bin/Quarch status

# 默认 Q1–Q22，每条独立执行 3 次，逐条测量 3.3V 供电轨能耗
./bin/tpch-ssd-energy

# 可选：仅测部分查询、改为单轮，或开启仪器逐点 CSV
./bin/tpch-ssd-energy --queries 1,6,9 --runs 1 --csv on
```

默认数据库为 `/home/xueqing/experiment_dbs/duckdb/tpch_sf30_uncompressed.duckdb`，
CLI 为同目录 `duckdb-v1.5.5/build/release/duckdb`。可用 `--database`、`--duckdb` 覆盖。
启动时保存表行数、存储压缩方式、SQL、CLI 版本、块设备型号和序列号，数据库以只读方式打开。
本机源码编译的 CLI 不支持 `-no-agent` 或 `.timer on 9`；入口检测实际支持的参数并使用兼容计时方式。

默认参数为 `--cache cold --runs 3 --threads 8 --memory-limit 8GB --spill-limit 8GB --sleep 2 --csv off`。
采样输出间隔沿用常驻服务，当前为 **10 ms / 100 Hz**。
每次查询使用新的 DuckDB 进程，配置完成后只对数据库文件调用 `POSIX_FADV_DONTNEED`，
并用 Linux `mincore` 检查缓存驻留比例不超过 0.1%；超出时拒绝把结果记为冷缓存。
不执行预热查询，缓存清理、进程启动、结果写盘和两次测量之间的等待均在采集窗口外。
这里的“冷缓存”指操作系统文件页缓存和新的数据库执行进程；SSD 控制器自身缓存仍可能影响读取。
`--cache keep` 保留系统文件缓存，同时仍为每次执行创建新进程，不保证全部数据已经热缓存。

一次采集只执行一条查询一次。停止录制后，记录 DuckDB 的存储读取/写入字节数、
SQL 耗时、采集窗口长度、功率积分能耗和进程峰值内存；失败或不完整采集不会以零值进入汇总。
能耗包含该 SSD 供电轨在采集窗口内的基础功耗、其他活动及可能的 DuckDB 临时文件 I/O，未扣除空闲基线。
`3.3V` 对应主电源轨；使用 `--channel Tot` 可选择夹具定义的两路合计。
原始功率 CSV 默认关闭，以免在被测 SSD 上额外写盘；`--csv on` 可开启，但会增加测量窗口内的记录开销。
查询的临时文件受 `--spill-limit` 限制，开始每次查询前要求剩余空间足够容纳该上限并留出 2 GiB。

输出位于 `results/tpch_ssd/<UTC时间>/`：

- `summary.csv`：每条查询的能耗/耗时均值和中位数、平均功率、读取量、资源峰值。
- `runs.csv`：每一次独立执行的结果，以及实际缓存驻留量、采集窗口与 SQL 耗时差。
- `captures/qXX_rYY.json`：每次查询的 Quarch 能耗证据，包含完整性标志和逐点 CSV 路径（若开启）。
- `queries/qXX.sql`：本次执行的 22 条 DuckDB TPC-H 查询。
- `run.json`：参数、完成状态、数据库只读检查和测量范围说明。

默认三次结果用于观察重复性。报告 `energy_median_J` 时应同时保留 `runs.csv`，
并检查 `run.json` 为 `complete`、完成查询数为 22。

本机 2026-11-09 实测完成 22 条查询、每条 3 次，共 66 次有效采集；所有查询前文件缓存驻留为 0，
所有执行均有实际存储读取，未观察到临时文件溢写，数据库文件大小和修改时间保持不变。
已发布的完整结果见 [summary.csv](../../experiments/2026-11-09-tpch-sf30-ssd/full-run/summary.csv)，
每次结果在 [runs.csv](../../experiments/2026-11-09-tpch-sf30-ssd/full-run/runs.csv)。

Q19、Q20 在整轮测试中的能耗最大/最小值比超过 1.5，应结合各次结果查看。
Q19 原始三次能耗约为 21.40、91.95、106.98 J；其后观察到一个 SSD 温度传感器约 95.85°C。
单独将间隔设为 30 秒复测 Q19，三次能耗约为 21.12、20.82、20.82 J，SQL 均约 7.8 秒，
同一传感器的最高记录温度约 60.85°C。这支持温度可能影响结果的判断，但没有证明降速的唯一原因。
原始值保留不变；[review.csv](../../experiments/2026-11-09-tpch-sf30-ssd/full-run/review.csv)
并列展示原始分布和 Q19 复测值，[validation.json](../../experiments/2026-11-09-tpch-sf30-ssd/full-run/validation.json)
记录完整性核对和复测目录。`complete` 表示测量流程完整，不表示查询已达到温度稳定状态。

全部说明见[实验归档](../../experiments/2026-11-09-tpch-sf30-ssd/README.md)，其中单独保留了
[Q19 复测](../../experiments/2026-11-09-tpch-sf30-ssd/q19-cooldown/summary.csv)。
本次 69 次 TPC-H 采集的逐点 CSV 均关闭，JSON 是在线积分结果，无法仅靠这些摘要重新积分原始波形。
硬件短测另有 CSV，可用 `python3 tools/analysis/verify_published_results.py` 离线核对积分、文件哈希及各次汇总。

本机验证使用已有源码编译的 DuckDB 1.5.5；包含真实 CLI 的 109 项自动测试全部通过。

## 原有连续执行批次测量

`tools/benchmark/tpch_energy.py` 复用现有 DuckDB CLI 和常驻 Quarch 采集服务，逐条测量 Q1–Q22。
它不启动 QPS，也不自动启动或关闭 QIS；运行前需要已有空闲的 `quarch_capture serve`。
只测耗时仍使用原来的 [tpch_timing.py](tpch-timing.md)。

## 开始测量

在项目根目录执行。若常驻服务已经运行，不要再启动一个实例：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m quarch_capture status

# 仅在服务未运行时，在单独终端启动一次
.venv/bin/python -m quarch_capture serve \
  --device USB::QTL2843-03-002 --channel L2 --csv off
```

确认 `state` 为 `idle`、`device_record_state` 为 `OFF`、`protocol_version` 为 `2`，然后执行：

```bash
.venv/bin/python -m tools.benchmark.tpch_energy \
  --database ~/duckdb/tpch_sf30.duckdb \
  --channel L2 \
  --threads 12 --memory-limit 16GB \
  --warmup 3 --runs 3 --batch-seconds 5 --sleep 5 --csv off
```

`--channel` 必填，QTL2843 使用 L1、L2、L3；QTL2312 使用夹具提供的 `3.3V`、`VIO_1V8`、`Tot`，必须与实际被测负载的电源轨一致。
只测部分查询可以加 `--queries 1,6,9,18,21`；数据库路径、线程数、内存限制与只测耗时脚本含义相同。

升级前已经运行的服务仍使用旧代码。确认空闲后，先 `python -m quarch_capture shutdown`，
再用上面的 `serve` 命令重新启动。脚本会在启动采集之前拒绝旧协议服务。

## 测量流程

每条查询先预热／试跑三次，此时采集关闭。按试跑耗时中位数估算重复次数：
`ceil(batch_seconds / pilot_median_seconds)`，至少一次、最多 `--max-executions` 次（默认 10000）。
然后运行三组正式测量，每组流程如下：

1. 确认服务空闲，生成独立请求标识和 45 秒启动期限，向指定通道发送 `start`。
2. 在一个 DuckDB CLI 请求中连续执行同一查询 N 次，收集每次 SQL 的引擎计时。
3. SQL 全部执行完成后携带会话编号发送 `stop`。服务端先校验编号，再停止和排空缓冲，确认采集完整。
4. 保存整组耗时、能耗及 N，计算每次平均值。等待五秒，再测下一组或下一条查询。

等待、预热、结果文件写入均在采集窗口之外；最后一组结束后不等待。
每组目标约五秒，实际时长随预热估计与运行速度变化，以输出的 `recorded_duration_s` 为准。
`--runs` 是每条查询的**测量组数**，每组实际执行多少次由试跑决定。
这是连续执行条件下的平均结果，与每次只执行一条、间隔五秒的原耗时测试条件不同。

## 结果含义

- 单次平均耗时：组内 DuckDB SQL 计时之和 ÷ N；计入规划与执行，不含启动、Python IPC 与等待。
- 单次平均能耗：该组所选输出端实测总能耗 ÷ N。单位 J，除以 3600 得到 Wh。
- 汇总表的 `sql_median_s`／`energy_median_J`：三组单次平均值的中位数。
  `sql_mean_s`／`energy_mean_J` 则由全部正式执行的总量除以总执行次数得到。
- 能耗来自区间平均瞬时功率积分，不使用 CPU 时间估算，也不使用电压／电流均值相乘估算。
- 上述 QTL2843 示例的 L2 给整台电脑供电，电脑同时运行数据库与 QIS／Python。
  所以能耗包含整机基础功耗、测量程序及采集命令边界的开销，**没有扣除空闲基线**。
- 采集窗口比 SQL 总计时通常更长。`duration_minus_sql_s`／`duration_minus_sql_percent`
  保留这个差值，其中包括控制延迟、IPC、SQL 间处理及采样区间量化等因素。
  不应把这部分能耗直接当成 SQL 引擎的净能耗；目标窗口加长可降低边界占比。
- 同一查询重复执行、操作系统页缓存未清理，因此结果不代表冷启动查询。
  三组标准差／CV 用于观察重复性，不代表仪器测量精度。

## 输出文件

```text
results/tpch_energy/<UTC时间>/
├── run.json                  # 配置、状态、CLI／SQL 版本、数据库只读验证、资源峰值
├── summary.csv               # Q1–Q22 的单次耗时与能耗汇总
├── batches.csv               # 各组 N、总 SQL 耗时、总能耗、平均值、采集时长及边界差值
├── pilots.csv                # 未采集能耗的试跑耗时
├── captures/q01_r01.json ... # 每组完整 Quarch 能耗结果的副本
├── queries/q01.sql ...       # 本次实际使用的 SQL
└── spill/                    # 本次 DuckDB 临时目录
```

`--csv off` 关闭的是仪器的逐采样点 CSV，汇总表和每组能耗 JSON 始终保存。
改为 `--csv on` 时，原始采样 CSV 由常驻服务写入它自己的输出目录，具体位置见每组 JSON 的 `csv_path`。
默认输出间隔由常驻服务决定，当前为 10ms；本脚本不改变服务的采样配置。

可用 `--output-dir` 指定尚不存在的目录，`--socket` 指定另一个常驻服务。
超时由 `--timeout` 设置，单位秒，作用于**整组** SQL，默认 300 秒，覆盖向 CLI 写入 SQL 与等待结果的时间。
超时或中断后会终止本次 DuckDB 子进程，再清理本次采集。
SIGTERM 同样执行清理并以中断状态退出；SIGKILL 和断电无法执行这套清理流程。
查询失败或中断会请求停止本次采集，保留部分结果并以非零状态退出。
启动回复丢失或超时时，按请求标识发送取消；取消即使先于启动到达，也会阻止对应的迟到启动。
取消失败时保留原始异常并报告清理状态未能确认。服务端在启动期限后拒绝启动，已开始但逾期的请求会尝试停止。
其他客户端提前停止本组采集时，该组会被拒绝；其他客户端已开始的新会话不会被自动停止。
失败响应中的部分能耗仍写入 `captures/`；若响应没有能耗结果，则写入诊断 JSON。
SQL 原始异常保留，清理异常作为附加说明写入终端及 `run.json` 的 `error_notes`。
停止通信失败时按会话重试，必要时按请求标识取消；恢复得到的采集证据会保留，但这组测量不进入汇总。
无法确认设备停止时会明确报告状态不确定。
收尾步骤独立尝试，汇总写盘失败不会阻止保存失败元数据或覆盖原始 SQL 异常。
缺失、出错或不完整的采集不会以零能耗计入汇总；分析前应检查 `run.json` 的完成状态。

服务端结果的 `complete` 表示采样完整性，`summary_saved` 表示 JSON 已保存成功。
写盘失败时返回 `ok: false`、`summary_saved: false` 和 `summary_save_error`，结果暂存在服务内存中，
并阻止新测量覆盖它。修复磁盘空间／权限问题后，再次发送 `stop` 可补写文件；此时设备无需再次采集。
未保存结果时 `shutdown` 默认保留服务供重试；只有显式 `shutdown --force` 才放弃这一恢复机会。
服务端 JSON、离线报告和基准汇总均采用原子替换，写入失败保留原文件。

## 自动测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试不连接仪器，覆盖测量边界、SQL 失败后的停止、错误／空结果拒绝、执行次数核对和能耗汇总，
以及启动回复丢失、取消与启动顺序竞争、误停防护、提前停止拒绝、写盘失败重试和管道写入超时。
另使用 CLI 内存数据库验证重复 SQL 全部执行且每次都有独立计时。
