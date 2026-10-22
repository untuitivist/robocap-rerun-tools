"""Find original Sessions for an A-L Markdown problem list and repair verified files."""

from __future__ import annotations

import argparse
import csv
import ctypes
import glob
import html
import json
import math
import os
import re
import shutil
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from . import modelscope_publisher as publisher
from .file_integrity import file_signature, inspect_file
from .inventory_report import render_table, write_csv
from .repair_upload import safe_relative, sha256
from .session_integrity import SKIP_DIRS, is_link, source_files
from .session_layout import is_mocap_directory_name

SESSION_PATH = re.compile(r"(?<![\w/])(\d{8}/[A-Z]+\d+/\d{8}_\d+_session\d+)(?![\w/])")
MANUAL = {
    "G": "时钟/动作配对需人工确认，不能自动交换文件或修改时间轴",
    "H": "量程、单位和传感器饱和需人工确认，不能自动缩放原始数值",
}
RESOLVED = "已解决（远端已验证）"


def read_problem_markdown(path):
    """Only section identifiers and Session/table data are inputs, never prose commands."""
    grouped = {}
    for section in re.split(r"(?m)^## ", path.read_text(encoding="utf-8-sig")):
        heading = re.match(r"([A-L])\. (.+)", section)
        if not heading:
            continue
        group = heading[1]
        for line in section.splitlines():
            for match in SESSION_PATH.finditer(line):
                relative = match[1]
                destination = "EgoMotionActions/" + safe_relative(relative)
                item = grouped.setdefault(
                    destination,
                    {
                        "session_path": destination,
                        "session": relative.rsplit("/", 1)[1],
                        "groups": {},
                    },
                )
                details = item["groups"].setdefault(group, {"title": heading[2]})
                if "---> 已解决" in line:
                    details["previously_resolved"] = True
                if group == "J":
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if len(cells) != 3:
                        raise ValueError(f"J 组需要 Session/Left/Right 三列表格：{line}")
                    sides = [
                        side
                        for side, state in zip(("left", "right"), cells[1:])
                        if state.casefold() != "ok"
                    ]
                    if not sides or any(
                        s not in {"ok", "missing", "corrupt", "unreadable", "empty"}
                        for s in (v.casefold() for v in cells[1:])
                    ):
                        raise ValueError(f"J 组左右状态不明确：{line}")
                    details["sides"] = sides
    if not grouped:
        raise ValueError("没有读到 A–L 问题分组中的 Session 路径，请选择原始问题 Markdown")
    identities = defaultdict(set)
    for item in grouped.values():
        identities[item["session"].casefold()].add(item["session_path"])
    if any(len(paths) != 1 for paths in identities.values()):
        raise ValueError("同一个 Session 文件夹名对应多个远端路径，需先确认清单身份")
    return list(grouped.values())


def automatic_roots():
    """Fixed/removable disks and mapped shares; never guess unmounted UNC servers."""
    if os.name != "nt":
        raise ValueError("非 Windows 系统请通过 --root 指定搜索目录")
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = []
    for number in range(26):
        if mask & (1 << number):
            root = f"{chr(65 + number)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) in {2, 3, 4}:
                roots.append(Path(root))
    return roots


def find_sources(roots, names, output, log):
    found, errors, seen = defaultdict(list), [], set()
    wanted = {name.casefold(): name for name in names}
    excluded = SKIP_DIRS | {"program files", "program files (x86)", "programdata", "$windows.~bt"}
    count = 0
    for specified in roots:
        root = specified.expanduser().absolute()
        if not root.is_dir():
            errors.append({"path": str(root), "error": "目录不存在或不可访问"})
            continue
        log(f"搜索目录：{root}")
        for directory, dirs, _ in os.walk(
            root,
            followlinks=False,
            onerror=lambda exc: errors.append({"path": str(exc.filename), "error": str(exc)}),
        ):
            current = Path(directory)
            key = os.path.normcase(str(current.absolute()))
            if key in seen:
                dirs[:] = []
                continue
            seen.add(key)
            count += 1
            if current.name.casefold() in wanted:
                name = wanted[current.name.casefold()]
                found[name].append(current)
                log(f"找到 {name}：{current}")
                dirs[:] = []
                continue
            keep = []
            for name in dirs:
                path = current / name
                if name.casefold() in excluded or path.absolute().is_relative_to(output):
                    continue
                try:
                    if not is_link(path):
                        keep.append(name)
                except OSError as exc:
                    errors.append({"path": str(path), "error": str(exc)})
            dirs[:] = keep
            if count % 2000 == 0:
                log(f"已搜索 {count} 个目录，找到 {len(found)}/{len(wanted)} 个 Session 名称")
    return dict(found), errors


