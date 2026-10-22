"""Locate known problematic NOKOV takes and copy verified re-exports, never by time."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import shutil
import sys
import uuid
import webbrowser
from collections import defaultdict
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path, PureWindowsPath

from filelock import FileLock

from .file_integrity import file_signature, inspect_file
from .inventory_report import render_table, write_csv
from .obj_inventory import name_category
from .problem_repair import (
    automatic_roots,
    marker_details,
    read_bundled_problems,
    timestamp_details,
)
from .repair_upload import sha256
from .session_integrity import SKIP_DIRS, is_link

SESSION = re.compile(r"^\d{8}_\d+_session\d+$", re.IGNORECASE)
EXPORTS = {".csv", ".trc", ".bvh"}
ARTIFACTS = Path(__file__).resolve().parents[2] / "_artifacts" / "mocap_reexport"


def read_targets():
    doc = json.loads(
        package_files("robocap_rerun_tools")
        .joinpath("data/mocap_reexport_sessions.json")
        .read_text(encoding="utf-8")
    )
    known = {
        r["session_path"]: r for r in read_bundled_problems() if set(r["groups"]) & set("ABCDEFG")
    }
    if doc.get("schema_version") != 1:
        raise ValueError("重导出清单版本不支持")
    seen = set()
    for item in doc["sessions"]:
        path, name = item["session_path"], item["recording"]
        if path in seen or path not in known or known[path]["session"] != item["session"]:
            raise ValueError("重导出清单的 Session 身份重复或不符")
        seen.add(path)
        if not name.lower().startswith("mocap-") or re.search(r"[\\/\x00-\x1f]", name):
            raise ValueError("录制名称无效")
        if item["groups"] != {
            g: d["title"] for g, d in known[path]["groups"].items() if g in "ABCDEFG"
        }:
            raise ValueError("问题组与内置清单不符")
        if any(Path(n).name != n or "\\" in n for n in item["expected_exports"]):
            raise ValueError("导出文件名无效")
    if seen != set(known):
        raise ValueError("重导出清单未完整覆盖动捕问题 Session")
    return doc["sessions"]


def inventory_directories(path):
    """WizTree is a Windows-path snapshot, independent of the host's mounted disks."""
    directories, batch_files = set(), defaultdict(set)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        for row in reader:
            if not row:
                continue
            value = row[0]
            if not re.match(r"^(?:[a-z]:\\|\\\\)", value, re.IGNORECASE):
                continue
            p = PureWindowsPath(value)
            if value.endswith("\\"):
                directories.add(p)
            else:
                batch_files[p.parent].add(p.name.casefold())
    if not directories:
        raise ValueError("WizTree 清单内没有可识别的 Windows 目录")
    return directories, batch_files, []


def scan_directories(roots, log=print):
    directories, batch_files, errors, seen = set(), defaultdict(set), [], set()
    excluded = SKIP_DIRS | {
        "program files",
        "program files (x86)",
        "programdata",
        ".cache",
        "_mocap_reexport_backups",
    }
    wanted = {r["recording"].casefold() for r in read_targets()}
    count = 0
    for root in roots:
        root = root.expanduser().absolute()
        if not root.is_dir() or is_link(root):
            errors.append({"path": str(root), "error": "目录不可访问或为链接"})
            continue
        log(f"搜索：{root}")
        for folder, children, filenames in os.walk(
            root,
            followlinks=False,
            onerror=lambda exc: errors.append({"path": str(exc.filename), "error": str(exc)}),
        ):
            current = Path(folder)
            key = os.path.normcase(str(current))
            if key in seen:
                children[:] = []
                continue
            seen.add(key)
            count += 1
            directories.add(current)
            batch_files[current].update(n.casefold() for n in filenames)
            # Only top-level capture identity is needed. Avoid huge export subtrees,
            # including accidentally repeated same-name mocap directories.
            if current.name.casefold() in wanted:
                children[:] = []
            else:
                keep = []
                for name in children:
                    child = current / name
                    if name.casefold() in excluded:
                        continue
                    try:
                        if is_link(child):
                            errors.append({"path": str(child), "error": "链接目录未跟随"})
                        else:
                            keep.append(name)
                    except OSError as exc:
                        errors.append({"path": str(child), "error": str(exc)})
                children[:] = keep
            if count % 5000 == 0:
                log(f"已扫描 {count} 个目录")
    return directories, batch_files, errors


