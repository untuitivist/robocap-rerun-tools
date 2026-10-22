# 动捕重新导出（双击 BAT）

只处理之前问题清单中动捕相关的 74 个 Session，不处理腕部 IMU/视频。

1. 双击 `01_find_batches.bat`：自动查找原始 NOKOV 批次，打开报告。点“复制路径”或“复制全部批次路径”，按报告列出的录制在 NOKOV 中重新导出。
2. 导出结束后，双击 `02_copy_reexports.bat`：重新搜索，按完整录制名复制回唯一对应的 Session。不要在导出进行中运行。

不需要选 Markdown、填盘符或共享地址。F 盘只是示例，其他本地盘、映射盘和当前账号可访问的已有共享同样适用；未共享或无权限的盘无法搜索。

复制前检查 Body0 的 CSV/TRC/BVH、Tracker0 的 CSV/TRC、原清单及旧目录中已有的动捕文件，以及新发现的额外人体和物品配套导出。内容无效、缺失、名字冲突或多份副本不乱配，报告中会说明。重复嵌套的同名 mocap 目录会合并一层；同名文件内容不同则停止该 Session。

原始 NOKOV 文件不动。旧动捕目录保存在 Session 同级的 `_artifacts\mocap_reexport_backups\Session名\批次编号\old`，可按其中 `journal.json` 查找；不会删除备份。备份不会被正常数据扫描/上传当成原件。复制不代表问题已解决，尤其 G 类时钟/动作配对仍需人工复核。随后使用已有检查/上传工具，不会在这里自动上传。

报告和日志：工具根目录 `_artifacts\mocap_reexport\运行时间_编号\`。`report.html` 看批次与逐 Session 对照；`batch_paths.txt` 是纯路径；`sessions.csv`、`scan_errors.csv` 查找不到/重复/不可访问；第二步另有 `copy_results.csv`。

维护者可附加 `--root "D:\数据"`（可重复），或在第一步附加 `--inventory "WizTree.csv"` 离线读清单。第二步不接受离线清单，必须检查当前实际文件。内置映射来源为 2026-10-22 核对的原始 manifest，不按时间猜配，也不宣称这些历史问题目前仍全部存在。
