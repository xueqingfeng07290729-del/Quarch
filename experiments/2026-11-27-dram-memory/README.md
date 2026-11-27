# DIMM1 内存读取／写入能耗对比（2026-11-27 UTC）

本次负载下，连续读取和写入都使已接的 R1/R5 两路功率明显上升，主要变化来自 CH2/R5。
三种状态各测三次，每次 15 秒、10 ms 输出；表中为三次平均，能耗列为**每个 15 秒窗口的平均能耗**。

| 状态 | CH1/R1 平均 W | CH2/R5 平均 W | 合计平均 W | 合计轮间标准差 W | 15 秒能耗 J |
| --- | ---: | ---: | ---: | ---: | ---: |
| 空闲 | 0.069515 | 0.834240 | 0.903755 | 0.042248 | 13.556323 |
| 连续读取 | 0.102475 | 2.376079 | 2.478553 | 0.013502 | 37.178296 |
| 连续写入 | 0.166773 | 2.806027 | 2.972800 | 0.043789 | 44.592000 |

相对同轮空闲，读取平均增加 **1.574798 W（174.25%）**，每 15 秒增量能耗 **23.621973 J**；
写入增加 **2.069045 W（228.94%）**，每 15 秒增量能耗 **31.035677 J**。
增量能耗按各段实际设备采样时长扣除同轮空闲功率计算。详见 [summary.csv](summary.csv) 和 [27 行逐次结果](comparison.csv)。

写入在本次程序下的合计功率比读取约高 19.94%；这两种操作的吞吐、CPU 计算和缓存流量不同，
不能由此得出“每写一个字节一定比读一个字节多耗多少能量”的结论。

## 硬件和负载方法

- Dell Precision 5820，Xeon W-2135，6 个物理核心／12 个逻辑 CPU；Linux 可见约 16 GiB 内存，单 NUMA 节点。
- DIMM1 只有一块 DDR4 CSR riser：R1 → CH1/POWER_1，R5 → CH2/POWER_2，各配置并读回 5 mΩ。CH3/CH4 未连接且不合计。
- QTL2312-01-578，经 USB 控制；夹具 QTL2628-03-004。QuarchPy 2.2.24、QIS 1.54、Python 3.12.3、NumPy 2.5.4；不启动 QPS。
- 模拟基础采样 250 kHz，硬件平均保持 1，QIS 先计算功率，再按 10 ms 平均输出。原始压降、对地电压、电流和功率保存在 CSV。
- 总计 1 GiB 缓冲区，两个线程各处理 512 MiB，远大于本机约 8.3 MiB 的末级缓存。采集前已分配并写入全部页面，空闲阶段也不释放缓冲区。
- 读取：反复执行 NumPy `uint64` 求和，记录校验结果；写入：反复填充交替的 1／2。负载在设备录制前预热 2 秒，覆盖完整录制窗口，导出后停止。
- 负载线程固定在逻辑 CPU 1、2，分别对应不同物理核心；本次启动的 QIS 与主控使用其余物理核心上的 CPU `0,3,4,5,6,9,10,11`。
- 每段设备采集 15 秒，段后暂停 5 秒；轮次顺序为 `idle/read/write`、`read/write/idle`、`write/idle/read`。全部录制均写 CSV，QIS 进程在同一批次中复用。

程序运行于 10:16:23–10:20:39 UTC。设备有效录制合计 **135 秒**，不包含预热、初始化、导出和间隔；
每段时间轴从自己的录制起点开始，不把九段拼成一次连续录制。
完整参数、线程计数和换页计数见 [batch.json](batch.json)。
硬件接线、分流电阻原理、未知电源轨和注意事项见[DRAM 硬件指南](../../docs/setup/qtl2312-dram.md)。

## 有效性检查与失败尝试

九段均完成，每段 **1500 个连续采样点**，合计 13,500 点；两路配置均读回 0.005 Ω。
27 份通道能耗均从原始 CSV 离线重新积分，并与逐次汇总一致。
逐点 `Tot − (POWER_1 + POWER_2)` 的输出舍入差不超过 1 µW；未检测到主机 swap in/out。
六个有负载阶段的两个工作线程都完成了多次整缓冲区扫描。
采集后已恢复原来的双路常驻服务，确认 `idle`、录制 `OFF`；119 项仓库自动测试通过，无跳过。

