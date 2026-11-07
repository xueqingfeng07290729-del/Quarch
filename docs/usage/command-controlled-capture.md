# 用开始／结束命令分别测量多条 SQL

适用于当前 **QuarchPy + headless QIS + USB + QTL2843** 的接线。无需启动 QPS。
本机被测电脑接在 **OUTPUT L2**；测量时选择 `--channel L2`。

Python 代码统一放在 `quarch_capture/` 包中。日常命令使用 `python -m quarch_capture`，
替代原来的 `python qtl2843_capture.py`；离线积分使用 `python -m quarch_capture.energy`，
替代原来的 `python quarch_energy.py`。请先进入项目根目录，不需要额外安装本地包。

## 常驻程序与测量命令

进入项目目录，并启动常驻程序：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m quarch_capture serve \
  --device USB::QTL2843-03-002 \
  --channel L2 \
  --resample 10ms \
  --csv off \
  --output-dir results/sessions
```

看到 `"ready": true` 后即可在另一个终端发送命令。这个进程保持 QIS 和 USB 连接，
初始状态为 `idle`，仪器 `rec?` 为 `OFF`，没有数据读取线程。
只有 `start` 才会向仪器发送 `rec stream`；`stop` 发送 `rec stop`，读完缓冲数据后汇总能耗。
停止后的等待时间不会加入下一次测量。这里的“不采集”指停止记录和读取数据，
不代表关闭仪器电源或内部 ADC。程序不改变交流输出的供电状态。

如果需要关闭终端后继续运行，可用下面的方式替代前台启动；不要同时启动两份：

```bash
mkdir -p results
nohup .venv/bin/python -u -m quarch_capture serve \
  --device USB::QTL2843-03-002 --channel L2 --resample 10ms \
  --csv off --output-dir results/sessions \
  > results/recorder.log 2>&1 < /dev/null &
```

常驻模式和固定时长模式的默认输出间隔均为 `10ms`（100 条记录／秒），省略 `--resample` 时采用该值。
输出间隔由 `serve` 设置，修改后需重启常驻程序。
服务启动需要数秒。可查看 `results/recorder.log` 中的 `ready`，或运行 `status`。
这是当前登录用户的本地常驻程序，未配置开机自启。

```bash
# 查询状态，不会开始采集
.venv/bin/python -m quarch_capture status

# 开始一段测量；此命令成功返回后执行 SQL
.venv/bin/python -m quarch_capture start --channel L2 --label sql_001 --csv off

# 在此执行并等待第一条 SQL 完成

# 停止，并输出这一段的 J、Wh、平均功率及文件路径
.venv/bin/python -m quarch_capture stop

# 间隔期间不采集
sleep 5

# 第二条 SQL 可以单独开启 CSV
.venv/bin/python -m quarch_capture start --channel L2 --label sql_002 --csv on

# 在此执行并等待第二条 SQL 完成
.venv/bin/python -m quarch_capture stop

