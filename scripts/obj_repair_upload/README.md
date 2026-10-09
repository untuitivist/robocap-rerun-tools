# 物品动捕文件统计

双击 `inspect_object_mocap.bat`。与 `scripts/repair_upload` 一样，本地搜索目录默认
`F:\`，共享目录可留空，也可输入真实的 UNC 或映射盘路径。无需异常 CSV。
CMD 显示扫描进度，结束后保留窗口；同时保存 `run.log`。

本工具只检查和统计本地原始导出，不上传、不重命名、不修改源数据。

也可以从仓库根目录运行：

```bat
scripts\obj_repair_upload\inspect_object_mocap.bat --root "F:\." --root "\\SERVER\F" --output "_artifacts\obj_check_01"
```

共享路径可省略。输出路径必须是新目录，以免覆盖之前的统计。直接使用 Python：

```bat
uv run python scripts\obj_repair_upload\inspect_object_mocap.py --root "F:\."
```

## 输出

默认保存在 `_artifacts/obj_repair_upload/<运行时间>/`：

| 文件 | 内容 |
| --- | --- |
| `report.html` | 中文统计表，可按物品、动作、参与者或 Session 筛选，离线可用 |
| `objects.csv` | 每个物品后缀的录制数、源副本数、各格式文件数、参与者、动作和表头状态 |
| `sessions.csv` | 每个录制标识的物品列表，以及 Body、Tracker、Unnamed 文件数量 |
| `files.csv` | 所有动捕文件的路径、后缀、身份、大小、表头名称和异常 |
| `scan_errors.csv` | 无法访问的目录、读取失败或扫描过程中发生变化的文件 |
| `summary.json` | 扫描范围、是否完整及汇总计数 |
| `run.log` | 扫描进度与统计结果 |

所有报告为 UTF-8 无 BOM。Excel 若未自动识别中文，请使用“数据 → 从文本/CSV”选择 UTF-8。
退出码 `0` 表示扫描完成，`1` 表示存在未能扫描的位置，`2` 表示参数有误。
扫描完成不等于每个文件内容有效，具体状态以报告为准。

## 统计口径

- 在 `mocap*` 目录下递归检查 `.csv/.trc/.bvh/.xrs/.c3d`，也检查其他目录中使用完整
  `mocap-动作-S序号-参与者-次数p[导出序号]-物品名` 文件名的动捕导出。
- 保留完整物品后缀。例如 `BigBox0`、`BigBox1` 分开统计，`Open box-left` 不截成 `left`。
  不限制为当前已知的道具清单，避免漏掉新物品；仅大小写不同的名称合并，并保留别名。
  早期不规范目录会先剥离与目录相同的文件名前缀，无法解析的人员/Session 字段留空并标记。
- `Body0/1/2` 和人体派生名称、`Tracker0/start/end`、`Unnamed` 分开计数。其他后缀为
  `object_candidate`，尚不能仅凭文件名证明它对应真实物品。识别出人体骨架表头时归为 Body，
  即使文件被命名为 `Chair.csv` 也不会算作物品。
- 一个录制中，同一物品的 CSV、TRC、BVH 算三个文件，但该物品的录制数只算一次。
  同名 Session＋完整动捕目录名按一个录制标识汇总；不同根目录的副本仍分别计入文件数和
  源副本数，**不声称这些副本字节一致**。重复/重叠的根目录只扫描一次。
- 文件头默认最多读取约 64 KiB，可用 `--header-bytes` 增加。报告中的帧数和帧率是表头
  声明值，没有全量核验轨迹或补帧。`header_ok` 只说明识别到了相应表头。
- 空文件、Git LFS 占位文件、无法识别/解码的表头均单独标明；C3D 只列出文件，不解析二进制内容。
- 跳过系统目录、Git、工具暂存区、标定目录、符号链接和目录联接；不展开 ZIP/RAR。
  没有具名导出只说明当前扫描范围没有对应名称，不能据此断定采集时没有贴点或录制物品。