首次使用 **4 个线程且未分配独立物理核心** 时，读取成功，但写入阶段触发
`Device buffer overrun - QIS buffer empty`。该批次标为失败，**不计入上表**，保留[失败批次记录](failed-four-workers.json)，
不完整 CSV 和日志保留在本机 `results/dram_memory/20261127_memory_rw_01/`。
随后同时减少线程数并分配独立核心，以上九段全部通过；这确认了当前配置可用，不能单独证明原先是 CPU 调度、内存带宽或其他因素触发溢出。

## 测量边界

两路电源轨名称仍未验证，R5 不标成 VPP，也不改写为 R6；`Tot` 只表示当前 CH1+CH2 的路径功率。
这不是全部内存、其他 DIMM 或整机能耗的证明，读数也未经参考仪表校准。
Linux 决定页面位置，不能把此缓冲区固定到某个物理 DIMM；供电覆盖仍需本板原理图核对。

求和包含 CPU 计算；普通写入还可能产生缓存行读取和回写。
同机 QIS、CSV 写入和后台活动也会使用内存，因此空闲基线包含采集软件自身的影响。
三次标准差表示这轮实验的轮间变化，不是仪器精度或误差界限；首轮空闲略高，原始值全部保留。
线程 `logical_bytes` 是完整负载生存期的逻辑扫描量，包含窗口外的预热和采集配置／导出，不能用来计算采样窗口内的每字节能耗。

## 复现与离线核对

先正常结束自己的测量并关闭空闲采集服务，再在仓库根目录执行：

```bash
.venv/bin/python tools/benchmark/dram_memory_energy.py \
  --device USB::QTL2312-01-578 --channel Tot \
  --seconds 15 --runs 3 --buffer-mib 1024 --threads 2 --resample 10ms
```

新结果保存在 `results/dram_memory/<UTC时间>/`，不会覆盖本次归档。
手动采集服务的恢复命令见[操作指南](../../docs/usage/dram-shunts.md#实时功率)。

离线验证归档文件校验和：

```bash
(cd experiments/2026-11-27-dram-memory && sha256sum -c SHA256SUMS)
```

在仓库根目录使用系统 Python 即可重新积分 27 项结果，无需仪器、QuarchPy、NumPy 或 QIS：

```bash
python3 - <<'PY'
import csv, math
from pathlib import Path
from quarch_capture.energy import energy_from_csv

base = Path('experiments/2026-11-27-dram-memory')
with (base / 'comparison.csv').open() as handle:
    rows = list(csv.DictReader(handle))
assert len(rows) == 27
for row in rows:
    channel = row['channel']
    result = energy_from_csv(base / row['raw_csv'], channel=channel, expected_duration_s=15)
    assert result['samples'] == 1500 and result['requested_duration_matches']
    assert math.isclose(result[channel]['energy_J'], float(row['energy_J']), abs_tol=1e-9)
print('27 项原始 CSV 能耗复核通过')
PY
```

## 归档内容与来源

每个 `rXX_idle/read/write/` 保存原始 `raw.csv`、采集元信息 `run.json` 和 `selected.energy.json`；
原始数值、文件内容和源文件一致。各段原始 CSV 使用相对时间，不含 CH3/CH4。
`batch.json`、两份汇总 CSV 和失败批次 JSON 也按原样复制。
元信息中的绝对路径是本机原始目录，归档访问以 `comparison.csv` 中的相对 `raw_csv` 为准。

完整诊断流、重复的所选／派生 CSV 和日志未重复上传，仍保留在本机。
[manifest.json](manifest.json)记录来源、逐文件 SHA256 和[负载脚本](../../tools/benchmark/dram_memory_energy.py)的 SHA256；
`SHA256SUMS` 覆盖整个归档。采集代码基于提交 `11e040c`，本次新增脚本复用已有 DRAM 采集和能耗积分代码。
