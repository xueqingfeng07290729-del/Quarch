# 正式实验结果

本目录只保存经过核对、附方法说明与校验和的实验快照。日常运行输出保存在 Git 忽略的 `results/`，
调试材料保存在 `validation/`；数据库和厂商二进制不随结果上传。

| 日期（UTC） | 实验 | 采集数量与注意事项 |
| --- | --- | --- |
| 2026-11-09 | [QTL2312：未压缩 SF30 的 TPC-H SSD 能耗](2026-11-09-tpch-sf30-ssd/README.md) | Q1–Q22 × 3，加 Q19 复测 × 3；原始 Q19/Q20 波动保留 |
| 2026-11-11 | [QTL2312：RTX 5050 静息与矩阵负载](2026-11-11-rtx5050-gpu/README.md) | 6 段采集、1,820 点；含 3 次温度相近的复测，功率差异待核查 |

每份归档包含 `README.md`、运行配置、逐次结果、负载 SQL 或源码、来源清单及 `SHA256SUMS`。
在仓库根目录离线复核，无需连接仪器：

```bash
python3 tools/analysis/verify_published_results.py  # SSD
python3 tools/analysis/verify_gpu_results.py        # GPU
```