def inside_session(path):
    return any(SESSION.fullmatch(p.name) for p in (path, *path.parents))


def make_plan(targets, directories, batch_files):
    sources, sessions, captures = defaultdict(list), defaultdict(list), defaultdict(list)
    parents_with_segments = {
        p.parent for p in directories if re.fullmatch(r"segment\d+", p.name, re.IGNORECASE)
    }
    wanted = {r["recording"].casefold() for r in targets}
    for path in sorted(directories, key=str):
        if any(
            p.name.casefold() in {".cache", "_artifacts", "_mocap_reexport_backups"}
            for p in (path, *path.parents)
        ):
            continue
        if SESSION.fullmatch(path.name):
            # Exclude packaged dataset caches: a raw Session has capture filenames
            # or a named mocap directory, rather than only a flattened mocap folder.
            names = batch_files.get(path, set())
            raw = "manifest.json" not in names and (
                path in parents_with_segments
                or any(n.startswith(("robocap_", "robowrist_", "audio_")) for n in names)
            )
            sessions[path.name.casefold()].append((path, raw))
        if path.name.casefold() not in wanted:
            continue
        if any(p.name.casefold() == path.name.casefold() for p in path.parents):
            continue
        if inside_session(path):
            if SESSION.fullmatch(path.parent.name):
                captures[str(path.parent).casefold()].append(path)
            continue
        # Require the NOKOV tree or the matching original .cap, never an arbitrary
        # exported copy found under an unrelated staging/output directory.
        parent_files = batch_files.get(path.parent, set())
        cap = path.name.casefold() + ".cap"
        if any(p.name.casefold() == "nokov" for p in path.parents) or cap in parent_files:
            sources[path.name.casefold()].append(path)
    # A failed/missing export can leave only the original .cap. It still identifies
    # the batch to open in NOKOV, but must not be treated as a copyable export.
    by_cap = {r["recording"].casefold() + ".cap": r["recording"] for r in targets}
    for batch, names in batch_files.items():
        if inside_session(batch) or any(
            p.name.casefold() in {".cache", "_artifacts", "_mocap_reexport_backups"}
            for p in (batch, *batch.parents)
        ):
            continue
        for name in names & by_cap.keys():
            recording = by_cap[name]
            if not any(p.parent == batch for p in sources[recording.casefold()]):
                sources[recording.casefold()].append(batch / recording)
    rows = []
    for item in targets:
        found = sources[item["recording"].casefold()]
        destinations = [
            p
            for p, raw in sessions[item["session"].casefold()]
            if raw
            or any(
                c.name.casefold() == item["recording"].casefold()
                for c in captures[str(p).casefold()]
            )
        ]
        state = (
            "未找到原始导出目录"
            if not found
            else "源目录重复，需确认"
            if len(found) > 1
            else "找到原始 CAP，尚无导出目录"
            if found[0] not in directories
            else "未找到本地原始 Session"
            if not destinations
            else "Session 多份副本，需确认"
            if len(destinations) > 1
            else "匹配唯一"
        )
        rows.append(
            {
                **item,
                "sources": list(map(str, found)),
                "batches": sorted({str(p.parent) for p in found}),
                "destinations": list(map(str, destinations)),
                "state": state,
                "manual_review": "G" in item["groups"],
            }
        )
    return rows


