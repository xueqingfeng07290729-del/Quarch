# 正式实验结果

本目录只保存经过核对、附方法说明与校验和的实验快照。日常运行输出保存在 Git 忽略的 `results/`，
调试材料保存在 `validation/`；数据库和厂商二进制不随结果上传。

| 日期（UTC） | 实验 | 采集数量与注意事项 |
| --- | --- | --- |
| 2026-11-09 | [QTL2312：未压缩 SF30 的 TPC-H SSD 能耗](2026-11-09-tpch-sf30-ssd/README.md) | Q1–Q22 × 3，加 Q19 复测 × 3；原始 Q19/Q20 波动保留 |

每份归档包含 `README.md`、运行配置、逐次结果、查询 SQL、来源清单及 `SHA256SUMS`。
当前归档可在仓库根目录用 `python3 tools/analysis/verify_published_results.py` 离线复核。