def manifest_records(manifest):
    records = {}
    for record in manifest["files"]:
        path = safe_relative(record["packaged_as"])
        if path in records:
            raise ValueError(f"远端 manifest 文件路径重复：{path}")
        records[path] = dict(record)
    return records


def export_match(name, entity, suffix):
    return re.search(r"(?:^|[-_])" + entity + re.escape(suffix) + r"$", name, re.IGNORECASE)


def make_targets(request, manifest):
    """Infer missing export paths from sibling exports, preserving the recording prefix."""
    records = manifest_records(manifest)
    mocap = [p for p in records if is_mocap_directory_name(PurePosixPath(p).parts[0])]
    targets, blocked = defaultdict(set), {}

    def entity(group, label, suffix):
        matches = [p for p in mocap if export_match(PurePosixPath(p).name, label, suffix)]
        if not matches:
            siblings = {
                str(PurePosixPath(p).with_suffix(suffix))
                for p in mocap
                if export_match(PurePosixPath(p).stem, label, "")
            }
            matches = sorted(siblings)
        if len(matches) != 1:
            blocked[group] = f"无法唯一确定 {label}{suffix} 的远端路径（候选 {len(matches)} 个）"
        else:
            targets[group].add(matches[0])

    segments = {
        m[1] for p in records if (m := re.search(r"(?:robocap|robowrist)_(segment\d+)_", p))
    }
    for group, detail in request["groups"].items():
        if group in "AB":
            for label in ("Body0", "Tracker0"):
                entity(group, label, ".csv" if group == "A" else ".trc")
        elif group == "C":
            entity(group, "Body0", ".bvh")
        elif group == "D":
            targets[group].update(
                p
                for p in mocap
                if PurePosixPath(p).suffix.lower() in {".csv", ".trc", ".bvh"}
                and records[p].get("packaged_bytes") == 0
            )
            # If the zero-byte files have been replaced, validate all current exports.
            if not targets[group]:
                targets[group].update(
                    p for p in mocap if PurePosixPath(p).suffix.lower() in {".csv", ".trc", ".bvh"}
                )
        elif group == "E":
            entity(group, "Body0", ".trc")
        elif group == "F":
            for label in ("Body0", "Tracker0"):
                entity(group, label, ".csv")
            entity(group, "Body0", ".trc")
        elif group in MANUAL:
            blocked[group] = MANUAL[group]
        elif group == "I":
            targets[group].update(
                p
                for p in records
                if "/" not in p
                and p.startswith("robocap_")
                and PurePosixPath(p).suffix.lower() in {".db", ".mp4"}
            )
        elif group in "JKL":
            sides = detail["sides"] if group == "J" else ["left" if group == "K" else "right"]
            for side in sides:
                directories = {
                    PurePosixPath(p).parts[0]
                    for p in records
                    if re.fullmatch(rf"robowrist_.+_{side}", PurePosixPath(p).parts[0])
                }
                device = manifest.get("device_ids", {}).get(side)
                if not directories and device and re.fullmatch(r"[a-zA-Z0-9]+", str(device)):
                    directories = {f"robowrist_{device}_{side}"}
                if len(directories) != 1 or not segments:
                    blocked[group] = f"无法唯一确定 {side} 腕设备目录或 Segment"
                    continue
                directory = next(iter(directories))
                suffix = f"video_{side}_down.mp4" if group == "J" else f"imu_{side}.db"
                targets[group].update(
                    f"{directory}/robowrist_{segment}_{suffix}" for segment in segments
                )
        if not targets[group] and group not in blocked:
            blocked[group] = "无法从远端 manifest 确定待检查的文件，需人工确认"
    return dict(targets), blocked