# 退出常驻程序；若仍在采集，先停止并保存这段汇总
.venv/bin/python -m quarch_capture shutdown
```

**每次 `start` 都必须显式传入 `--channel L1`、`--channel L2` 或 `--channel L3`，不可省略。**
即使 `serve` 已指定通道，开始每段测量时仍需再次指定；Python／socket 调用也必须传入 `channel`。
缺失或无效通道会报错，不会开始采集。通道选择决定统计和 CSV 的内容，不会切换实物电源线路。
每次只允许一段活动测量；重复 `start` 会报错，重复 `stop` 返回上一段结果而不重复计量。

## CSV 开关与输出

| 设置 | 采样 CSV | 能耗汇总 JSON |
| --- | --- | --- |
| `--csv off` | 不生成，程序在内存中累计能耗，不保留整段采样数组 | 停止后生成 |
| `--csv on` | 生成时间戳和所选一路的 CSV | 停止后生成 |

`serve --csv off/on` 决定默认值，`start --csv off/on` 可以逐段覆盖。
CSV 关闭时也必须在测量期间读取功率数据才能计算能耗，但不会把采样数据写到 CSV。
默认结果目录为 `results/sessions/`，文件名含 UTC 时间、`label`、通道和随机标识。
CSV 开启时只导出所选一路；常驻模式不生成全部通道的临时 CSV。

`stop` 输出 JSON，主要结果在 `result` 下。例如此前以 `50ms` 输出间隔采集的一段实机 L2 测量结果节选
（当前默认已改为 `10ms`）：

```json
{
  "ok": true,
  "state": "idle",
  "result": {
    "label": "validation_csv",
    "selected_channel": "L2",
    "csv_enabled": true,
    "device_record_state": "OFF",
    "samples": 64,
    "interval_s": 0.05,
    "duration_s": 3.2,
    "L2": {
      "mean_power_W": 71.8823125,
      "energy_J": 230.0234,
      "energy_Wh": 0.06389538888888889
    },
    "complete": true,
    "errors": []
  }
}
```

该例是功能验证时的电脑用电量，不是某条 SQL 的基准结果。
`energy_J` 是本段所选一路的净能耗，`energy_Wh = energy_J / 3600`。
计算方法为每个输出区间的平均瞬时功率乘以区间时长再求和；负功率保留。
记录 CSV 时，可以再用原来的离线工具核对：

```bash
.venv/bin/python -m quarch_capture.energy results/sessions/实际文件名.csv --channel L2
```

常驻模式已经在 `stop` 中计算能耗，无需再单独运行离线积分命令。

## 降低常驻采集的 CPU 开销

常驻模式按每段的通道和 CSV 开关配置 QIS 的合成通道：

| 设置 | QIS 合成计算 | CSV 内容 |
| --- | --- | --- |
| `--csv off` | 仅所选一路的 `PInst` 瞬时功率 | 不生成 |
| `--csv on` | 所选一路的瞬时功率、RMS、频率及其他默认电参数，共 8 个合成通道 | 保留该路原有的 11 列 |

不会为未选择的另外两路、三路总计和中性线 RMS 创建合成计算。连续测量使用相同通道和 CSV 设置时，
复用现有配置；换通道或切换 CSV 开关时，在开始记录之前重新配置。
结果 JSON 中的 `processing_profile` 和 `synthetic_channels` 记录实际使用的配置。

仪器原始采样仍为 8000 次／秒，QIS 在原始电压、电流上计算瞬时功率，再按默认 `10ms` 输出区间平均。
能耗仍使用该功率乘以区间时长累加，负功率保留；没有改用“平均电压 × 平均电流”。

Python 约每 `100ms` 批量读取一次缓冲数据，减少对 QIS 的轮询和线程唤醒次数。
每个 `10ms` 数据点都会保留，`status` 中的累计值可能延迟一个读取批次。
缓冲中有完整数据块时会连续读取，`stop` 会立即唤醒读取并取完尾部数据，避免批处理丢失最后一段。
CSV 关闭时仍只保留累计值，内存不会随测量时长累积。

2026-11-07 在本机以 L2、10ms、CSV 关闭进行两轮交替对照，使用同一个 QIS Java 进程，
每段先等待 5 秒，再观察 10 秒。下表取预热后的第二轮，`100%` 表示占满一个逻辑核：

| 组件 | 优化前平均 CPU | 优化后平均 CPU |
| --- | --- | --- |
| Python | 4.70% | 0.40% |
| QIS | 23.30% | 12.30% |
| 合计 | 28.00% | 12.70% |

这一轮合计下降约 55%，优化后约占本机 12 个逻辑核总能力的 1.06%。这些是短时对照均值，
启动预热和不同负载下会波动。本机完整记录在 `validation/performance/cpu_optimization_same_jvm.json`
（Git 忽略），包含两轮数据、通道切换和 125µs 数据完整性校验。

以上优化针对本文的 `serve/start/stop` 常驻模式；原固定时长模式仍保留全部通道 CSV 的采集方式。

## 在 Python 数据库测试中调用

数据库驱动负责执行 SQL 并等待完成。DuckDB 自动化测试可复用现有的 `DuckDBSession` 与
`measure_batch`，后者通过本地 Unix socket 调用服务，并处理启动回复丢失、会话校验和异常清理。
下面假定 `duckdb_session` 已初始化为 `DuckDBSession`，每个 `sql` 是单条 SELECT：

```python
import time
from pathlib import Path
import uuid
from quarch_capture.commands import DEFAULT_SOCKET
from tools.benchmark.tpch_energy import measure_batch


output = Path("results/sql_sessions")
output.mkdir(parents=True, exist_ok=True)
for index, sql in enumerate(sql_statements, 1):
    result = measure_batch(
        duckdb_session, sql, executions=1, socket_path=DEFAULT_SOCKET,
        channel="L2", label=f"sql_{index:03d}", save_csv=False,
        capture_path=output / f"sql_{index:03d}_{uuid.uuid4().hex}.json",
    )
    print(index, result["sql_mean_s"], "s", result["energy_mean_J"], "J")
    time.sleep(5)  # 仪器此时为 OFF，这 5 秒不计入任何一段
