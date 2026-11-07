# 安装准备：QuarchPy + headless QIS

本指南复现当前服务器已经验证的安装路径：**Python 虚拟环境 → 厂商 QuarchPy 源码 → 配套 Java → QIS 共享运行包 → USB 权限 → 身份查询**。不需要安装完整桌面环境，也不启动 QPS。

## 已验证版本

| 组件 | 版本 / 来源 |
| --- | --- |
| Python | `3.12.3`，Ubuntu 的 Python 3 |
| QuarchPy | `2.2.24` |
| 厂商源码提交 | `e4aa8a5ed0ee15b223504f8e254db6bb5bc123e5` |
| `quarchpy-binaries` | `0.0.4`，本机提供 Java `25.0.1` |
| 共享运行包 | `QPS_1.52.zip`，其中包含 QIS `1.54` |
| 设备 | `QTL2843-03-002`，固件 `5.119` |

下面使用 `/home/ubuntu/Quarch` 作为项目目录。源码依赖、运行包和虚拟环境只保存在本机，已由 `.gitignore` 排除。

## 1. 取得本仓库并安装基础工具

先安装系统工具：

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 python3-venv usbutils
```

新机器首次取得文档和检查工具时，可执行：

```bash
git clone https://github.com/xueqingfeng07290729-del/Quarch.git /home/ubuntu/Quarch
```

如果项目目录已经存在，直接进入目录，跳过这条 clone 命令。

```bash
cd /home/ubuntu/Quarch
```

本机第一次创建虚拟环境时曾提示缺少 `ensurepip`，后来安装 `python3.12-venv` 后解决；在 Ubuntu 的默认 Python 环境通常安装 `python3-venv` 即可。如果提示指定版本，就安装提示中的 `python3.X-venv`。

## 2. 创建虚拟环境，下载 QuarchPy 源码

```bash
cd /home/ubuntu/Quarch
python3 -m venv .venv
source .venv/bin/activate
```

第一次下载厂商依赖时执行：

```bash
git clone https://github.com/QuarchTechnologyLtd/QuarchPy.git QuarchPy
git -C QuarchPy checkout --detach e4aa8a5ed0ee15b223504f8e254db6bb5bc123e5
```

这会把新下载的依赖固定到本次已验证版本。如果已经存在 `QuarchPy/`，先检查其版本和本地改动，不要重复 clone 或覆盖已有工作；当前服务器已经有这份依赖。

## 3. 安装 QuarchPy 与 Java 依赖

在已激活的虚拟环境中执行：

```bash
python -m pip install quarchpy-binaries==0.0.4
python -m pip install -e ./QuarchPy
python -m pip check
```

QuarchPy 会安装它声明的依赖，包括 USB 库、网络发现库、NumPy、pandas 等。`quarchpy-binaries` 提供配套 Java；本方案不依赖系统中另外安装的 `java` 命令。安装 Python 包使用虚拟环境内的普通用户，不需要 `sudo pip`。

验证包和 Java 路径：

```bash
python - <<'PY'
import importlib.metadata as metadata
import quarchpy
import quarchpy_binaries
print('QuarchPy:', metadata.version('quarchpy'))
print('Source:', quarchpy.__file__)
print('Java home:', quarchpy_binaries.get_jre_home())
PY
```

## 4. 下载并安装包含 QIS 的共享运行包

当前 QuarchPy 启动器从 `quarchpy/connection_specific/QPS/qis/qis.jar` 寻找 QIS，因此仅安装 Python 代码和 Java 后，还要确认该共享运行包已经到位。

```bash
mkdir -p downloads
curl -fL --continue-at - --retry 2 --connect-timeout 15 --max-time 900 \
  --output downloads/QPS_1.52.zip \
  https://quarch.com/software_update/qps/QPS_1.52.zip