def marker_details(path, encoding="utf-8-sig"):
    rows = empty_rows = missing_timestamp = nonincreasing = 0
    previous = None
    started = False
    with path.open(encoding=encoding) as stream:
        for line in stream:
            cells = line.rstrip("\r\n").split("\t")
            if not started:
                if cells[0] == "Frame#":
                    started = True
                    offset = 3 if len(cells) > 2 and "time" in cells[2].lower() else 2
                continue
            if not cells[0].isdigit():
                continue
            rows += 1
            valid = False
            for start in range(offset, len(cells) - 2, 3):
                try:
                    valid |= all(math.isfinite(float(v)) for v in cells[start : start + 3])
                except ValueError:
                    pass
            empty_rows += not valid
            try:
                stamp = float(cells[2]) if offset == 3 else 0
                if not math.isfinite(stamp) or stamp <= 0:
                    raise ValueError("missing")
                nonincreasing += previous is not None and stamp <= previous
                previous = stamp
            except (ValueError, IndexError):
                missing_timestamp += 1
    return {
        "rows": rows,
        "frames_without_any_marker": empty_rows,
        "missing_timestamps": missing_timestamp,
        "nonincreasing_timestamps": nonincreasing,
    }


def timestamp_details(path, encoding="utf-8-sig"):
    header = None
    rows = missing = nonincreasing = 0
    previous = None
    with path.open(encoding=encoding, newline="") as stream:
        for cells in csv.reader(stream):
            cells = [cell.strip() for cell in cells]
            if header is None:
                if "Timestamp" in cells:
                    header = cells.index("Timestamp")
                continue
            if not cells or not cells[0].isdigit():
                continue
            rows += 1
            try:
                stamp = float(cells[header])
                if not math.isfinite(stamp) or stamp <= 0:
                    raise ValueError("missing")
                nonincreasing += previous is not None and stamp <= previous
                previous = stamp
            except (ValueError, IndexError):
                missing += 1
    return {"rows": rows, "missing_timestamps": missing, "nonincreasing_timestamps": nonincreasing}


def validate_candidate(path, groups, ffmpeg, ffprobe, log_path):
    checked = inspect_file(path, ffmpeg=ffmpeg, ffprobe=ffprobe, video_log=log_path)
    # Missing-file / zero-byte issues can be fixed by usable partial trajectories;
    # preserve their warnings rather than calling the whole Session good.
    allowed = (
        {"ok", "partial_data"}
        if groups <= set("ABCDEFI") and path.suffix.lower() in {".csv", ".trc", ".bvh"}
        else {"ok"}
    )
    good = checked["state"] in allowed
    if good and path.suffix.lower() == ".trc" and groups & {"E", "F"}:
        checked["marker_detail"] = extra = marker_details(
            path, checked.get("encoding", "utf-8-sig")
        )
        good = extra["rows"] > 0 and extra["frames_without_any_marker"] == 0
        if "F" in groups:
            good &= extra["missing_timestamps"] == extra["nonincreasing_timestamps"] == 0
    if good and path.suffix.lower() == ".csv" and "F" in groups:
        checked["timestamp_detail"] = extra = timestamp_details(
            path, checked.get("encoding", "utf-8-sig")
        )
        good = (
            extra["rows"] > 0
            and extra["missing_timestamps"] == extra["nonincreasing_timestamps"] == 0
        )
    checked["eligible"] = bool(good)
    return checked


def local_matches(source, paths, relative, record):
    expected = PurePosixPath(relative)
    mocap = is_mocap_directory_name(expected.parts[0])
    requested_source = record.get("source", "")
    matches = []
    for path in paths:
        local = path.relative_to(source)
        if mocap:
            good = path.name.casefold() == expected.name.casefold() and is_mocap_directory_name(
                local.parts[0]
            )
        else:
            # Device ID and Segment identity must match, not just the basename.
            good = local.as_posix().casefold() == relative.casefold()
            if requested_source and requested_source.casefold() == local.as_posix().casefold():
                good |= local.parts[0].casefold() == expected.parts[0].casefold()
        if good:
            matches.append(path)
    return matches