```

服务的 `--socket` 参数允许指定其他本地 socket 路径；客户端命令需要传相同参数。
默认 `~/.cache/quarch/recorder.sock` 的访问权限为当前用户独占，不提供网络监听。
不要让其他采集脚本或 QPS 同时操作同一台仪器。

完整 TPC-H 运行方式见 [耗时与能耗指南](tpch-energy.md)。自动测量要求 `status` 返回
`protocol_version: 2`；升级代码后，在空闲时重启原有服务一次。
自行实现客户端时，`start` 可附加 `request_id`（32 位十六进制标识）和 `start_deadline`
（Unix 秒时间戳，最多提前 120 秒）；不确定是否启动成功时用相同字段发送 `cancel_start`。
`stop` 可携带 `session_id`，服务端在同一把锁内先校验再停止；命令行也支持 `stop --session-id ...`。
自动化客户端必须拒绝 `already_stopped: true` 的能耗，避免把提前终止的采集计入完整 SQL。
写盘失败后结果的 `summary_saved` 为 `false`，服务保留结果，可在修复磁盘问题后重试 `stop`。

## 时间边界与测量含义

- 默认 `10ms` 是输出区间，即 100 个数据点／秒。`duration_s = samples × interval_s` 是这段数据实际覆盖的时长。
- `start`、`stop` 需要通过 USB／QIS 传递命令，启动读取和停止确认也需要时间。`command_window_s`、
  开始／停止时间戳与数据时长同时保存在 JSON 中；数据时长可能与 SQL 的墙钟执行时间不同。
  该方式测量的是命令括住的一段电脑总用电，包含边界处控制开销，不能把它当作无误差的 SQL 起止时间。
- 对远短于 10ms 的 SQL，单次测量的边界误差可能占比很高。可在一段内重复执行 SQL，统计整组，
  或在重新启动服务时设置更细输出区间；更细的区间不会消除命令传输延迟。
- 结果是整台电脑在所选交流输出上的能耗，未扣除待机基线，包含控制程序本身的用电。
- `complete: false`、`ok: false` 或非零命令退出码代表结果不完整或操作失败。没有样本时能耗为 `null`，
  不能把它当作零能耗。丢失样本、缓冲溢出、USB 中断会报错；不能确认停止时会禁止新测量。
- 正常停止后可用 `status` 确认 `state: idle`、`device_record_state: OFF`、`reader_active: false`。
  保留 USB 和 PoE 连接，使用 `shutdown` 或 Ctrl+C 正常退出；强制终止或断电无法保证最后一段完整保存。

原来的 `--seconds` 固定时长模式仍然保留；逐条 SQL 的测试应使用本文的 `serve/start/stop` 模式。

## 可选：固定时长采集与完整通道分析

固定时长模式会保存所选一路 CSV、全部通道 CSV 和所选一路的能耗 JSON。
先正常退出占用同一台仪器的常驻程序，再运行固定时长采集：

```bash
.venv/bin/python -m quarch_capture shutdown
mkdir -p results/timed
.venv/bin/python -m quarch_capture \
  --device USB::QTL2843-03-002 --channel L2 --seconds 10 \
  --output results/timed/example_L2.csv
```

`example_L2.csv` 是所选一路的数据，`example_L2.all-channels.csv` 是完整通道数据。
输出文件已存在时采集会报错，请换一个新文件名。

`tools/analysis/analyze_quarch_csv.py` 是可选的完整通道统计、绘图工具，需要 NumPy、pandas 和 Matplotlib。
前两者随 QuarchPy 安装，Matplotlib 可单独安装；这些分析依赖不用于常驻采集循环：

```bash
.venv/bin/python -m pip install matplotlib
.venv/bin/python tools/analysis/analyze_quarch_csv.py \
  results/timed/example_L2.all-channels.csv \
  --outdir results/analysis/example_L2
```

该工具要求默认 AC 合成通道齐全、RMS／有功功率窗口为 100ms；统计三路和总计，并以 L2 绘制电压、电流细节。
常驻模式保存的单路 CSV 应使用 `python -m quarch_capture.energy 文件.csv --channel L2` 复核能耗，
不适用于此完整通道绘图工具。

## 无仪器自动测试

以下测试使用模拟设备和本地临时 socket，不连接 USB，也不会启动 QIS 或采集：

```bash
.venv/bin/python -m unittest discover -s tests -v
```
