# DuckDB TPC-H 逐查询耗时测试

脚本位于 `tools/benchmark/tpch_timing.py`，直接复用本机现有 DuckDB CLI，不需要安装 Python 的 `duckdb` 包。
当前已验证 CLI 为 `/home/ubuntu/.local/bin/duckdb`，版本 `v2.0.0 (Cyanoptera) d54cbbe3df`。
数据库使用同一版本的 CLI 创建，测试也使用这个程序；脚本使用 `-no-agent`、`-no-init`、`-readonly`。

这份脚本只测试 SQL 耗时，不调用 Quarch 的 `start/stop`，不启动 QPS 或 QIS。

## 推荐规模与本机实测

本机有约 31GiB 内存、Xeon W-2135 的 6 个物理核／12 个逻辑核。当前建议以 **SF30** 做正式实验，
SF10 用于快速调试，SF20 用于需要缩短整套测试时间的情况。

2026-11-07，三库各执行全部 22 条查询，每条预热 1 次、正式计时 3 次，12 线程、内存上限 16GB，
查询之间不等待（`--sleep 0`）。结果如下：

| 数据库 | 实际文件大小 | 22 条查询中位耗时之和 | DuckDB 进程峰值 RSS |
| --- | --- | --- | --- |
| SF10 | 2.63GiB | 3.42s | 3.06GiB |
| SF20 | 5.35GiB | 7.11s | 5.54GiB |
| SF30 | 8.11GiB | 10.72s | 7.73GiB |

三库均完成，数据库文件大小和修改时间未变化。以 200ms 周期观察临时目录，均未观察到磁盘溢出。
峰值 RSS 是每个数据库对应进程在整个测试期间的峰值，不是每条查询单独的峰值。
SF30 的实测内存占用有余量，查询时间也比 SF10／SF20 更长，因此更适合当前机器后续的 SQL 能耗实验。
这些数值适用于本机与上述配置，不能单凭数据库文件大小推断其他查询或机器的内存要求。

SF30 仍有很短的查询，例如预热后 Q11 中位约 0.055s、Q6 约 0.133s；Q9 约 1.582s、Q18 约 1.444s。
后续结合 Quarch 测能耗时，短查询宜在一个采集窗口内重复执行多次，再报告整段能耗与每次平均值，
以减少控制命令边界开销的占比。独立的 [tpch_energy.py](tpch-energy.md) 已支持这种测量；
当前耗时脚本仍只计时。

## 运行 SF30

```bash
cd /home/ubuntu/Quarch
python3 tools/benchmark/tpch_timing.py \
  --database ~/duckdb/tpch_sf30.duckdb \
  --threads 12 --memory-limit 16GB \
  --warmup 1 --runs 3 --sleep 5
```

默认执行 Q1–Q22。每条先预热一次，再计时三次；每次执行之间等待 5 秒，最后一次执行后不等待。
等待时间不计入 SQL 耗时。预热也记录在明细中，但不计入平均值、中位数等正式统计。
默认每条查询超时为 300 秒，可用 `--timeout` 调整。

只测试指定查询，例如：

```bash
python3 tools/benchmark/tpch_timing.py \
  --database ~/duckdb/tpch_sf30.duckdb \
  --queries 1,6,9,18,21 --warmup 1 --runs 3 --sleep 5
```

`--queries` 支持 `1-22`、`1,6,9` 或 `1-5,9,18`，保留指定顺序，重复编号会去重。

## 一次比较三个规模

```bash
python3 tools/benchmark/tpch_timing.py \
  --database ~/duckdb/tpch_sf10.duckdb \
  --database ~/duckdb/tpch_sf20.duckdb \
  --database ~/duckdb/tpch_sf30.duckdb \
  --threads 12 --memory-limit 16GB \
  --warmup 1 --runs 3 --sleep 0
```

可以用 `--duckdb /实际路径/duckdb` 指定可执行文件。测试前须已有匹配该 CLI 版本的 `tpch` 扩展；
本机已经安装。脚本不会自动安装扩展、生成数据库或更改现有表。

## 输出与计时口径

默认输出到新的 `results/tpch/<UTC时间>/` 目录，也可通过 `--output-dir` 指定一个尚不存在的目录。

```text
results/tpch/<UTC时间>/
├── run.json                       # CLI 版本、参数、查询集合哈希
├── queries/q01.sql ... q22.sql     # 从本机 tpch 扩展导出的查询
├── 01_tpch_sf30/
│   ├── timings.csv                # 每次执行的详细计时，含预热标记
│   ├── summary.csv                # 每条查询的均值／中位数／最小／最大／标准差
│   ├── metadata.json              # 只读状态、内存峰值、临时文件观察等
│   └── spill/                     # DuckDB 的本次临时文件目录
└── comparison.csv                 # 按查询编号横向比较各库的中位耗时
```

- `elapsed_s` 来自 CLI 的 `.timer on 9` 的 `real` 值，单位为秒，保留九位小数。
  计入 SQL 规划和执行；结果使用 `.mode trash` 消费，不做终端表格打印。
- `driver_wall_s` 是 Python 从发送查询到收到完成标记的墙钟耗时，包含 IPC 和命令处理开销，供核对。
- `user_cpu_s`／`system_cpu_s` 是 DuckDB 进程 CPU 时间，多线程时可以大于墙钟耗时。
- 每个数据库复用一个 CLI 进程／连接，启动、配置、查询文本加载、输出文件写入和等待时间不计入 SQL 耗时。
- 不清理操作系统页缓存；`--warmup 0` 也不能视为真正冷缓存测试。
- 查询失败或超时会停止，退出码非零，已经完成的明细和部分汇总会保留；不要把未完成套件当作完整结果。

所有测试结果和数据库临时文件均放在 Git 忽略的 `results/` 下。

## 自动测试

```bash
python3 -m unittest discover -s tests -p 'test_tpch_timing.py' -v
```

测试覆盖查询编号解析、多个 SQL 语句的计时合并、排除预热、持久 CLI 会话和只读数据库行为。
CLI 集成测试使用临时库，不执行现有 SF10／20／30 的负载；没有 DuckDB 可执行文件时跳过集成测试。

## 参考资料

- [DuckDB TPC-H 扩展与查询定义](https://duckdb.org/docs/current/core_extensions/tpch)
- [DuckDB CLI 输出模式](https://www.duckdb.org/docs/current/clients/cli/output_formats)
- [DuckDB 连接复用与性能](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads)
- [DuckDB 工作负载与内存要求](https://www.duckdb.org/docs/current/guides/performance/environment)
