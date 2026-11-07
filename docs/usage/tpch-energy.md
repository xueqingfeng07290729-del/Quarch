# DuckDB TPC-H 逐查询耗时与能耗

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

`--channel` 必填，可选 L1、L2、L3，必须与实际被测负载的输出端一致。
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
- 当前 L2 给整台电脑供电，电脑同时运行数据库与 QIS／Python。
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
