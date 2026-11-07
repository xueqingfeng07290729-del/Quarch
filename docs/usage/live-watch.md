# 终端查看功率：Quarch watch

## 安装和启动

先完成[安装](../setup/installation.md)，包括把 `bin/Quarch` 链接到 PATH。
入口会自动使用仓库中的 `.venv`，无需每次激活虚拟环境，也不需要 sudo。
首次安装命令入口时执行：

```bash
cd /home/ubuntu/Quarch
mkdir -p ~/.local/bin
ln -s /home/ubuntu/Quarch/bin/Quarch ~/.local/bin/Quarch
export PATH="$HOME/.local/bin:$PATH"
Quarch watch --help
```

如果链接已经存在，直接使用。新终端也需要 `~/.local/bin` 在 PATH 中；详细准备步骤见
[安装指南第 8 步](../setup/installation.md#8-安装-quarch-命令)。

QIS 与常驻服务沿用已有方案，不启动 QPS。服务尚未启动时，在一个终端执行：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m quarch_capture serve \
  --device USB::QTL2843-03-002 --channel L2 --csv off
```

服务等待命令期间录制为 OFF。更新代码前已运行的旧服务，需要在空闲时关闭并重启一次；
新服务的 `status` 包含 `live_watch: true`。

已有环境更新时，先用 `Quarch status` 确认 `state: idle`、`device_record_state: OFF`
和 `reader_active: false`，然后执行：

```bash
cd /home/ubuntu/Quarch
Quarch shutdown
git pull --ff-only
.venv/bin/python -m quarch_capture serve \
  --device USB::QTL2843-03-002 --channel L2 --csv off
```

如果正在测 SQL，等本段测量正常完成后再更新。`serve` 会持续占用当前终端；另开终端运行 watch。

## 查看功率和停止

在另一个终端的任意目录运行：

```bash
Quarch watch --L2 --10ms
Quarch watch --L2             # 默认也是 10ms，100 个采样点/秒
Quarch watch --L1 --100ms     # 也可指定 L1 或 L3 及其他间隔
```

`--L1`、`--L2`、`--L3` 必须且只能指定一个。间隔也可以写成 `--interval 10ms`。
参数要求正整数 `ms` 或 `us`，最小 125us；极短间隔能否持续逐点输出，还受 QIS、主机和终端吞吐限制。

标准输出为 CSV 格式，例如下面的数值仅用于展示格式：

```text
time_s,L2_power_W
0.000000,73.214000
0.010000,74.906000
0.020000,72.518000
```

`time_s` 是设备本次录制的时间戳，`L2_power_W` 是该输出间隔内的平均瞬时功率，单位 W。
每行对应一个真实采样点；USB / QIS 的缓冲可能使多行同时到达终端，10ms 指采样点间隔。
终端提示写入标准错误，不混入数据列。

按 **Ctrl+C** 停止本次录制、等待清理并退出 watch；不要等待关闭 QIS 才停止采样。
常驻服务随后保持连接并回到空闲状态，设备录制 OFF、数据读取线程停止，可再次运行 watch 或正常的 `start`。
watch 临时修改的输出间隔会恢复为服务原有配置。可查询确认：

```bash
Quarch status
```

连接断开或输出管道关闭时，服务也会停止这次 watch。若其他测量已经运行，watch 会报错，不会中断它。
watch 默认不保存原始采样 CSV；停止时仍沿用服务的能耗 JSON 汇总。需要文件时可自行重定向标准输出：

```bash
Quarch watch --L2 > power.csv
```

同一台电脑既被测又控制仪器时，终端逐点打印及更频繁读取会增加测量程序自身的开销。
正式测 SQL 的能耗继续使用原来的开始／结束接口；watch 适合观察功率变化。

## 实现方式

watch 复用已有的本地采集服务，通过 Unix socket 接收采样点。控制进程保持唯一设备连接，
避免另一个 watch 进程直接打开 USB 并与现有测量竞争。

| 文件 | 职责 |
| --- | --- |
| [`bin/Quarch`](../../bin/Quarch) | 解析入口所在仓库路径，自动切换到 `.venv/bin/python`，支持从任意目录调用 |
| [`capture.py`](../../quarch_capture/capture.py) | 把 `watch` 子命令分发到独立模块，处理退出码和终端错误 |
| [`watch.py`](../../quarch_capture/watch.py) | 解析必选通道、默认 10ms 和 `--10ms` 简写；收发逐点消息并打印；处理断连和退出 |
| [`commands.py`](../../quarch_capture/commands.py) | 接收本地 socket 的 watch 请求，交给流式处理函数 |
| [`measurement.py`](../../quarch_capture/measurement.py) | 管理本次会话、临时输出间隔、停止后恢复配置和能耗汇总 |
| [`qis_backend.py`](../../quarch_capture/qis_backend.py) | 通过 QIS 启停设备、设置输出间隔和读取缓冲数据 |
| [`stream_data.py`](../../quarch_capture/stream_data.py) | 校验设备时间戳、将功率转换为 W、积分能耗并回调每个有效采样点 |
| [`client.py`](../../quarch_capture/client.py) | 复用按请求编号取消、按会话编号停止及通信失败恢复逻辑 |

采样数据的传送顺序为：

```text
QTL2843 → USB → QIS → QisBackend → EnergySink
                                      ↓ 每个有效采样点
                                  有界队列
                                      ↓ Unix socket / JSON 行
                                Quarch watch → 终端
```

`EnergySink` 使用设备原始时间戳，把所选通道的 `PInst` 从 mVA 除以 1000 转为 W，
再把每个有效采样点放入队列；没有通过重复读取最后一个功率值生成输出。
队列有容量限制，终端持续跟不上时会报错并停止测量，避免无限占用内存或悄悄丢点。

每次 watch 使用独立的请求编号和会话编号。Ctrl+C 或 SIGTERM 会请求停止自己的会话；
启动阶段未收到确认时，按请求编号取消。服务端也会在客户端断连时停止本次采集。
这些处理不会停止随后由其他客户端启动的会话。

watch 运行期间临时调整输出间隔和读取等待时间；停止时先让设备停止录制、排空缓冲，
然后恢复服务原来的间隔及读取配置。普通 `start` 的批量读取设置保持原有行为。

## 验证

无需连接仪器的回归测试：

```bash
cd /home/ubuntu/Quarch
.venv/bin/python -m unittest discover -s tests -v
```

2026-11-07 已通过全部 76 项测试。新增的
[`test_watch.py`](../../tests/test_watch.py) 覆盖默认间隔、必选通道、时间戳和单位转换、
Ctrl+C / SIGTERM、断连、关闭输出管道、已有会话保护、启动失败恢复和读取错误。

同日使用 `QTL2843-03-002` 实机验证 `Quarch watch --L2`、`--10ms` 和 `--100ms`：
输出相邻设备时间戳分别相差 0.01s、0.01s 和 0.1s；Ctrl+C 后均确认
`state: idle`、`device_record_state: OFF`、`reader_active: false`，服务默认间隔恢复为 0.01s。
