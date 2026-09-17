from __future__ import annotations

import csv
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robocap_rerun_tools import statistics_report
from robocap_rerun_tools.dataset_statistics import SegmentStatistic, SessionStatistic


def _write_inspection(path: Path, **overrides: object) -> None:
    payload = {
        "ratio": 8,
        "referenceFrames": 9,
        "mocapFrames": 80,
        "thirdFrames": 10,
        "source": "测试",
        **overrides,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<script>const report="
        + json.dumps(payload, ensure_ascii=False)
        + "; const eventTypes=[];</script>",
        encoding="utf-8",
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_complete_report_preserves_full_relative_paths_and_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "morning" / "20260803_032401_session29"
    second = tmp_path / "afternoon" / "20260803_032401_session29"
    for session in (first, second):
        session.mkdir(parents=True)
        (session / "robocap_segment1_video_left.mp4").write_bytes(b"video")
    mocap = first / "mocap-SM001-S07-王洋-05p2"
    mocap.mkdir()
    (mocap / "take.trc").write_text("trc", encoding="utf-8")
    first_report = (
        first / "_artifacts" / "segment1" / "inspection" / "timestamp_anomaly_detail_table.html"
    )
    second_report = (
        second / "_artifacts" / "segment1" / "inspection" / "timestamp_anomaly_detail_table.html"
    )
    _write_inspection(first_report)
    second_report.parent.mkdir(parents=True)
    second_report.write_text("broken", encoding="utf-8")

    def fake_summary(root: Path, session: Path, _ffprobe: str) -> SessionStatistic:
        report = (
            session
            / "_artifacts"
            / "segment1"
            / "inspection"
            / "timestamp_anomaly_detail_table.html"
        )
        clean = session == first
        return SessionStatistic(
            primitive_id="SM001" if clean else "UNASSIGNED",
            session_dir=session,
            unchecked_duration_s=0.0 if clean else 2.5,
            frame_difference_duration_s=0.0,
            clean_duration_s=2.5 if clean else 0.0,
            segments=(
                SegmentStatistic(
                    segment="segment1",
                    video_path=session / "robocap_segment1_video_left.mp4",
                    report_path=report,
                    duration_s=2.5,
                    status="clean" if clean else "unchecked",
                    detail="" if clean else "inspection report is unreadable",
                ),
            ),
            errors=() if clean else ("segment1: inspection report is unreadable",),
        )

    monkeypatch.setattr(statistics_report, "summarize_session", fake_summary)
    progress: list[tuple[int, int, str]] = []
    result = statistics_report.generate_statistics_report(
        tmp_path,
        [second, first],
        "ffprobe",
        progress=lambda current, total, path: progress.append((current, total, path)),
        generated_at=datetime(2026, 8, 27, 12, tzinfo=timezone(timedelta(hours=8))),
    )

    sessions = _read_csv(result.sessions_csv)
    assert [row["session_relative_path"] for row in sessions] == [
        "afternoon/20260803_032401_session29",
        "morning/20260803_032401_session29",
    ]
    first_row = sessions[1]
    assert first_row["participants"] == "王洋"
    assert first_row["action_ids"] == "SM001"
    assert first_row["repetition_counts"] == "5"
    assert first_row["mocap_directories"] == (
        "morning/20260803_032401_session29/mocap-SM001-S07-王洋-05p2"
    )
    assert first_row["session_time_utc"] == "2026-08-03T03:24:01+00:00"
    assert first_row["session_time_utc_plus_8"] == "2026-08-03T11:24:01+08:00"

    inspections = _read_csv(result.inspections_csv)
    assert inspections[0]["parse_status"] == "error"
    assert inspections[1]["parse_status"] == "ok"
    assert inspections[1]["mocap_frame_difference"] == "0"
    assert inspections[1]["payload_scalar_json"].find("测试") >= 0
    files = _read_csv(result.files_csv)
    assert any(
        row["root_relative_path"]
        == "morning/20260803_032401_session29/mocap-SM001-S07-王洋-05p2/take.trc"
        for row in files
    )
    assert progress == [
        (1, 2, "afternoon/20260803_032401_session29"),
        (2, 2, "morning/20260803_032401_session29"),
    ]
    assert "morning/20260803_032401_session29" in result.html_path.read_text(encoding="utf-8")
    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    assert names == {
        f"{result.output_dir.name}/files.csv",
        f"{result.output_dir.name}/inspections.csv",
        f"{result.output_dir.name}/segments.csv",
        f"{result.output_dir.name}/sessions.csv",
        f"{result.output_dir.name}/statistics_report.html",
    }


def test_report_output_directory_is_not_recounted(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "20260803_032401_session29"
    session.mkdir()
    (session / "robocap_segment1_video_left.mp4").write_bytes(b"video")
    stale = session / statistics_report.REPORTS_DIRECTORY / "old" / "files.csv"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")

    monkeypatch.setattr(
        statistics_report,
        "summarize_session",
        lambda root, session_dir, ffprobe: SessionStatistic(
            primitive_id="UNASSIGNED",
            session_dir=session_dir,
            unchecked_duration_s=0.0,
            frame_difference_duration_s=0.0,
            clean_duration_s=0.0,
            segments=(),
            errors=(),
        ),
    )
    result = statistics_report.generate_statistics_report(
        tmp_path,
        [session],
        "ffprobe",
        generated_at=datetime(2026, 8, 27, 13, tzinfo=timezone(timedelta(hours=8))),
    )
    paths = {row["root_relative_path"] for row in _read_csv(result.files_csv)}
    assert paths == {"20260803_032401_session29/robocap_segment1_video_left.mp4"}


def test_report_uses_a_new_suffix_when_only_the_base_zip_exists(
    tmp_path: Path, monkeypatch
) -> None:
    session = tmp_path / "20260803_032401_session29"
    session.mkdir()
    generated_at = datetime(2026, 8, 27, 13, tzinfo=timezone(timedelta(hours=8)))
    reports_root = tmp_path / statistics_report.REPORTS_DIRECTORY
    reports_root.mkdir()
    (reports_root / "statistics_report_20260827_130000.zip").write_bytes(b"existing")
    monkeypatch.setattr(
        statistics_report,
        "summarize_session",
        lambda root, session_dir, ffprobe: SessionStatistic(
            primitive_id="UNASSIGNED",
            session_dir=session_dir,
            unchecked_duration_s=0.0,
            frame_difference_duration_s=0.0,
            clean_duration_s=0.0,
            segments=(),
            errors=(),
        ),
    )

    result = statistics_report.generate_statistics_report(
        tmp_path, [session], "ffprobe", generated_at=generated_at
    )

    assert result.output_dir.name == "statistics_report_20260827_130000_2"
    assert result.zip_path.name == "statistics_report_20260827_130000_2.zip"
