# DIMM1 单块 DDR4 riser：QTL2312 + QTL2628 双路采集

本页采用用户于 **2026-11-27 修正的实际接线**：Dell Precision 5820 的 DIMM1 插槽中只有一块 DDR4 CSR riser。
控制器为 `USB::QTL2312-01-578`，夹具为 `QTL2628-03-004`。
使用 QuarchPy / headless QIS，不启动 QPS。

## 阅读入口与代码结构

先看[硬件接线与电路原理](../setup/qtl2312-dram.md)：包含整套连接图、差分取样图、R1/R5 各 5 mΩ 的作用和接线注意事项。
本页负责软件安装、命令和测试。实际映射为 **CH1/POWER_1 → R1，CH2/POWER_2 → R5**，CH3/CH4 未接。
不将 R5 改写为 R6，不自动标为 VPP；两路电源轨名称均待本板原理图核对。

| 文件 | 职责 |
| --- | --- |
| [quarch_capture/dram.py](../../quarch_capture/dram.py) | DIMM1 双路入口、身份检查、R1/R5 映射、原始 CSV 保留、独立电流换算 |
| [quarch_capture/shunts.py](../../quarch_capture/shunts.py) | 阻值单位解析、设置与读回、外部分流电流通道、只合计有效输入 |
| [quarch_capture/capture.py](../../quarch_capture/capture.py) | 复用通用固定时长采集、QIS 配置、导出和清理 |
| [quarch_capture/channels.py](../../quarch_capture/channels.py) | 通道依赖和单位；兼容固件的 `A` / `current` 命名 |
| [quarch_capture/qis_backend.py](../../quarch_capture/qis_backend.py) | 常驻服务的 QIS 通信与采集 |
| [quarch_capture/measurement.py](../../quarch_capture/measurement.py)、[commands.py](../../quarch_capture/commands.py) | 分段测量状态与命令接口 |
| [quarch_capture/energy.py](../../quarch_capture/energy.py) | 按各时间窗平均功率积分，输出 J 和 Wh |
| [tests/test_external_shunts.py](../../tests/test_external_shunts.py) | 单位、依赖、双路合计、原始数据和重算回归测试 |
| [tools/benchmark/dram_memory_energy.py](../../tools/benchmark/dram_memory_energy.py) | 相同常驻缓冲区上的空闲／读取／写入对比，逐路统计功率和相对空闲增量 |

这些是本仓库的测量代码；没有修改官方 QuarchPy / QIS。厂商依赖按安装指南另行下载，测量输出保存在 Git 忽略的 `results/dram/`。

## 安装和 USB 权限