def write_report(output, rows, errors, *, inventory=None):
    batches = defaultdict(list)
    for row in rows:
        for batch in row["batches"]:
            batches[batch].append(row)
    text = "\n".join(sorted(batches))
    (output / "batch_paths.txt").write_text(text + ("\n" if text else ""), encoding="utf-8")
    fields = ["session", "recording", "state", "batches", "sources", "destinations", "groups"]
    flat = [
        {**r, "groups": "; ".join(f"{g}: {title}" for g, title in r["groups"].items())}
        for r in rows
    ]
    write_csv(output / "sessions.csv", flat, fields)
    write_csv(output / "scan_errors.csv", errors, ["path", "error"])
    (output / "plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "inventory": str(inventory) if inventory else None,
                "rows": rows,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    batch_rows = [
        {
            "batch": p,
            "count": len({r["session"] for r in rs}),
            "recordings": [r["recording"] for r in rs],
        }
        for p, rs in sorted(batches.items())
    ]
    buttons = "".join(
        f"<p><button data-path='{html.escape(p, quote=True)}'>复制路径</button> "
        f"{html.escape(p)}</p>"
        for p in sorted(batches)
    )
    warning = (
        "这是 WizTree 目录快照，只证明清单中存在这些路径；未检查这些盘上的实际文件内容。"
        if inventory
        else "搜索失败或未开放的共享仍可能有原件，详见 scan_errors.csv。"
    )
    page = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<title>动捕问题批次重导出清单</title><style>
body{font:15px system-ui;margin:28px;color:#172334}table{border-collapse:collapse}
td,th{border:1px solid #ccd4df;padding:7px;vertical-align:top;white-space:pre-wrap}
.scroll{overflow:auto}button{padding:8px;cursor:pointer}textarea{width:98%;height:140px}
</style><h1>需要重新导出的 NOKOV 批次</h1>"""
    page += (
        f"<p>仅包含之前动捕问题的 {len(rows)} 个 Session；找到 {len(batches)} 个批次，"
        f"{sum(bool(r['sources']) for r in rows)} 个 Session 有源目录，"
        f"{sum(r['state'] == '匹配唯一' for r in rows)} 个可唯一配对。</p>"
        f"<p>{warning} G 类时钟/动作配对问题，重导出后仍需人工复核。</p>"
        "<p>在下列批次中重新导出列出的录制，完成后双击 02_copy_reexports.bat。"
        "本工具只复制，不上传，也不会把问题自动标成已解决。</p>"
        "<button id='all'>复制全部批次路径</button><span id='status'></span>"
        f"<textarea id='paths' readonly>{html.escape(text)}</textarea>" + buttons
    )
    page += render_table(
        batch_rows,
        {"batch": "批次大目录", "count": "问题 Session 数", "recordings": "本批次需重新导出的录制"},
    )
    page += "<h2>逐 Session 对照（含找不到、重复和需人工判断）</h2>"
    page += render_table(
        flat,
        dict(
            zip(
                fields,
                [
                    "Session",
                    "录制名称",
                    "匹配状态",
                    "批次大目录",
                    "源目录",
                    "目标 Session",
                    "原问题",
                ],
            )
        ),
    )
    page += """<script>
async function copy(s){try{if(navigator.clipboard){await navigator.clipboard.writeText(s);}
else{throw Error('fallback');}document.querySelector('#status').textContent=' 已复制';}
catch(e){const t=document.querySelector('#paths');t.value=s;t.focus();t.select();
document.querySelector('#status').textContent=document.execCommand('copy')?' 已复制':' 请按 Ctrl+C';}}
document.querySelector('#all').onclick=()=>copy(document.querySelector('#paths').defaultValue);
document.querySelectorAll('[data-path]').forEach(b=>b.onclick=()=>copy(b.dataset.path));
</script></html>"""
    (output / "report.html").write_text(page, encoding="utf-8")
    return len(batches)


def export_files(source, recording):
    """Flatten only repeated same-name wrappers; conflicting duplicates are blocked."""
    found = {}
    if is_link(source) or not source.is_dir():
        raise ValueError("源目录不可访问或为链接")
    for folder, dirs, names in os.walk(
        source, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)
    ):
        current = Path(folder)
        for name in [*dirs, *names]:
            if is_link(current / name):
                raise ValueError(f"源内存在链接，不复制：{current / name}")
        for name in names:
            path = current / name
            parts = list(path.relative_to(source).parts)
            while len(parts) > 1 and parts[0].casefold() == recording.casefold():
                parts.pop(0)
            relative = Path(*parts)
            if relative in found and sha256(found[relative]) != sha256(path):
                raise ValueError(f"同名嵌套导出内容冲突：{relative}")
            found.setdefault(relative, path)
    return found


def validate_exports(source, row):
    files = export_files(source, row["recording"])
    motion = {p.name.casefold(): p for p in files if p.suffix.lower() in EXPORTS}
    if len(motion) != sum(p.suffix.lower() in EXPORTS for p in files):
        raise ValueError("多个子目录下存在同名动捕导出，需人工确认")
    expected = set(row["expected_exports"])
    expected.update(
        row["recording"] + "-" + label + ext
        for label, exts in (("Body0", (".csv", ".trc", ".bvh")), ("Tracker0", (".csv", ".trc")))
        for ext in exts
    )
    # Any extra body/prop present must also have its companion formats.
    prefix = row["recording"].casefold() + "-"
    for relative in files:
        if relative.suffix.lower() not in EXPORTS:
            continue
        if not relative.stem.casefold().startswith(prefix):
            raise ValueError(f"存在其他录制的导出，不能混合：{relative}")
        label = relative.stem[len(prefix) :]
        if name_category(label) == "unnamed" or (
            name_category(label) == "body" and not re.fullmatch(r"Body\d+", label, re.IGNORECASE)
        ):
            continue  # Auxiliary skeleton segments do not require complete-body formats.
        exts = (
            (".csv", ".trc", ".bvh")
            if re.fullmatch(r"Body\d+", label, re.IGNORECASE)
            else (".csv", ".trc")
        )
        expected.update(relative.stem + ext for ext in exts)
    missing = sorted(n for n in expected if n.casefold() not in motion)
    if missing:
        raise ValueError("缺少导出：" + "; ".join(missing))
    for relative, path in files.items():
        if path.stat().st_size == 0:
            raise ValueError(f"存在零字节文件：{relative}")
        if relative.suffix.lower() in EXPORTS:
            check = inspect_file(path)
            if check["state"] != "ok":
                raise ValueError(f"{relative}: {check['state']} {check.get('detail', '')}")
            if relative.suffix.lower() in {".csv", ".trc"}:
                inspect = marker_details if relative.suffix.lower() == ".trc" else timestamp_details
                stamps = inspect(path, check.get("encoding", "utf-8-sig"))
                if (
                    not stamps["rows"]
                    or stamps["missing_timestamps"]
                    or stamps["nonincreasing_timestamps"]
                ):
                    raise ValueError(f"{relative}: 时间戳缺失、为零或不递增")
    return files


def copy_one(row, output):
    if row["state"] != "匹配唯一":
        raise ValueError(row["state"])
    source, target = Path(row["sources"][0]), Path(row["destinations"][0])
    recording = row["recording"]
    if target.name != row["session"] or not target.is_dir() or is_link(target):
        raise ValueError("目标 Session 身份不符、不可访问或为链接")
    if source.resolve().is_relative_to(target.resolve()) or target.resolve().is_relative_to(
        source.resolve()
    ):
        raise ValueError("源与目标相互包含")
    # Files removed in a re-export must not silently erase an existing body/prop.
    old = []
    extra_expected = set(row["expected_exports"])
    for child in target.iterdir():
        if not child.name.casefold().startswith("mocap"):
            continue
        if is_link(child) or not child.is_dir():
            raise ValueError(f"目标动捕路径不是普通目录：{child}")
        if child.name.casefold() not in {recording.casefold(), "mocap"}:
            raise ValueError(f"目标已有其他录制，需人工确认：{child}")
        old.append(child)
        extra_expected.update(
            p.name for p in export_files(child, recording) if p.suffix.lower() in EXPORTS
        )
    old_signatures = {
        str(path): {str(p): file_signature(p) for p in export_files(path, recording).values()}
        for path in old
    }
    files = validate_exports(source, {**row, "expected_exports": sorted(extra_expected)})
    token = uuid.uuid4().hex
    # Stage and back up alongside the Session, on its volume. Final replacement
    # uses atomic renames rather than cross-volume move/copy/delete operations.
    backup_root = target.parent / "_artifacts" / "mocap_reexport_backups"
    work = backup_root / row["session"] / token
    for candidate in (work, *work.parents):
        if candidate == target.parent:
            break
        if candidate.exists() and is_link(candidate):
            raise ValueError("备份路径包含链接，未复制")
    stage = work / "new" / recording
    stage.mkdir(parents=True)
    signatures = {relative: file_signature(p) for relative, p in files.items()}
    hashes = {}
    for relative, path in files.items():
        dest = stage / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        hashes[str(relative)] = sha256(path)
        shutil.copy2(path, dest)
        if sha256(dest) != hashes[str(relative)] or file_signature(path) != signatures[relative]:
            raise ValueError(f"复制校验失败或源文件仍在变化：{path}；暂存件保留在 {work}")
    current = export_files(source, recording)
    if set(current) != set(files) or any(
        file_signature(current[p]) != signatures[p] for p in current
    ):
        raise ValueError("导出目录仍在变化，请结束导出后重试；暂存件保留")
    destination = target / recording
    journal = {
        "source": str(source),
        "target": str(target),
        "destination": str(destination),
        "hashes": hashes,
        "backups": [],
        "stage": str(stage),
        "state": "prepared",
    }

    def save():
        (work / "journal.json").write_text(
            json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()
    try:
        for path in old:
            now = {str(p): file_signature(p) for p in export_files(path, recording).values()}
            if now != old_signatures[str(path)]:
                raise ValueError("目标动捕目录仍在变化，未替换")
        for path in old:
            backup = work / "old" / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            # Persist the intended move before mutating; backups are never deleted.
            journal["backups"].append({"original": str(path), "backup": str(backup)})
            save()
            path.rename(backup)
        if destination.exists():
            raise ValueError("目标路径被其他进程占用，未覆盖")
        stage.rename(destination)
        for relative, digest in hashes.items():
            if sha256(destination / relative) != digest:
                raise ValueError(f"落盘校验失败：{relative}；新旧文件均保留，请检查 journal.json")
        journal["state"] = "copied_verified"
        save()
    except Exception:
        journal["state"] = "failed"
        for move in reversed(journal["backups"]):
            original, backup = Path(move["original"]), Path(move["backup"])
            if backup.exists() and not original.exists():
                backup.rename(original)
        save()
        raise
    return {
        "state": "已复制并校验",
        "files": len(hashes),
        "journal": str(work / "journal.json"),
        "note": "G 类时钟配对仍需人工复核" if row["manual_review"] else "请重新运行数据检查后上传",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("report", "copy"))
    parser.add_argument("--root", type=Path, action="append")
    parser.add_argument("--inventory", type=Path, help="维护者：离线读取 WizTree CSV")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--open-report", action="store_true")
    args = parser.parse_args(argv)
    if args.inventory and args.mode == "copy":
        parser.error("WizTree 只用于离线报告，复制必须重新扫描实际磁盘")
    output = (
        args.output
        or ARTIFACTS / (datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    ).absolute()
    output.mkdir(parents=True, exist_ok=False)
    with (output / "run.log").open("a", encoding="utf-8", buffering=1) as stream:

        def log(message):
            print(message, flush=True)
            stream.write(str(message) + "\n")

        try:
            targets = read_targets()
            errors = []
            if args.inventory:
                dirs, names, scan_errors = inventory_directories(args.inventory)
            else:
                roots = args.root or automatic_roots()
                if not args.root:
                    from .share_discovery import discover_shares

                    shares, discoveries = discover_shares(log)
                    errors = [r for r in discoveries if r.get("error")]
                    roots = list(dict.fromkeys([*roots, *shares]))
                dirs, names, scan_errors = scan_directories(roots, log)
            errors.extend(scan_errors)
            rows = make_plan(targets, dirs, names)
            count = write_report(output, rows, errors, inventory=args.inventory)
            log(f"找到 {count} 个问题批次；报告：{output / 'report.html'}")
            if args.mode == "copy":
                ARTIFACTS.mkdir(parents=True, exist_ok=True)
                results = []
                with FileLock(ARTIFACTS / "copy.lock", timeout=0):
                    for row in rows:
                        log(f"处理 {row['session']}：{row['state']}")
                        try:
                            outcome = copy_one(row, output)
                        except (OSError, ValueError) as exc:
                            outcome = {"state": "未复制", "note": str(exc)}
                        results.append({"session": row["session"], **outcome})
                        (output / "copy_results.json").write_text(
                            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        log(outcome)
                write_csv(
                    output / "copy_results.csv",
                    results,
                    ["session", "state", "files", "note", "journal"],
                )
                page = output / "report.html"
                page.write_text(
                    page.read_text(encoding="utf-8").replace(
                        "<script>",
                        "<h2>本次复制结果</h2>"
                        + render_table(
                            results,
                            {
                                "session": "Session",
                                "state": "复制状态",
                                "files": "文件数",
                                "note": "说明",
                                "journal": "备份与校验记录",
                            },
                        )
                        + "<script>",
                        1,
                    ),
                    encoding="utf-8",
                )
                log(
                    f"复制成功 {sum(r['state'] == '已复制并校验' for r in results)}/{len(rows)}；"
                    "详情见 copy_results.csv；原件、旧版本保留，未上传远端。"
                )
            if args.open_report:
                webbrowser.open((output / "report.html").as_uri())
            return int(
                bool(errors)
                or any(r["state"] != "匹配唯一" for r in rows)
                or (args.mode == "copy" and any(r["state"] == "未复制" for r in results))
            )
        except Exception as exc:  # noqa: BLE001 - always preserve a staff-readable run log
            log(f"失败：{type(exc).__name__}: {exc}")
            return 2


if __name__ == "__main__":
    sys.exit(main())
