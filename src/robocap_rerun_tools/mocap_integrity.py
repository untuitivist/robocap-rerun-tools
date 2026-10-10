"""Check each Mocap entity's export set and action-specific required bodies/props."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from .file_integrity import result
from .obj_inventory import filename_parts, name_category

BODY_FORMATS = (".trc", ".bvh", ".csv")
RIGID_FORMATS = (".trc", ".csv")
ENTITY_FORMATS = {*BODY_FORMATS, ".tsv", ".xrs", ".trb", ".xrb"}
BODY_NAME = re.compile(r"body\d+", re.IGNORECASE)
ACTION_DIRECTORY = re.compile(r"^mocap[-_]([a-z]+\d+)(?:[-_]|$)", re.IGNORECASE)
DEFAULT_REQUIREMENTS = (
    Path(__file__).resolve().parents[2] / "scripts/session_inspection/mocap_requirements.csv"
)


def _names(value: str) -> tuple[str, ...]:
    names = tuple(item.strip() for item in value.split(";") if item.strip())
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("刚体名称重复")
    if any(re.search(r"[\\/\x00-\x1f]", name) for name in names):
        raise ValueError("刚体名称不能包含路径或控制字符")
    return names


def read_requirements(path: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """An absent action is unknown; an explicit row with empty objects means no props."""
    requirements = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["action_id", "additional_bodies", "objects"]:
            raise ValueError("清单表头必须为 action_id,additional_bodies,objects")
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"清单第 {number} 行列数不符")
            action = row["action_id"].strip().upper()
            if not re.fullmatch(r"[A-Z]+\d+", action) or action in requirements:
                raise ValueError(f"清单第 {number} 行动作编号无效或重复: {action}")
            bodies, objects = _names(row["additional_bodies"]), _names(row["objects"])
            if any(not BODY_NAME.fullmatch(name) for name in bodies):
                raise ValueError(f"{action}: additional_bodies 只接受 Body 加编号")
            if any(name_category(name) != "object_candidate" for name in objects):
                raise ValueError(f"{action}: 帽子、人体和 Unnamed 不能列为物品")
            requirements[action] = {"bodies": bodies, "objects": objects}
    return requirements


def check_mocap_exports(mocap_dirs, mocap_files, add, *, requirements_path=None):
    """Append checks using the Session report's standard path/category/result interface."""
    groups = {}
    actions = set()
    definitions = set()
    for directory in mocap_dirs:
        if match := ACTION_DIRECTORY.match(directory.name):
            actions.add(match[1].upper())
    for path in mocap_files:
        if path.suffix.lower() == ".mars":
            label, _ = filename_parts(path)
            if name_category(label) != "unnamed":
                definitions.add(label)
        if path.suffix.lower() not in ENTITY_FORMATS:
            continue
        label, capture = filename_parts(path)
        if capture:
            actions.add(capture.action_id)
        category = name_category(label)
        if category == "unnamed":
            continue
        # Formats of different takes or different Body indices must never fill each
        # other's missing slots. The export stem retains the complete capture prefix.
        group = groups.setdefault(
            path.stem.casefold(), {"stem": path.stem, "label": label, "files": defaultdict(list)}
        )
        group["files"][path.suffix.lower()].append(path)

    by_label = defaultdict(list)
    for group in groups.values():
        by_label[group["label"].casefold()].append(group)
    for label in sorted(definitions):
        if label.casefold() not in by_label:
            group = {"stem": label, "label": label, "files": {}}
            groups["definition:" + label.casefold()] = group
            by_label[label.casefold()].append(group)

    configured = None
    action = next(iter(actions)) if len(actions) == 1 else None
    if action and action.startswith("HH"):
        bodies = sorted(label for label in by_label if BODY_NAME.fullmatch(label))
        add(
            "mocap*/Body*",
            "mocap",
            result(
                "ok" if len(bodies) >= 2 else "missing",
                f"{action} 是多人交互动作，至少需要 2 个人体；"
                f"发现 {len(bodies)} 个: {'; '.join(bodies) or '无'}。物品不计作人体。",
            ),
            "HH 多人采集",
        )
    path = requirements_path if requirements_path is not None else DEFAULT_REQUIREMENTS
    try:
        rules = read_requirements(Path(path))
        configured = rules.get(action)
    except FileNotFoundError:
        if requirements_path is not None:
            add("mocap_requirements.csv", "mocap", result("read_error", "指定的清单不存在"))
    except (OSError, ValueError, csv.Error) as exc:
        add("mocap_requirements.csv", "mocap", result("invalid_format", str(exc)))
    if len(actions) > 1:
        add(
            "mocap*/action",
            "mocap",
            result("ambiguous", "发现多个动作编号: " + "; ".join(sorted(actions))),
            "动作刚体清单匹配",
        )
    if configured is None:
        add(
            "mocap_requirements.csv",
            "mocap",
            result(
                "requirements_unknown",
                f"动作 {action or '未识别'} 未配置必需物品及额外人体；"
                "仅核对已发现刚体，不能确认是否有整个刚体漏导出。",
            ),
            "动作刚体清单",
        )
    else:
        add(
            "mocap_requirements.csv",
            "mocap",
            result(
                detail=f"动作 {action}；额外人体: {'; '.join(configured['bodies']) or '无'}；"
                f"必需物品: {'; '.join(configured['objects']) or '无'}"
            ),
            "动作刚体清单",
        )

    expected = ["Body0", "Tracker0"]
    if configured is not None:
        expected += list(configured["bodies"]) + list(configured["objects"])
    for label in dict.fromkeys(name.casefold() for name in expected):
        matches = by_label[label]
        formats = BODY_FORMATS if BODY_NAME.fullmatch(label) else RIGID_FORMATS
        role = (
            "人体" if BODY_NAME.fullmatch(label) else "帽子" if label == "tracker0" else "物品刚体"
        )
        display = next(name for name in expected if name.casefold() == label)
        if not matches:
            for suffix in formats:
                add(
                    f"mocap*/{display}{suffix}",
                    "mocap",
                    result("missing", f"未找到 {display} 的 {suffix} 导出（可带录制名称前缀）"),
                    role,
                )
        elif len(matches) > 1:
            add(
                f"mocap*/{display}",
                "mocap",
                result(
                    "ambiguous",
                    "同一刚体存在多个录制前缀: " + "; ".join(g["stem"] for g in matches),
                ),
                role,
            )

    objects = set()
    for group in groups.values():
        label = group["label"]
        category = name_category(label)
        if category == "body" and not BODY_NAME.fullmatch(label):
            continue  # Auxiliary hand/segment exports do not replace a complete Body.
        if category == "object_candidate":
            objects.add(label)
        role = (
            "主人体"
            if label.casefold() == "body0"
            else "额外人体/追踪对象"
            if category == "body"
            else "帽子/其他 Tracker"
            if category == "tracker"
            else "物品刚体"
        )
        formats = BODY_FORMATS if category == "body" else RIGID_FORMATS
        for suffix in formats:
            matches = group["files"].get(suffix, [])
            state = "missing" if not matches else "ambiguous" if len(matches) > 1 else "ok"
            add(
                matches[0] if len(matches) == 1 else f"mocap*/{group['stem']}{suffix}",
                "mocap",
                result(
                    state,
                    f"{label} 必需 {suffix}；发现 {len(matches)} 个文件。"
                    + ("；".join(str(p) for p in matches) if len(matches) > 1 else ""),
                ),
                role,
            )
    add(
        "mocap*/objects",
        "mocap",
        result(detail="已发现的物品名称: " + ("; ".join(sorted(objects)) or "无")),
        "物品导出清单（按名称逐个检查）",
    )