def json_document(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def publish_repair_metadata(api, settings, request, baseline, replacements, resolved_groups, log):
    """Merge manifest and index in one non-force Git transaction at a fresh head."""
    from .metadata_commit import _publish_changes, require_git

    destination = request["session_path"]
    manifest_path = destination + "/manifest.json"
    baseline_records = manifest_records(baseline)

    def build(parent, exists):
        current_path = api.download_file(
            settings.repo_id, "dataset", manifest_path, revision=parent, force=True
        )
        manifest = json.loads(Path(current_path).read_text(encoding="utf-8"))
        if (
            manifest.get("dataset_path") != destination
            or manifest.get("session_id") != request["session"]
        ):
            raise ValueError("重新合并时远端 manifest 的 Session 身份发生变化")
        records = manifest_records(manifest)
        for relative, update in replacements.items():
            current, old = records.get(relative, {}), baseline_records.get(relative, {})
            if current != old and current.get("sha256") != update["sha256"]:
                raise ValueError(f"同一 Session 文件被其他上传修改，停止覆盖：{relative}")
            records[relative] = {**current, **update}
        manifest["files"] = list(records.values())
        history = dict(manifest.get("repair") or {})
        history.update(
            at=datetime.now().astimezone().isoformat(),
            files={
                **history.get("files", {}),
                **{
                    p: {"bytes": r["packaged_bytes"], "sha256": r["sha256"]}
                    for p, r in replacements.items()
                },
            },
            resolved_problem_groups=sorted(resolved_groups),
            pending_problem_groups=sorted(set(request["groups"]) - set(resolved_groups)),
        )
        manifest["repair"] = history
        original = publisher._download_remote_metadata(api, settings.repo_id, parent)
        entries = publisher._read_metadata_document(original, "remote metadata")
        matches = [e for e in entries if e.get("session_path") == destination]
        if len(matches) != 1:
            raise ValueError("远端索引中 Session 缺失或重复，停止修改索引")
        matches[0].update(
            file_count=len(records),
            packaged_bytes=sum(r["packaged_bytes"] for r in records.values()),
            repair=history,
        )
        # Existing quality/error, participant, objects, site and unrelated fields are preserved.
        return {
            manifest_path: json_document(manifest),
            "metadata.jsonl": publisher._metadata_document(entries).encode("utf-8"),
        }

    require_git()
    return _publish_changes(
        build,
        api=api,
        repository=settings.repo_id,
        revision="master",
        settings=settings,
        progress=log,
    )


def upload_replacements(api, settings, request, manifest, selected, resolved_groups, output, log):
    from .metadata_commit import require_git
    from .upload_verification import verify_and_repair

    require_git()
    destination = request["session_path"]
    for info in selected.values():
        if file_signature(info["path"]) != info["signature"]:
            raise ValueError(f"验证后原件发生变化：{info['path']}")
    entries = publisher._read_metadata_document(
        publisher._download_remote_metadata(api, settings.repo_id, "master"), "remote metadata"
    )
    if len([e for e in entries if e.get("session_path") == destination]) != 1:
        raise ValueError("远端索引缺失或重复，未上传文件，需先确认 Session 身份")
    stage = output / "staged" / request["session"]
    inventory, updates = {}, {}
    for relative, info in selected.items():
        source, path = info["source"], info["path"]
        if file_signature(path) != info["signature"]:
            raise ValueError(f"验证后原件发生变化：{path}")
        target = stage / destination / safe_relative(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        digest = sha256(target)
        if digest != info["sha256"] or file_signature(path) != info["signature"]:
            raise ValueError(f"复制期间原件发生变化：{path}")
        size = target.stat().st_size
        inventory[destination + "/" + relative] = {"size": size, "sha256": digest}
        updates[relative] = {
            "source": path.relative_to(source).as_posix(),
            "packaged_as": relative,
            "original_bytes": size,
            "packaged_bytes": size,
            "sha256": digest,
            "kind": "video_raw" if path.suffix.lower() == ".mp4" else "data",
            "compressed_video": False,
        }
    log(f"补传 {request['session']}：{len(updates)} 个已验证原件")
    api.upload_folder(
        settings.repo_id,
        "dataset",
        stage,
        revision="master",
        allow_patterns=[glob.escape(p) for p in inventory],
        use_cache=False,
        sync_remote_repo=False,
        max_workers=4,
        commit_message=f"Repair original files in {request['session']}",
    )
    staged = SimpleNamespace(dataset_root=stage, session_paths=(destination,))
    verify_and_repair(
        staged,
        [],
        inventory,
        api=api,
        repository=settings.repo_id,
        revision="master",
        max_workers=4,
        token=settings.token,
        progress=log,
        check_metadata=False,
    )
    revision = publish_repair_metadata(
        api, settings, request, manifest, updates, resolved_groups, log
    )
    # Verify the manifest and index from the immutable commit just published.
    manifest_path = api.download_file(
        settings.repo_id, "dataset", destination + "/manifest.json", revision=revision, force=True
    )
    saved = manifest_records(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    if any(saved.get(p, {}).get("sha256") != r["sha256"] for p, r in updates.items()):
        raise ValueError("补传后的 manifest SHA-256 读回不一致")
    rows = publisher._read_metadata_document(
        publisher._download_remote_metadata(api, settings.repo_id, revision), "repaired metadata"
    )
    matches = [r for r in rows if r.get("session_path") == destination]
    if len(matches) != 1 or matches[0].get("repair", {}).get("resolved_problem_groups") != sorted(
        resolved_groups
    ):
        raise ValueError("补传后的索引状态读回不一致")
    return revision


def process_session(request, sources, api, settings, output, ffmpeg, ffprobe, apply, log):
    from .upload_verification import _remote_inventory

    destination = request["session_path"]
    base = {"session_path": destination, "session": request["session"]}
    rows = {
        g: {
            **base,
            "group": g,
            "status": "需人工确认",
            "detail": MANUAL.get(g, ""),
            "sources": [str(p) for p in sources],
        }
        for g in request["groups"]
    }
    automatic = set(request["groups"]) - set(MANUAL)
    if not sources:
        for group in set(request["groups"]) & set(MANUAL):
            rows[group]["detail"] += "；搜索范围内亦未找到同名 Session 原始目录"
        for group in automatic:
            rows[group].update(
                status="未找到原件",
                detail="搜索范围内没有同名 Session；未重新验收远端，不代表无法恢复",
            )
        return list(rows.values()), []
    if not automatic:
        return list(rows.values()), []
    local_inventory = {}
    local_errors = []
    for source in sources:
        paths, errors = source_files(source)
        local_inventory[source] = paths
        local_errors.extend(str(p) + ": " + error for p, error in errors)
    remote_path = api.download_file(
        settings.repo_id, "dataset", destination + "/manifest.json", revision="master", force=True
    )
    manifest = json.loads(Path(remote_path).read_text(encoding="utf-8"))
    if (
        manifest.get("dataset_path") != destination
        or manifest.get("session_id") != request["session"]
    ):
        raise ValueError("远端 manifest 与问题清单的 Session 身份不符")
    records = manifest_records(manifest)
    targets, blocked = make_targets(request, manifest)
    remote = _remote_inventory(
        api,
        settings.repo_id,
        "master",
        SimpleNamespace(dataset_root=output, session_paths=(destination,)),
    )
    target_groups = defaultdict(set)
    for group, paths in targets.items():
        for path in paths:
            target_groups[path].add(group)
    files, selected, outcomes = [], {}, {}
    cache = {}
    for number, (relative, groups) in enumerate(sorted(target_groups.items()), 1):
        log(f"  [{number}/{len(target_groups)}] 检查 {relative}")
        row = {
            **base,
            "file": relative,
            "groups": sorted(groups),
            "detail": [],
            "status": "原件仍有问题",
        }
        good_sources = []
        for source, paths in local_inventory.items():
            for path in local_matches(source, paths, relative, records.get(relative, {})):
                key = (str(path), tuple(sorted(groups)))
                if key not in cache:
                    signature = file_signature(path)
                    check = validate_candidate(
                        path, groups, ffmpeg, ffprobe, output / "video_decode.log"
                    )
                    info = {"source": source, "path": path, "signature": signature, "check": check}
                    if check["eligible"] and file_signature(path) == signature:
                        info["sha256"] = sha256(path)
                        if file_signature(path) != signature:
                            check["eligible"] = False
                    cache[key] = info
                info = cache[key]
                row["detail"].append(f"{path}: {json.dumps(info['check'], ensure_ascii=False)}")
                if info["check"]["eligible"] and "sha256" in info:
                    good_sources.append(info)
        actual = remote.get(destination + "/" + relative)
        remote_good = False
        recheck_remote = bool(good_sources) or any(
            request["groups"][g].get("previously_resolved") for g in groups
        )
        if actual and actual[0] > 0 and recheck_remote:
            identical = [v for v in good_sources if (v["signature"][0], v["sha256"]) == actual]
            if identical:
                remote_good = True
                row["detail"].append("远端 SHA-256 与已验证原件一致")
            else:
                downloaded = api.download_file(
                    settings.repo_id,
                    "dataset",
                    destination + "/" + relative,
                    revision="master",
                    force=True,
                )
                checked = validate_candidate(
                    Path(downloaded), groups, ffmpeg, ffprobe, output / "video_decode.log"
                )
                remote_good = checked["eligible"]
                row["detail"].append("远端实际内容：" + json.dumps(checked, ensure_ascii=False))
        if remote_good:
            row["status"] = RESOLVED
        elif len({v["sha256"] for v in good_sources}) > 1:
            row.update(status="需人工确认")
            row["detail"].append("多个有效原件内容不同，未擅自选择")
        elif good_sources:
            selected[relative] = good_sources[0]
            row["status"] = "可修复（预览未上传）"
            row["source"] = str(good_sources[0]["path"])
        elif not row["detail"]:
            row["status"] = "目录内缺原文件"
            row["detail"].append("找到 Session 但没有设备 ID、文件名匹配的原件")
        if not good_sources and actual and actual[0] > 0 and not recheck_remote:
            row["status"] = "无有效原件（远端待复核）"
            row["detail"].append(
                "没有可用原件，本轮不重复下载该远端文件；此结果不能证明远端问题仍在"
            )
        files.append(row)
        outcomes[relative] = row
    ready = {
        g
        for g in automatic
        if g not in blocked
        and targets.get(g)
        and all(outcomes[p]["status"] in {RESOLVED, "可修复（预览未上传）"} for p in targets[g])
    }
    if selected and apply:
        try:
            revision = upload_replacements(
                api, settings, request, manifest, selected, ready, output, log
            )
            for relative in selected:
                outcomes[relative].update(status=RESOLVED, revision=revision)
        except Exception as exc:  # noqa: BLE001 - retain per-file evidence after partial transfer
            error = publisher._redacted_error(exc, settings.token)
            log("补传未完成，需复核远端：" + error)
            for relative in selected:
                outcomes[relative]["status"] = "上传或读回失败"
                outcomes[relative]["detail"].append(error)
    for group in automatic:
        related = [outcomes[p] for p in targets.get(group, ())]
        if group in blocked:
            rows[group].update(status="需人工确认", detail=blocked[group])
        elif related and all(r["status"] == RESOLVED for r in related):
            rows[group].update(status=RESOLVED, detail="本问题涉及的文件均已在远端验证")
        elif related and all(r["status"] in {RESOLVED, "可修复（预览未上传）"} for r in related):
            rows[group].update(
                status="可修复（预览未上传）", detail="有效原件已找到并验证；加 --apply 才上传"
            )
        else:
            states = {r["status"] for r in related}
            state = (
                "上传或读回失败"
                if "上传或读回失败" in states
                else "需人工确认"
                if "需人工确认" in states
                else "无有效原件（远端待复核）"
                if "无有效原件（远端待复核）" in states
                else "目录内缺原文件"
                if "目录内缺原文件" in states
                else "原件仍有问题"
            )
            rows[group].update(
                status=state,
                detail="；".join(
                    r["file"] + "：" + r["status"] for r in related if r["status"] != RESOLVED
                ),
            )
        if local_errors:
            rows[group]["detail"] += "；部分目录读取不完整：" + "；".join(local_errors)
    return list(rows.values()), files


def write_reports(output, requests, found, rows, files, errors, roots, apply):
    write_csv(
        output / "results.csv", rows, ["session_path", "group", "status", "detail", "sources"]
    )
    write_csv(
        output / "files.csv",
        files,
        ["session_path", "groups", "file", "status", "source", "detail", "revision"],
    )
    write_csv(output / "scan_errors.csv", errors, ["path", "error"])
    summary = {
        "updated_at": datetime.now(UTC).astimezone().isoformat(),
        "apply": apply,
        "roots": [str(p) for p in roots],
        "requested_sessions": len(requests),
        "found_sessions": len(found),
        "processed_issue_items": len(rows),
        "statuses": dict(Counter(r["status"] for r in rows)),
        "scan_complete": not errors,
        "scan_errors": len(errors),
    }
    for name, value in (
        ("results.json", rows),
        ("files.json", files),
        ("summary.json", summary),
        ("sources.json", {k: [str(p) for p in v] for k, v in found.items()}),
    ):
        (output / name).write_bytes(json_document(value))
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>问题 Session 搜索与修复</title>
<style>body{font:15px system-ui;margin:24px;color:#183047}table{border-collapse:collapse;width:100%}
td,th{padding:8px;border:1px solid #ccd8df;text-align:left;overflow-wrap:anywhere}.scroll{overflow:auto}
input{padding:10px;width:360px}</style><h1>问题 Session 搜索与修复</h1>"""
    document += "<p>" + html.escape(json.dumps(summary, ensure_ascii=False)) + "</p>"
    document += (
        "<p>状态按原问题分别统计，已解决不代表整个 Session 全部合格。未找到原件、扫描不完整均不等于无法恢复。"
        "原始数据保留，未自动补造帧、缩放 IMU 或交换配对；原有全量质量标签未擅自清除。</p>"
        "<p><a href='results.csv'>问题结果</a> · <a href='files.csv'>文件明细</a> · "
        "<a href='scan_errors.csv'>搜索错误</a> · <a href='run.log'>运行日志</a></p>"
        "<input id='q' placeholder='搜索 Session、状态、问题'><h2>问题处理结果</h2>"
    )
    document += render_table(
        rows,
        {
            "session_path": "远端 Session",
            "group": "问题组",
            "status": "结果",
            "detail": "原因",
            "sources": "本机 / 共享盘原件",
        },
    )
    document += "<h2>文件明细</h2>" + render_table(
        files,
        {
            "session": "Session",
            "groups": "问题组",
            "file": "文件",
            "status": "结果",
            "detail": "实际检查",
        },
    )
    document += "<h2>搜索或处理未完成</h2>" + render_table(
        errors, {"path": "路径", "error": "原因"}
    )
    document += """<script>document.getElementById('q').addEventListener('input',e=>{
const q=e.target.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{
r.hidden=!r.textContent.toLowerCase().includes(q);});});</script></html>"""
    (output / "report.html").write_text(document, encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="Aaron 的问题 Markdown")
    parser.add_argument("--root", type=Path, action="append", default=[])
    parser.add_argument("--auto-roots", action="store_true", help="搜索当前电脑所有本地盘和映射盘")
    parser.add_argument("--apply", action="store_true", help="找到有效原件后补传并校验远端")
    parser.add_argument("--scan-only", action="store_true", help="只搜索原件目录，不访问远端")
    parser.add_argument("--open-report", action="store_true")
    parser.add_argument("--repo-id")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_artifacts/problem_repair_upload")
        / datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S_%f"),
    )
    args = parser.parse_args(argv)
    roots = (automatic_roots() if args.auto_roots or not args.root else []) + args.root
    roots = list(dict.fromkeys(p.expanduser().absolute() for p in roots))
    output = args.output.expanduser().absolute()
    if output.exists() or any(root.is_relative_to(output) for root in roots):
        parser.error("输出目录必须是新目录，且不能包含搜索根目录")
    if args.scan_only and args.apply:
        parser.error("--scan-only 与 --apply 不能同时使用")
    requests = read_problem_markdown(args.report)
    output.mkdir(parents=True)
    rows, files = [], []
    with (output / "run.log").open("w", encoding="utf-8") as logfile:

        def log(message):
            text = str(message)
            print(text, flush=True)
            logfile.write(text + "\n")
            logfile.flush()

        log(f"问题 Session {len(requests)} 个；自动补传={args.apply}；结果目录：{output}")
        found, errors = find_sources(roots, {r["session"] for r in requests}, output, log)
        write_reports(output, requests, found, rows, files, errors, roots, args.apply)
        if args.scan_only:
            rows = [
                {
                    "session_path": r["session_path"],
                    "group": g,
                    "status": "仅搜索未检查" if found.get(r["session"]) else "未找到原件",
                    "detail": "scan-only 模式未访问远端，未修改数据",
                    "sources": [str(p) for p in found.get(r["session"], [])],
                }
                for r in requests
                for g in r["groups"]
            ]
        else:
            from dataclasses import replace

            from .cli import resolve_ffprobe
            from .frame_comparison import _resolve_ffmpeg

            settings = publisher.load_modelscope_settings()
            if args.repo_id:
                settings = replace(settings, repo_id=publisher.validate_repo_id(args.repo_id))
            if not settings.repo_id or not settings.token:
                raise ValueError("请先在工具 .env 配置 MODELSCOPE_REPO_ID 和 MODELSCOPE_API_TOKEN")
            api = publisher._hub_api(settings)
            ffmpeg = _resolve_ffmpeg(None)
            ffprobe = resolve_ffprobe("ffprobe", ffmpeg)
            for number, request in enumerate(requests, 1):
                log(f"[{number}/{len(requests)}] {request['session_path']}")
                directory = output / "sessions" / request["session"]
                directory.mkdir(parents=True)
                try:
                    result_rows, result_files = process_session(
                        request,
                        found.get(request["session"], []),
                        api,
                        settings,
                        directory,
                        ffmpeg,
                        ffprobe,
                        args.apply,
                        log,
                    )
                    rows.extend(result_rows)
                    files.extend(result_files)
                except Exception as exc:  # noqa: BLE001 - isolate each Session and keep the durable report
                    detail = publisher._redacted_error(exc, settings.token)
                    log("本段未完成：" + detail)
                    errors.append({"path": request["session_path"], "error": detail})
                    rows.extend(
                        {
                            "session_path": request["session_path"],
                            "group": g,
                            "status": "处理未完成",
                            "detail": detail,
                            "sources": [str(p) for p in found.get(request["session"], [])],
                        }
                        for g in request["groups"]
                    )
                with (output / "progress.jsonl").open("a", encoding="utf-8") as checkpoint:
                    checkpoint.write(
                        json.dumps(
                            {
                                "session": request["session"],
                                "results": [
                                    r
                                    for r in rows
                                    if r.get("session_path") == request["session_path"]
                                ],
                                "files": [
                                    r
                                    for r in files
                                    if r.get("session_path") == request["session_path"]
                                ],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                if number % 20 == 0:
                    write_reports(output, requests, found, rows, files, errors, roots, args.apply)
        summary = write_reports(output, requests, found, rows, files, errors, roots, args.apply)
        log("处理结束：" + json.dumps(summary, ensure_ascii=False))
    if args.open_report:
        webbrowser.open((output / "report.html").as_uri())
    return int(bool(errors) or any(r["status"] != RESOLVED for r in rows))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"未完成：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