本机目录 `/home/ubuntu/Quarch`，运行用户 `ubuntu`。已有环境不用重装；新环境按[安装指南第 1–4 步](../setup/installation.md#1-取得本仓库并安装基础工具)
取得仓库、创建 `.venv`、安装固定版本 QuarchPy / Java，并下载包含 QIS 的共享运行包。
其中示例的 `/home/xueqing/Quarch/Quarch` 要换成本机路径。使用 Python 3.12、QuarchPy 2.2.24、QIS 1.54，只启动 headless QIS。

USB 权限让普通用户能读写仪器；`lsusb` 能看到设备不代表已经有写权限。
DRAM 本机使用下面的 `ubuntu` 规则。仓库另有 `xueqing` 的 SSD 主机规则，不要把它直接装到这台机器；
其他用户名／序列号应先修改相应字段。同一设备只使用一份符合实际用户的规则。

```bash
cd /home/ubuntu/Quarch
sudo install -m 0644 config/udev/99-quarch-qtl2312-ubuntu.rules \
  /etc/udev/rules.d/99-quarch-qtl2312.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=usb \
  --attr-match=idVendor=16d0 --attr-match=idProduct=0449 \
  --attr-match=serial=QTL2312-01-578
sudo udevadm settle --timeout=10
```

后续 Python 和采集命令使用普通用户。新装环境可依次运行下面两条只读检查；已有服务占用仪器时先正常结束测量并关闭服务：

```bash
lsusb -d 16d0:0449
.venv/bin/python tools/setup/check_connection.py --backend usb --serial QTL2312-01-578
.venv/bin/python tools/setup/check_connection.py --backend qis --serial QTL2312-01-578
```

Quarch 命令直接使用仓库内的 `./bin/Quarch`，无需另装本项目 Python 包；可选安装到 PATH 的方法见[安装指南第 8 步](../setup/installation.md#8-安装-quarch-命令)。

## 保存原始压降和独立电流

先用 `./bin/Quarch status` 检查是否有常驻服务。若有自己的活动测量，先 `./bin/Quarch stop` 保存结果，
确认 `idle` 后再 `./bin/Quarch shutdown`；没有服务时不必执行 stop/shutdown。然后运行独立脚本。
下面采集 30 秒、每 1 ms 输出一组数据（1000 Hz）；两个阻值按 CH1/R1、CH2/R5 顺序填写，单位 mΩ：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m quarch_capture.dram \
  --device USB::QTL2312-01-578 \
  --channel Tot --seconds 30 --resample 1ms \
  --shunt-mohm 5 5
```

脚本固定使用已接线的 CH1/CH2，`Tot` 在这个配置中仅合计 `POWER_1 + POWER_2`。
`--channel` 必填，可选 `Tot`、`POWER_1`、`POWER_2`；该参数选择能耗汇总对象，
两路原始数据和独立电流文件始终保存。CH3/CH4 不可在本脚本中选为测量对象。
默认时间 30 秒、输出间隔 10 ms、两个阻值均为 5 mΩ；阻值是用户提供的 nominal 值，不是程序测得的校准值。
更换电阻时应显式填写正确值。

程序检查 QTL2312 / QTL2628 身份、确认录制 OFF，只设置并读回 CH1/CH2 阻值；相同值不重复写入。
CH3/CH4 的历史阻值参数可能仍为 5 mΩ，当前配置不读取或使用它们，也不由此推断有电阻接入。
采集结束后停止录制，关闭本次创建的 QIS。Ctrl+C / SIGTERM 会尝试停止并保留已写入的原始诊断数据，运行标为未完成。

## 实时功率

在终端一启动服务时显式指定两个有效通道；不要同时运行独立脚本与常驻服务：

```bash
cd /home/ubuntu/Quarch
./bin/Quarch serve --device USB::QTL2312-01-578 \
  --channel Tot --active-channels POWER_1 POWER_2 --resample 10ms --csv off
```

终端二查看 CH1+CH2 合计功率，单位 W：

```bash
./bin/Quarch watch --channel Tot --10ms
# 或单看 R1 / R5，各命令单独运行
./bin/Quarch watch --channel POWER_1 --10ms
./bin/Quarch watch --channel POWER_2 --10ms
```

Ctrl+C 停止 watch，本地服务回到空闲。`./bin/Quarch status` 中的 `active_shunt_channels` 应只有
`POWER_1`、`POWER_2`，可用功率通道也仅为这两路及 `Tot`。
`--active-channels` 只在启动服务时传入；更改后需退出旧服务并重新启动。
没有显式限制通道的通用服务仍使用夹具原有四路 `Tot`，不适用于本次单 riser 合计。
常驻服务读取当前阻值配置；首次使用或换阻值时先运行前面的独立脚本，设置并验证 CH1/CH2 后再启动服务。

逐段测量命令如下；先结束 watch，服务保持运行：

```bash
./bin/Quarch start --channel Tot --label dimm1_sql_01 --csv on
# 在另一个终端执行并等待被测 SQL / 内存访问任务完成
./bin/Quarch stop
./bin/Quarch status
```

`stop` 输出该段能耗；`--csv off` 可关闭逐点文件，仍保留能耗 JSON。分段结果默认在 `results/sessions/`。
需要同时保存两路原始压降及独立电流时使用 `quarch_capture.dram`；`watch` 和通用 start 的所选功率记录不代替双路原始 CSV。

## 输出文件

默认目录为 `results/dram/<UTC时间>/`；`--output-dir` 可指定一个尚不存在的目录。

| 文件 | 内容 |
| --- | --- |
| `run.json` | 身份、DIMM1 的 R1/R5 映射、两个阻值、CH3/CH4 未使用标记、采样与完成状态 |
| `raw.csv` | CH1/CH2 的原始 QIS 输出数值与时间戳，不含 CH3/CH4 或数字通道 |
| `dram.csv` | 两路合并的 SI 单位数据 |
| `CH1_riser1_R1.csv` | R1 独立数据 |
| `CH2_riser1_R5.csv` | R5 独立数据，电源轨未知 |
| `selected.csv` / `selected.energy.json` | 选定一路或两路合计的功率及 nominal 阻值下的能耗 |
| `selected.all-channels.csv` | 仪器完整诊断输出备份，可能有未连接输入的原始值；不把它们映射成 riser 数据或计入合计 |

独立文件的列为：

```text
time_s,measured_voltage_V,shunt_voltage_V,shunt_resistance_ohm,current_A,qis_current_A,qis_power_W
```

`time_s` 是仪器本次录制起点后的相对秒数，单路文件和合并文件使用相同时间轴；
`run.json` 的 UTC `started_at` / `finished_at` 是脚本开始／结束时间，包含初始化与导出开销，不能当作每个 ADC 点的绝对采样时刻。
`current_A = shunt_voltage_V / shunt_resistance_ohm`，本次分别除以 `0.005 Ω`。
原始 `udV` 按微伏换算，正负号保留。`measured_voltage_V` 是仪器测得的对地电压，不按电阻标签猜电源轨。
QIS 电流／功率列独立保留，不覆盖由原始压降计算的电流。
转换检查时间戳连续、字段完整、数值有限；不完整测量不会标为完成。

保留压降和采集时使用的阻值后，可以按实测电阻重新计算电流。
对每一路，QIS 功率在其他条件相同时可按 `P_new = P_old × R_old / R_new` 修正；
电流同样按该比例修正。若两路新阻值不同，应分别修正后重新求和，不能用一个比例修正整个 `Tot`。
1 ms / 10 ms 是输出平均区间；不要把平均电压乘平均电流当成区间平均功率。
`raw.csv` 保留的是 QIS 输出值，不包含区间内所有底层 ADC 点；需要底层模拟点可用 `--resample 4us` 短测。
当前固定时长导出按 256 MB 分卷；大规模全速采集需要另行处理分卷，本脚本只接受完整覆盖请求时长的单卷。

## 建议的测试顺序

1. **检查接线、内存与身份**：先按[硬件核对步骤](../setup/qtl2312-dram.md#上电前后怎样核对)完成断电检查，上电后用 `free -h` 确认 Linux 识别内存；这不能替代槽位与供电轨核对。
   没有常驻服务时，运行下面的 inspect，不开始录制，也不设置阻值：

   ```bash
   ./bin/Quarch --device USB::QTL2312-01-578 --inspect \
     --active-channels POWER_1 POWER_2
   ```

   预期控制器 QTL2312、夹具 QTL2628、可用功率通道 POWER_1/POWER_2/Tot、录制 OFF。初次读回 0 Ω 表示未配置，不能据此测有效电流。

2. **先做 3 秒短测**：它会设置并读回两路 5 mΩ，同时保存压降、电流和功率：

   ```bash
   .venv/bin/python -m quarch_capture.dram \
     --device USB::QTL2312-01-578 --channel Tot \
     --seconds 3 --resample 1ms --shunt-mohm 5 5
   ```

   在本次新建目录中检查 `run.json`：`status` 为 `complete`、阻值各为 `0.005`、`samples` 为 3000、间隔 `0.001 s`、仅两路有效输入。
   CSV 不含 CH3/CH4；两路时间戳一致且连续，逐点 `current_A = shunt_voltage_V / 0.005`，R5 标签保留。
   不能只因命令退出就判定采集完整。

3. **离线复核能耗**：将 `DRAM_RUN_DIR` 换成上一步实际输出目录；这些命令不连接仪器：

   ```bash
   DRAM_RUN_DIR=results/dram/替换为实际运行目录
   .venv/bin/python -m quarch_capture.energy "$DRAM_RUN_DIR/raw.csv" --channel Tot --seconds 3
   .venv/bin/python -m quarch_capture.energy "$DRAM_RUN_DIR/raw.csv" --channel POWER_1 --seconds 3
   .venv/bin/python -m quarch_capture.energy "$DRAM_RUN_DIR/raw.csv" --channel POWER_2 --seconds 3
   ```

   检查 `requested_duration_matches: true`；`Tot` 与两路功率之和应只差输出舍入量，三份能耗也应满足合计关系。
   原始功率单位 `uW` 会换算成 W；能耗输出 `energy_J`、`energy_Wh`，不是电流相加后乘任意电压。

4. **实际任务与退出检查**：通过短测后再采 30 秒空闲基线，再用相同配置测内存访问／SQL 负载。
   固定时长脚本在运行期间执行负载时，能耗覆盖整个采集窗口，包含窗口内空闲时间；只测任务段可使用前面的 start/stop。
   实时 watch 在 Ctrl+C 后应回到 `idle`、`device_record_state: OFF`；新段录制由下一次 start/watch 开始。
   低频输出只能观察区间平均，不据此推断 4 μs 瞬态或全部 DRAM 能耗。

5. **离线自动测试**：无需接仪器，完整仓库测试中真实 DuckDB CLI 测试需要 `duckdb` 在 PATH 上：

   ```bash
   .venv/bin/python -m unittest discover -s tests -p test_external_shunts.py -v
   .venv/bin/python -m unittest discover -s tests -v
   ```

需要直接读回时，退出采集服务后激活 `.venv`，执行 `python -m quarchpy.run terminal` 并选择当前 USB 目标，
可查询 `*IDN?`、`FIX:IDN?`、`REC?`、`FIX:POWER_1:SHUNT?`、`FIX:POWER_2:SHUNT?`。
本机已验证设置命令为 `FIX:POWER_1:SHUNT 5 MilliOhm` 和 `FIX:POWER_2:SHUNT 5 MilliOhm`，
必须在录制 OFF 时设置并读回；日常用双路脚本自动执行即可。读取／设置的是仪器换算参数，不是对实物电阻进行测阻。

## 内存读取／写入能耗对比

新增[内存负载脚本](../../tools/benchmark/dram_memory_energy.py)，复用双路采集程序，不修改仪器供电。
先正常结束自己的测量并关闭空闲的常驻服务，再运行：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python tools/benchmark/dram_memory_energy.py \
  --device USB::QTL2312-01-578 --channel Tot \
  --seconds 15 --runs 3 --buffer-mib 1024 --threads 2 --resample 10ms
```

默认分配并预先写入 **总计 1 GiB** 的匿名内存，分成 2 个 512 MiB 缓冲区，全部阶段保持驻留。
读取阶段用 NumPy 对 `uint64` 缓冲区反复求和；写入阶段反复填充交替的 1／2。
两者在整个采样窗口内运行，包含窗口外的 2 秒预热及采集配置／导出时间；每段设备采集 15 秒、1500 点，段间暂停 5 秒。
三轮顺序依次为 `idle → read → write`、`read → write → idle`、`write → idle → read`，减少固定顺序造成的偏差。
脚本检查缓冲区不超过可用内存的四分之一，并在检测到主机换页时拒绝将对比标为完成。
负载线程各使用一个物理核心；主控和由本次启动的 QIS 使用其他核心，避免与负载共用物理核心。
CPU 分配写入 `run.json`，退出后恢复主控原有的 CPU affinity；不修改系统 CPU 调度配置。
若 QIS 已由其他程序启动，其 CPU 分配由原程序决定。缓冲区溢出会使该轮失败，保留原始诊断数据，不补齐缺失点或冒充完整测量。

默认输出到 `results/dram_memory/<UTC时间>/`：

- 每个 `rXX_idle/read/write/` 保存双路采集的全部原始与派生 CSV、阻值和能耗 JSON；不与之前的诊断结果混用。
- `comparison.csv`：三种阶段各三次，分别记录 POWER_1/R1、POWER_2/R5 和 Tot，共 27 行。
- `summary.csv`：各阶段、各通道的三次平均功率、轮间标准差、平均能耗，以及相对同轮空闲的功率／能耗增量。
- `run.json`：软件版本、工作集、轮次顺序、工作线程计数、换页检查和完成状态。线程逻辑字节数覆盖完整负载生存期，不能直接当作采样窗口内的 DRAM 总线流量。

增量能耗按 `该段能耗 − 同轮空闲平均功率 × 该段采样时长` 计算；比较不同长度的任务时应同时看时长、功率和能耗。
测试结束后，按本页[实时功率](#实时功率)命令重新启动常驻服务，即可继续手动测量。

该测试是带 CPU 处理的顺序内存负载，不是只测某条内存指令的实验；普通写入还可能产生缓存行读取和回写流量。
Linux 分配页面，不能保证工作集只位于 DIMM1；若有其他内存条，当前仪器仍只看到已接 riser 的路径。
QIS、采集 CSV、系统后台活动也会使用内存。电源轨识别和供电覆盖尚未完成，因此结果用 R1、R5 命名，
不把两路合计直接当作全部 DRAM 能耗，也不从这些读数推定 R5 是 VPP。

2026-11-27 的[三轮实测结果与原始数据](../../experiments/2026-11-27-dram-memory/README.md)已归档：
两路合计平均功率由空闲 0.904 W 增至读取 2.479 W、写入 2.973 W，主要变化在 CH2/R5。
首次四线程写入发生缓冲区溢出，失败记录单独保留；改用两线程并分配独立物理核心后的九段均完整通过。

## 原 `Tot` 报错

最初四路阻值未配置；夹具电流定义还使用 `A` 组名，而功率引用 `current`，
旧程序没有正确解析依赖，导致 QIS 只输出电压和压降，找不到 `Tot` 功率列。
程序已统一组名、补充必要电流换算，并支持按有效输入构建合计。
本次单 riser 配置中合计严格为 CH1+CH2，仍不表示已证明所有 DRAM 供电轨都被覆盖。
没有修改厂商代码，也不发送供电切换、软重启或恢复出厂命令。
此前按“两块 riser／四路”生成的本机诊断文件保留原样，属于旧假设下的测试，不作为本次单 riser 的正式测量。

## 本机验证

修正为单 riser 后，1000 Hz、3 秒采集获得 3000 个连续采样点；`raw.csv` 和两路独立 CSV
均不含 CH3/CH4。逐点核对 `Tot = POWER_1 + POWER_2`，输出舍入差不超过 1 µW。
实时 watch 短测收到数据，Ctrl+C 后回到 idle、录制 OFF；服务只暴露 POWER_1、POWER_2 和 Tot。
119 项自动测试通过，无跳过，包含“固件已有电流定义、但分流阻值仍为零时拒绝采集”的回归检查。
本次验证确认软件采集与映射，不替代电源轨识别或测量精度校准。
该 3 秒短测为 2026-11-27 的软件验收记录，不是 SQL 性能实验：合计平均功率 1.207571 W、能耗 3.622714 J，
仅表示当时接线下两条路径的仪器输出。原始输出位于本机 `results/dram/20261127_single_dimm1_validation/`，
调试记录位于 `validation/dram/`，均未随代码上传，不将此前四路诊断数据混入新结果。

官方参考：[QTL2628](https://quarch.com/products/external-shunt-pam-fixture/)、
[QTL2312](https://quarch.com/products/power-analysis-module/)。
