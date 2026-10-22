"""Batch Session inspection for the double-click BAT handoff."""

from __future__ import annotations

import argparse
import html
import json
import webbrowser
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .cli import inspect_session_report
from .dataset_statistics import load_report_payload
from .inspection_issues import ISSUE_LABELS, classify_inspection
from .inventory_report import render_table, write_csv
from .session_integrity import discover_sessions


def write_reports(output, roots, sessions, issues, errors, quality_issues=()):
    write_csv(
        output / "sessions.csv",
        sessions,
        ["session", "status", "quality_status", "quality_codes", "files", "errors", "warnings",
         "report", "source"],
    )
    write_csv(
        output / "issues.csv",
        issues,
        [
            "session",
            "source",
            "path",
            "category",
            "requirement",
            "severity",
            "state",
            "status",
            "detail",
        ],
    )
    write_csv(output / "scan_errors.csv", errors, ["path", "error"])
    write_csv(output / "quality_issues.csv", quality_issues,
              ["session", "source", "code", "group", "label", "severity", "path", "count", "detail"])
    type_counts = [
        {"code": code, "group": group, "label": label,
         "sessions": len({row["source"] for row in quality_issues if row["code"] == code})}
        for code, (group, label) in ISSUE_LABELS.items()
        if any(row["code"] == code for row in quality_issues)
    ]
    summary = {
        "roots": [str(p.resolve()) for p in roots],
        "sessions": len(sessions),
        "states": dict(Counter(row["status"] for row in sessions)),
        "quality_states": dict(Counter(row.get("quality_status", "unchecked") for row in sessions)),
        "issue_type_sessions": {row["code"]: row["sessions"] for row in type_counts},
        "scan_errors": len(errors),
        "scan_complete": not errors,
        "all_passed": bool(sessions)
        and not errors
        and all(row["status"] == row.get("quality_status") == "passed" for row in sessions),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Session 完整检查</title>
<style>body{font:15px system-ui;margin:28px;color:#183047;background:#f5f8fa}table{width:100%;
border-collapse:collapse;background:white}td,th{border:1px solid #ccd8df;padding:9px;text-align:left}
.scroll{overflow:auto}input{padding:9px;width:340px}li{margin:7px 0}a{color:#00648a}</style>
<h1>Session 完整检查</h1><p>检查目录结构、文件实际内容及原有帧数和时间戳。文件内容通过不代表动作质量和同步均通过，需打开各 Session 的完整报告确认。</p>"""
    document += (
        f"<p>录制目录：{len(sessions)}；文件问题：{len(issues)}；扫描错误：{len(errors)}。</p>"
    )
    if errors:
        document += "<p><strong>扫描未完整完成，请查看扫描错误。</strong></p>"
    if not sessions:
        document += "<p><strong>未找到 Session，请确认原始数据目录。</strong></p>"
    document += "<p><a href='sessions.csv'>Session 汇总</a> · <a href='issues.csv'>文件检查明细</a> · <a href='quality_issues.csv'>综合问题明细</a> · <a href='run.log'>运行日志</a></p>"
    document += "<h2>问题分类</h2><p>同一 Session 可属于多类，每类按源目录去重，分类数量不能相加。兼容格式仅作说明。</p>"
    document += render_table(type_counts, {"group": "大类", "label": "问题", "sessions": "Session 数"})
    document += "<h2>打开完整报告</h2><ul>"
    labels = {"passed": "文件通过", "failed": "文件未通过", "incomplete": "仍需确认"}
    quality_labels = {"passed": "正常", "error": "确认异常", "review": "待确认", "unchecked": "检查未完成"}
    for row in sessions:
        document += (
            f"<li><a href='{html.escape(row['report'], quote=True)}'>"
            f"{html.escape(row['session'])}</a> — {labels.get(row['status'], row['status'])}"
            f"；综合质量：{quality_labels.get(row.get('quality_status'), '检查未完成')}"
            f"，错误 {row['errors']}，待确认 {row['warnings']}</li>"
        )
    document += "</ul><h2>综合问题</h2><input id='query' placeholder='搜索 Session、文件、问题'>"
    document += render_table(quality_issues, {"session": "Session", "group": "大类", "label": "问题",
                                             "severity": "状态", "path": "文件", "detail": "依据"})
    document += "<h2>文件问题</h2>"
    document += render_table(
        issues, {"session": "Session", "path": "文件 / 要求", "status": "结果", "detail": "说明"}
    )
    if errors:
        document += "<h2>扫描错误</h2>" + render_table(errors, {"path": "路径", "error": "错误"})
    document += """<script>document.getElementById('query').addEventListener('input',e=>{
 const q=e.target.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{
 r.hidden=!r.textContent.toLowerCase().includes(q);});});</script></html>"""
    (output / "report.html").write_text(document, encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_artifacts/session_inspection")
        / datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S_%f"),
    )
    parser.add_argument("--layout", choices=("raw", "published"), default="raw")
    parser.add_argument(
        "--mocap-requirements", type=Path, default=None,
        help="Action-specific required objects/additional bodies CSV (default: BAT folder).",
    )
    parser.add_argument("--mocap-ratio", type=int, choices=(4, 8), default=8)
    parser.add_argument("--open-report", action="store_true")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.exists() or any(p.expanduser().resolve().is_relative_to(output) for p in args.root):
        parser.error("Output must be a new directory and must not contain a source root")
    output.mkdir(parents=True)
    with (output / "run.log").open("w", encoding="utf-8") as logfile:

        def log(message):
            print(message, flush=True)
            logfile.write(str(message) + "\n")
            logfile.flush()

        log("Session structure/content/timestamp inspection; source data is read-only")
        log("Report directory: " + str(output))
        sources, errors = discover_sessions(args.root, output, log)
        if not sources:
            errors.append({"path": "; ".join(map(str, args.root)), "error": "no_sessions_found"})
        sessions, issues, quality_issues = [], [], []
        for index, source in enumerate(sources, 1):
            log(f"[{index}/{len(sources)}] SESSION {source}")
            directory = output / "sessions" / f"{index:05d}"
            directory.mkdir(parents=True)
            try:
                report, integrity = inspect_session_report(
                    source,
                    None,
                    directory,
                    mocap_ratio=args.mocap_ratio,
                    layout=args.layout,
                    progress=log,
                    video_log=directory / "video_decode.log",
                    requirements_path=args.mocap_requirements,
                )
                quality = classify_inspection(load_report_payload(report))
                quality_issues.extend(
                    {"session": source.name, "source": str(source), **row}
                    for row in quality["issues"]
                )
                sessions.append(
                    {
                        "session": source.name,
                        "source": str(source),
                        "status": integrity["status"],
                        "quality_status": quality["status"],
                        "quality_codes": "|".join(dict.fromkeys(row["code"] for row in quality["issues"])),
                        "files": integrity["fileCount"],
                        "errors": integrity["errors"],
                        "warnings": integrity["warnings"],
                        "report": report.relative_to(output).as_posix(),
                    }
                )
                issues.extend(
                    {"session": source.name, "source": str(source), **row}
                    for row in integrity["checks"]
                    if row["severity"] != "pass"
                )
            except Exception as exc:  # noqa: BLE001 - complete other Sessions and expose this failure
                log(f"FAILED: {source}: {exc}")
                errors.append({"path": str(source), "error": f"{type(exc).__name__}: {exc}"})
            write_reports(output, args.root, sessions, issues, errors, quality_issues)
        summary = write_reports(output, args.root, sessions, issues, errors, quality_issues)
        log(json.dumps(summary, ensure_ascii=False))
        log("FINISHED: " + str(output / "report.html"))
        if args.open_report:
            try:
                webbrowser.open((output / "report.html").as_uri())
            except (OSError, webbrowser.Error) as exc:
                log(f"Open report.html manually: {exc}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