python tools/setup/install_qis_runtime.py --archive downloads/QPS_1.52.zip
```

这个 ZIP 虽然以 QPS 命名，但其中也包含 QIS 及其运行文件；安装共享文件不等于启动 QPS。安装工具会校验本次已验证安装包的 SHA256、检查 ZIP 完整性、安装缺失组件，并为 Java 的可执行文件设置 `0755` 权限。它不会启动 QIS 或 QPS，也不会用不同版本静默覆盖已有运行包。

本次下载归档的 SHA256：

```text
f22405a2cf5dc92f01b43da7a5245ae2b731dc6c1ea2652e6211a5b64517147d
```

该值记录本次已验证归档，用于复现相同安装文件，不是厂商数字签名。若厂商修改了同名下载文件导致校验不同，应重新核对版本和兼容性。

只检查已安装文件和权限时：

```bash
python tools/setup/install_qis_runtime.py --archive downloads/QPS_1.52.zip --check-only
```

## 5. 配置普通用户 USB 权限

先按 [接线简图](current-wiring.md)给仪器供电，并连接 USB。以下规则仅针对当前设备和 `ubuntu` 用户；其他机器应先修改规则中的用户名、必要时修改序列号。

```bash
sudo install -m 0644 config/udev/99-quarch-qtl2843.rules \
  /etc/udev/rules.d/99-quarch-qtl2843.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=usb \
  --attr-match=idVendor=16d0 --attr-match=idProduct=0449 \
  --attr-match=serial=QTL2843-03-002
sudo udevadm settle --timeout=10
```

重插 USB 后，以普通用户执行下一步。规则已在本机重插后验证，不依赖固定的 USB Bus/Device 编号。更多排查见 [USB 注意事项](usb-notes.md)。

## 6. 验证原生 USB 命令通信

```bash
cd /home/ubuntu/Quarch
source .venv/bin/activate
lsusb -d 16d0:0449
python tools/setup/check_connection.py --backend usb
```

本机预期包含以下输出：

```text
Part#:      QTL2843-03
Serial#:    QTL2843-03-002
Recording state: OFF
Connection check passed
```

检查工具显示运行 UID；应使用普通用户验证，而不是仅验证 sudo 是否成功。它查询设备身份和录制状态，不开始录制、不切换电脑的交流电源。

## 7. 验证 headless QIS

```bash
python tools/setup/check_connection.py --backend qis
```

工具内部调用 `startLocalQis(headless=True)`，然后查询 QIS 版本、发现 USB 设备并读取身份。它只启动 QIS，不启动 QPS。正常情况下会看到 QIS `v1.54` 和 `USB::QTL2843-03-002`；结束时会关闭由本次检查创建的 QIS，保留检查前就已经运行的 QIS。

至此，安装、Java 执行权限、普通用户 USB 权限和 QIS 通信链路均有实际检查。录制数据还需另行配置采集时长、输出间隔和存储位置；本工具本身不采集功率曲线。

## 常见安装问题

| 提示 / 现象 | 处理方向 |
| --- | --- |
| `ensurepip is not available` | 安装对应 Python 版本的 `venv` 系统包，再创建虚拟环境 |
| `ModuleNotFoundError` | 确认使用 `.venv/bin/python`，并完成 `pip install -e ./QuarchPy` |
| 启动 QIS 时提示缺少 QPS 组件 | 按第 4 步安装共享运行文件；这不要求启动 QPS GUI |
| Java `Permission denied` | 使用运行包安装工具检查 / 修正 Java 可执行权限 |
| sudo 可以访问 USB，普通用户不行 | 按第 5 步配置 udev，再在普通用户环境中验证 |
| 无设备或设备 busy / locked | 查看供电、USB 线缆、权限和已有采集进程，见 USB 注意事项 |

## 厂商资料

- [QuarchPy 厂商源码与安装说明](https://github.com/QuarchTechnologyLtd/QuarchPy)
- [QIS：明确支持无 GUI 服务器](https://quarch.com/file/qis-quarch-instrument-server/)
- [Quarch Linux USB 权限 FAQ](https://quarch.com/support/faqs/usb/)
- [QTL2843 技术手册](https://quarch.com/wp-content/uploads/2024/09/QTL2843-IEC-Mains-PAM-Technical-Manual-1.pdf)
