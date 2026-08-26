from __future__ import annotations

import bisect
import csv
import io
import json
import math
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .alignment import FrameAlignment
from .data_packager import PackagedFile, encoder_args, is_video

TEXT_MOCAP_SUFFIXES = frozenset({".bvh", ".csv", ".trc", ".xrs"})
MOCAP_SUFFIXES = frozenset({*TEXT_MOCAP_SUFFIXES, ".c3d"})
DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


class DatasetIntersectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrameSlice:
    start: int
    end_exclusive: int
    source_count: int

    def __post_init__(self) -> None:
        if not 0 <= self.start <= self.end_exclusive <= self.source_count:
            raise ValueError(
                "Invalid frame slice: "
                f"{self.start}:{self.end_exclusive} for {self.source_count} source frames."
            )

    @property
    def count(self) -> int:
        return self.end_exclusive - self.start

    def as_manifest(self) -> dict[str, int]:
        return {
            "source_frames": self.source_count,
            "source_start_frame": self.start,
            "source_end_frame_exclusive": self.end_exclusive,
            "staged_frames": self.count,
        }


@dataclass(frozen=True)
class FileFrameSlice:
    relative_path: str
    role: str
    frames: FrameSlice
    source_capture_start_ns: int | None = None
    staged_capture_start_ns: int | None = None

    def as_manifest(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.relative_path,
            "role": self.role,
            **self.frames.as_manifest(),
        }
        if self.source_capture_start_ns is not None:
            payload["source_capture_start_ns"] = self.source_capture_start_ns
        if self.staged_capture_start_ns is not None:
            payload["staged_capture_start_ns"] = self.staged_capture_start_ns
        return payload


@dataclass(frozen=True)
class AlignedIntersectionPlan:
    ratio: float
    video_frame_offset: int
    gt_frame_offset: int
    reference_video: str
    robocap_frames: FrameSlice
    mocap_frames: FrameSlice
    third_person_frames: FrameSlice | None
    capture_start_ns: int | None
    capture_end_ns_exclusive: int | None
    video_slices: tuple[FileFrameSlice, ...]
    motion_slices: tuple[FileFrameSlice, ...]

    def video_slice(self, relative_path: str) -> FileFrameSlice:
        return _selection_for_path(self.video_slices, relative_path)

    def motion_slice(self, relative_path: str) -> FileFrameSlice:
        return _selection_for_path(self.motion_slices, relative_path)

    def as_manifest(self) -> dict[str, object]:
        return {
            "enabled": True,
            "semantics": (
                "source half-open frame intervals cropped to one shared Robocap timeline; "
                "staged streams have zero residual frame offset"
            ),
            "ratio": self.ratio,
            "video_frame_offset": self.video_frame_offset,
            "offset_unit": "robocap_video_frames",
            "gt_frame_offset": self.gt_frame_offset,
            "offset_applied_by_cropping": True,
            "staged_video_frame_offset": 0,
            "staged_gt_frame_offset": 0,
            "reference_video": self.reference_video,
            "robocap": self.robocap_frames.as_manifest(),
            "mocap": self.mocap_frames.as_manifest(),
            "third_person": (
                self.third_person_frames.as_manifest()
                if self.third_person_frames is not None
                else None
            ),
            "capture_time_ns": {
                "start": self.capture_start_ns,
                "end_exclusive": self.capture_end_ns_exclusive,
            },
            "video_files": [selection.as_manifest() for selection in self.video_slices],
            "mocap_files": [selection.as_manifest() for selection in self.motion_slices],
        }

    def as_metadata(self) -> dict[str, object]:
        return {
            "mode": "aligned_intersection",
            "ratio": self.ratio,
            "video_frame_offset": self.video_frame_offset,
            "offset_unit": "robocap_video_frames",
            "gt_frame_offset": self.gt_frame_offset,
            "offset_applied_by_cropping": True,
            "staged_video_frame_offset": 0,
            "staged_gt_frame_offset": 0,
            "reference_video": self.reference_video,
            "robocap": self.robocap_frames.as_manifest(),
            "mocap": self.mocap_frames.as_manifest(),
            "third_person": (
                self.third_person_frames.as_manifest()
                if self.third_person_frames is not None
                else None
            ),
        }

    def summary(self) -> str:
        third = (
            f"; third-person {self.third_person_frames.start}:"
            f"{self.third_person_frames.end_exclusive}"
            if self.third_person_frames is not None
            else ""
        )
        return (
            "Aligned intersection: "
            f"ratio={self.ratio:g}, Robocap offset={self.video_frame_offset:+d}, "
            f"GT offset={self.gt_frame_offset:+d}; "
            f"Robocap {self.robocap_frames.start}:{self.robocap_frames.end_exclusive}; "
            f"mocap {self.mocap_frames.start}:{self.mocap_frames.end_exclusive}"
            f"{third}."
        )


def _selection_for_path(selections: Sequence[FileFrameSlice], relative_path: str) -> FileFrameSlice:
    normalized = Path(relative_path).as_posix()
    for selection in selections:
        if selection.relative_path == normalized:
            return selection
    raise KeyError(f"No aligned frame selection for {normalized}.")


def calculate_aligned_frame_slices(
    robocap_count: int,
    mocap_count: int,
    third_person_count: int | None,
    ratio: float,
    video_frame_offset: int,
) -> tuple[FrameSlice, FrameSlice, FrameSlice | None, FrameAlignment]:
    if robocap_count <= 0 or mocap_count <= 0:
        raise ValueError("Robocap and mocap frame counts must be positive.")
    if third_person_count is not None and third_person_count <= 0:
        raise ValueError("Third-person frame count must be positive when provided.")
    alignment = FrameAlignment(ratio, video_frame_offset)

    common_start = max(0.0, -alignment.gt_frame_offset / alignment.ratio)
    common_end = min(
        float(robocap_count),
        (mocap_count - alignment.gt_frame_offset) / alignment.ratio,
    )
    if third_person_count is not None:
        common_start = max(common_start, float(-video_frame_offset))
        common_end = min(common_end, float(third_person_count - video_frame_offset))

    epsilon = 1e-9
    robocap_start = max(0, math.ceil(common_start - epsilon))
    robocap_end = min(robocap_count, math.floor(common_end + epsilon))
    if robocap_end <= robocap_start:
        raise DatasetIntersectionError(
            "The configured ratio/offset leaves no complete Robocap frame in the aligned "
            f"intersection: Robocap={robocap_count}, mocap={mocap_count}, "
            f"third-person={third_person_count}, ratio={ratio:g}, "
            f"offset={video_frame_offset:+d}."
        )

    mocap_start = max(
        0,
        math.ceil(robocap_start * alignment.ratio + alignment.gt_frame_offset - epsilon),
    )
    mocap_end = min(
        mocap_count,
        math.ceil(robocap_end * alignment.ratio + alignment.gt_frame_offset - epsilon),
    )
    if mocap_end <= mocap_start:
        raise DatasetIntersectionError("The aligned intersection contains no mocap frame.")

    third_slice = None
    if third_person_count is not None:
        third_start = robocap_start + video_frame_offset
        third_end = robocap_end + video_frame_offset
        third_slice = FrameSlice(third_start, third_end, third_person_count)

    return (
        FrameSlice(robocap_start, robocap_end, robocap_count),
        FrameSlice(mocap_start, mocap_end, mocap_count),
        third_slice,
        alignment,
    )


def read_video_frame_timestamps_ns(path: Path) -> tuple[int, ...]:
    import rerun as rr

    return tuple(int(value) for value in rr.AssetVideo(path=path).read_frame_timestamps_nanos())


def video_comment_us(path: Path, ffprobe: str) -> int | None:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags=comment",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        payload = json.loads(result.stdout)
        tags = payload.get("format", {}).get("tags", {})
        value = next(
            (tag_value for key, tag_value in tags.items() if str(key).casefold() == "comment"),
            None,
        )
        return int(float(value)) if value not in {None, ""} else None
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def _text_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        has_utf8_bom = stream.read(3) == b"\xef\xbb\xbf"
    encodings = (
        ("utf-8-sig", "gbk", "latin-1")
        if has_utf8_bom
        else (
            "utf-8",
            "gbk",
            "latin-1",
        )
    )
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as stream:
                stream.read(65_536)
            return encoding
        except UnicodeDecodeError:
            continue
    raise DatasetIntersectionError(f"Unable to decode mocap text file: {path}")


def _prefix_lines(path: Path, encoding: str, limit: int = 2048) -> list[str]:
    lines: list[str] = []
    with path.open("r", encoding=encoding, newline="") as stream:
        for line in stream:
            lines.append(line.rstrip("\r\n"))
            if len(lines) >= limit:
                break
    return lines


def _next_nonempty(lines: Sequence[str], start: int) -> int | None:
    return next((index for index in range(start, len(lines)) if lines[index].strip()), None)


def _split_nokov_line(line: str, suffix: str) -> list[str]:
    if suffix == ".csv":
        return [cell.strip() for cell in next(csv.reader([line]))]
    if "\t" in line:
        return [cell.strip() for cell in line.split("\t")]
    return line.strip().split()


def _trc_structure(lines: Sequence[str]) -> tuple[int, int | None, int | None]:
    frame_header = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("Frame#")),
        None,
    )
    if frame_header is None:
        raise DatasetIntersectionError("TRC file has no Frame# header.")
    for header_index in range(frame_header):
        headers = lines[header_index].split("\t")
        if "NumFrames" not in headers:
            continue
        value_index = _next_nonempty(lines, header_index + 1)
        return frame_header, value_index, headers.index("NumFrames")
    return frame_header, None, None


def _nokov_structure(lines: Sequence[str], suffix: str) -> tuple[int, int | None, int | None]:
    data_section = next(
        (index for index, line in enumerate(lines) if line.strip() == "[SegmentData]"),
        None,
    )
    if data_section is None:
        raise DatasetIntersectionError(f"{suffix.upper()} file has no [SegmentData] section.")
    head_section = next(
        (index for index, line in enumerate(lines) if line.strip() == "[Head]"), None
    )
    if head_section is None or head_section >= data_section:
        return data_section, None, None
    header_index = _next_nonempty(lines, head_section + 1)
    if header_index is None:
        return data_section, None, None
    value_index = _next_nonempty(lines, header_index + 1)
    headers = _split_nokov_line(lines[header_index], suffix)
    if value_index is None or "NumFrames" not in headers:
        return data_section, None, None
    return data_section, value_index, headers.index("NumFrames")


def _first_cell_is_frame(line: str, suffix: str) -> bool:
    if not line.strip():
        return False
    if suffix == ".csv":
        first = next(csv.reader([line]))[0].strip()
    else:
        match = re.search(r"\S+", line)
        first = match.group(0) if match else ""
    return bool(re.fullmatch(r"[+-]?\d+(?:\.0+)?", first))


def _count_data_rows(path: Path, encoding: str, start_line: int, suffix: str) -> int:
    count = 0
    with path.open("r", encoding=encoding, newline="") as stream:
        for index, line in enumerate(stream):
            if index > start_line and _first_cell_is_frame(line, suffix):
                count += 1
    return count


def mocap_frame_count(path: Path) -> int:
    suffix = path.suffix.casefold()
    if suffix not in MOCAP_SUFFIXES:
        raise DatasetIntersectionError(f"Unsupported mocap format for aligned staging: {path}")
    if suffix == ".c3d":
        return c3d_metadata(path)[0]
    encoding = _text_encoding(path)
    lines = _prefix_lines(path, encoding)
    if suffix == ".bvh":
        motion_index = next(
            (index for index, line in enumerate(lines) if line.strip() == "MOTION"), None
        )
        if motion_index is None:
            raise DatasetIntersectionError(f"BVH file has no MOTION section: {path}")
        frames_index = _next_nonempty(lines, motion_index + 1)
        if frames_index is None or not lines[frames_index].lstrip().startswith("Frames:"):
            raise DatasetIntersectionError(f"BVH file has no Frames declaration: {path}")
        return int(lines[frames_index].split(":", 1)[1].strip())
    if suffix == ".trc":
        data_start, value_index, column_index = _trc_structure(lines)
    else:
        data_start, value_index, column_index = _nokov_structure(lines, suffix)
    if value_index is not None and column_index is not None:
        values = (
            lines[value_index].split("\t")
            if suffix == ".trc"
            else _split_nokov_line(lines[value_index], suffix)
        )
        if column_index < len(values):
            return int(float(values[column_index].strip()))
    count = _count_data_rows(path, encoding, data_start, suffix)
    if count <= 0:
        raise DatasetIntersectionError(f"Mocap file has no frame rows: {path}")
    return count


def _relative_is_under(path: Path, session_dir: Path, directory: str) -> bool:
    relative = path.relative_to(session_dir)
    return bool(relative.parts and relative.parts[0].casefold() == directory.casefold())


def _reference_video(
    session_dir: Path,
    files: Sequence[Path],
    segment: str | None,
    preferred_label: str,
) -> Path:
    candidates = [
        path
        for path in files
        if is_video(path)
        and not _relative_is_under(path, session_dir, "mocap")
        and path.name.casefold().startswith("robocap_")
    ]
    if not candidates:
        raise DatasetIntersectionError("Aligned staging requires a Robocap reference video.")
    preferred_suffix = f"video_{preferred_label.casefold()}{candidates[0].suffix.casefold()}"

    def rank(path: Path) -> tuple[int, int, str]:
        name = path.name.casefold()
        segment_match = segment is None or segment.casefold() in name
        return (
            0 if segment_match else 1,
            0 if name.endswith(preferred_suffix) else 1,
            path.as_posix().casefold(),
        )

    return min(candidates, key=rank)


def _frame_end_timestamp_ns(timestamps_ns: Sequence[int], end_exclusive: int) -> int:
    if end_exclusive < len(timestamps_ns):
        return int(timestamps_ns[end_exclusive])
    positive_diffs = [
        current - previous for previous, current in pairwise(timestamps_ns) if current > previous
    ]
    frame_duration_ns = round(statistics.median(positive_diffs)) if positive_diffs else 33_333_333
    return int(timestamps_ns[-1]) + frame_duration_ns


def _slice_video_by_capture_window(
    timestamps_ns: Sequence[int],
    comment_us: int,
    capture_start_ns: int,
    capture_end_ns_exclusive: int,
) -> FrameSlice:
    absolute = [comment_us * 1_000 + value for value in timestamps_ns]
    start = bisect.bisect_left(absolute, capture_start_ns)
    end = bisect.bisect_left(absolute, capture_end_ns_exclusive)
    return FrameSlice(start, end, len(timestamps_ns))


def build_aligned_intersection_plan(
    session_dir: Path,
    files: Sequence[Path],
    *,
    segment: str | None,
    ratio: float,
    video_frame_offset: int,
    ffprobe: str,
    reference_video_label: str = "left",
    progress: Callable[[str], None] | None = None,
) -> AlignedIntersectionPlan:
    source = session_dir.resolve()
    reference = _reference_video(source, files, segment, reference_video_label)
    all_mocap_data_files = [
        path for path in files if _relative_is_under(path, source, "mocap") and not is_video(path)
    ]
    unsupported_mocap_files = [
        path for path in all_mocap_data_files if path.suffix.casefold() not in MOCAP_SUFFIXES
    ]
    if unsupported_mocap_files:
        relative_files = ", ".join(
            path.relative_to(source).as_posix() for path in unsupported_mocap_files
        )
        raise DatasetIntersectionError(
            "Aligned staging cannot crop these motion-capture formats: "
            f"{relative_files}. Export BVH, TRC, CSV, XRS, or C3D, or disable aligned intersection."
        )
    mocap_files = all_mocap_data_files
    if not mocap_files:
        raise DatasetIntersectionError(
            "Aligned staging requires at least one BVH, TRC, CSV, XRS, or C3D file under mocap/."
        )
    third_videos = [
        path for path in files if _relative_is_under(path, source, "mocap") and is_video(path)
    ]

    reference_timestamps = read_video_frame_timestamps_ns(reference)
    if not reference_timestamps:
        raise DatasetIntersectionError(f"Reference video has no readable frames: {reference}")
    motion_counts = {path: mocap_frame_count(path) for path in mocap_files}
    third_timestamps = {path: read_video_frame_timestamps_ns(path) for path in third_videos}
    if any(not timestamps for timestamps in third_timestamps.values()):
        empty = next(path for path, timestamps in third_timestamps.items() if not timestamps)
        raise DatasetIntersectionError(f"Third-person video has no readable frames: {empty}")

    mocap_count = min(motion_counts.values())
    third_count = min((len(values) for values in third_timestamps.values()), default=None)
    robocap_slice, mocap_slice, third_slice, alignment = calculate_aligned_frame_slices(
        len(reference_timestamps),
        mocap_count,
        third_count,
        ratio,
        video_frame_offset,
    )

    reference_comment_us = video_comment_us(reference, ffprobe)
    capture_start_ns = None
    capture_end_ns = None
    if reference_comment_us is not None:
        capture_start_ns = reference_comment_us * 1_000 + reference_timestamps[robocap_slice.start]
        capture_end_ns = reference_comment_us * 1_000 + _frame_end_timestamp_ns(
            reference_timestamps, robocap_slice.end_exclusive
        )

    video_selections: list[FileFrameSlice] = []
    for path in (item for item in files if is_video(item)):
        relative = path.relative_to(source).as_posix()
        timestamps = (
            reference_timestamps
            if path == reference
            else third_timestamps.get(path) or read_video_frame_timestamps_ns(path)
        )
        if path in third_timestamps:
            assert third_slice is not None
            frames = FrameSlice(
                third_slice.start,
                third_slice.end_exclusive,
                len(timestamps),
            )
            role = "third_person"
            comment_us = video_comment_us(path, ffprobe)
            source_capture_start_ns = comment_us * 1_000 if comment_us is not None else None
            staged_capture_start_ns = (
                source_capture_start_ns + timestamps[frames.start]
                if source_capture_start_ns is not None and frames.count
                else None
            )
        elif path == reference:
            frames = robocap_slice
            role = "robocap_reference"
            source_capture_start_ns = (
                reference_comment_us * 1_000 if reference_comment_us is not None else None
            )
            staged_capture_start_ns = capture_start_ns
        elif path.name.casefold().startswith("robocap_"):
            frames = FrameSlice(
                min(robocap_slice.start, len(timestamps)),
                min(robocap_slice.end_exclusive, len(timestamps)),
                len(timestamps),
            )
            role = "robocap"
            comment_us = video_comment_us(path, ffprobe)
            source_capture_start_ns = comment_us * 1_000 if comment_us is not None else None
            staged_capture_start_ns = (
                source_capture_start_ns + timestamps[frames.start]
                if source_capture_start_ns is not None and frames.count
                else None
            )
        else:
            comment_us = video_comment_us(path, ffprobe)
            if comment_us is None or capture_start_ns is None or capture_end_ns is None:
                raise DatasetIntersectionError(
                    f"Cannot align an ancillary video without capture-start metadata: {path}."
                )
            else:
                frames = _slice_video_by_capture_window(
                    timestamps, comment_us, capture_start_ns, capture_end_ns
                )
            role = "robowrist" if "robowrist" in relative.casefold() else "ancillary"
            source_capture_start_ns = comment_us * 1_000 if comment_us is not None else None
            staged_capture_start_ns = (
                source_capture_start_ns + timestamps[frames.start]
                if source_capture_start_ns is not None and frames.count
                else None
            )
        if frames.count <= 0:
            raise DatasetIntersectionError(
                f"Video has no frame inside the aligned intersection: {path}"
            )
        video_selections.append(
            FileFrameSlice(
                relative,
                role,
                frames,
                source_capture_start_ns,
                staged_capture_start_ns,
            )
        )

    motion_selections = tuple(
        FileFrameSlice(
            path.relative_to(source).as_posix(),
            f"mocap_{path.suffix.casefold().lstrip('.')}",
            FrameSlice(mocap_slice.start, mocap_slice.end_exclusive, count),
        )
        for path, count in sorted(motion_counts.items(), key=lambda item: item[0].as_posix())
    )
    plan = AlignedIntersectionPlan(
        ratio=alignment.ratio,
        video_frame_offset=alignment.video_frame_offset,
        gt_frame_offset=alignment.gt_frame_offset,
        reference_video=reference.relative_to(source).as_posix(),
        robocap_frames=robocap_slice,
        mocap_frames=mocap_slice,
        third_person_frames=third_slice,
        capture_start_ns=capture_start_ns,
        capture_end_ns_exclusive=capture_end_ns,
        video_slices=tuple(video_selections),
        motion_slices=motion_selections,
    )
    if any(path.suffix.casefold() in DATABASE_SUFFIXES for path in files) and (
        plan.capture_start_ns is None or plan.capture_end_ns_exclusive is None
    ):
        raise DatasetIntersectionError(
            "Cannot crop SQLite sensors because the Robocap reference video has no numeric "
            "comment capture-start timestamp."
        )
    if progress is not None:
        progress(plan.summary())
    return plan


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _replace_delimited_field(line: str, field_index: int, value: str, suffix: str) -> str:
    body, ending = _split_line_ending(line)
    if suffix == ".csv":
        cells = next(csv.reader([body]))
        if field_index >= len(cells):
            raise DatasetIntersectionError("NumFrames value row is shorter than its header.")
        cells[field_index] = value
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator=ending).writerow(cells)
        return output.getvalue()
    if "\t" in body:
        cells = body.split("\t")
        if field_index >= len(cells):
            raise DatasetIntersectionError("NumFrames value row is shorter than its header.")
        cells[field_index] = value
        return "\t".join(cells) + ending
    matches = list(re.finditer(r"\S+", body))
    if field_index >= len(matches):
        raise DatasetIntersectionError("NumFrames value row is shorter than its header.")
    match = matches[field_index]
    return body[: match.start()] + value + body[match.end() :] + ending


def _temporary_path(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
    ) as handle:
        return Path(handle.name)


def crop_mocap_text(source: Path, target: Path, frames: FrameSlice) -> int:
    suffix = source.suffix.casefold()
    encoding = _text_encoding(source)
    lines = _prefix_lines(source, encoding)
    if suffix == ".bvh":
        motion_index = next(
            (index for index, line in enumerate(lines) if line.strip() == "MOTION"), None
        )
        if motion_index is None:
            raise DatasetIntersectionError(f"BVH file has no MOTION section: {source}")
        declaration_index = _next_nonempty(lines, motion_index + 1)
        frame_time_index = (
            _next_nonempty(lines, declaration_index + 1) if declaration_index is not None else None
        )
        if declaration_index is None or frame_time_index is None:
            raise DatasetIntersectionError(f"BVH motion header is incomplete: {source}")
        data_start = frame_time_index
        value_index = declaration_index
        column_index = None
    elif suffix == ".trc":
        data_start, value_index, column_index = _trc_structure(lines)
    elif suffix in {".csv", ".xrs"}:
        data_start, value_index, column_index = _nokov_structure(lines, suffix)
    else:
        raise DatasetIntersectionError(f"Unsupported mocap format for aligned staging: {source}")

    temporary = _temporary_path(target)
    written = 0
    source_index = 0
    with (
        source.open("r", encoding=encoding, newline="") as input_stream,
        temporary.open("w", encoding=encoding, newline="") as output_stream,
    ):
        for line_index, line in enumerate(input_stream):
            if line_index == value_index:
                if suffix == ".bvh":
                    body, ending = _split_line_ending(line)
                    prefix = body.split(":", 1)[0]
                    line = f"{prefix}: {frames.count}{ending}"
                elif column_index is not None:
                    line = _replace_delimited_field(line, column_index, str(frames.count), suffix)

            is_frame = line_index > data_start and (
                bool(line.strip()) if suffix == ".bvh" else _first_cell_is_frame(line, suffix)
            )
            if not is_frame:
                output_stream.write(line)
                continue
            if frames.start <= source_index < frames.end_exclusive:
                output_stream.write(line)
                written += 1
            source_index += 1

    if source_index < frames.end_exclusive or written != frames.count:
        raise DatasetIntersectionError(
            f"Mocap frame rows do not match the declared range in {source}: "
            f"found {source_index}, need {frames.end_exclusive}; wrote {written}, "
            f"expected {frames.count}."
        )
    temporary.replace(target)
    shutil.copystat(source, target)
    return written


def _load_c3d(path: Path):
    import ezc3d

    # ezc3d's native Windows reader cannot open non-ASCII paths. Loading through an
    # ASCII temporary path also makes behavior consistent across platforms.
    with tempfile.TemporaryDirectory(prefix="robocap_c3d_read_") as temp_dir:
        local_source = Path(temp_dir) / "source.c3d"
        shutil.copy2(path, local_source)
        try:
            return ezc3d.c3d(str(local_source))
        except (OSError, RuntimeError, ValueError) as exc:
            raise DatasetIntersectionError(f"Unable to read C3D file: {path}") from exc


def c3d_metadata(path: Path) -> tuple[int, float, int]:
    capture = _load_c3d(path)
    count = int(capture["data"]["points"].shape[2])
    if count <= 0:
        raise DatasetIntersectionError(f"C3D file has no point frames: {path}")
    try:
        frame_rate = float(capture["parameters"]["POINT"]["RATE"]["value"][0])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise DatasetIntersectionError(f"C3D file has no valid POINT:RATE: {path}") from exc
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        raise DatasetIntersectionError(f"C3D file has invalid POINT:RATE {frame_rate!r}: {path}")
    first_frame = int(capture["header"]["points"]["first_frame"])
    return count, frame_rate, first_frame


def _crop_c3d_axis(data, frames: FrameSlice, source_count: int, label: str):
    count = int(data.shape[-1])
    if count == 0:
        return data
    if count % source_count != 0:
        raise DatasetIntersectionError(
            f"C3D {label} sample count {count} is not an integer multiple of "
            f"the {source_count} point frames."
        )
    samples_per_frame = count // source_count
    return data[
        ...,
        frames.start * samples_per_frame : frames.end_exclusive * samples_per_frame,
    ].copy()


def crop_c3d(source: Path, target: Path, frames: FrameSlice) -> int:
    capture = _load_c3d(source)
    points = capture["data"]["points"]
    source_count = int(points.shape[2])
    if source_count != frames.source_count:
        raise DatasetIntersectionError(
            f"C3D frame count changed while staging {source}: "
            f"planned {frames.source_count}, found {source_count}."
        )

    capture["data"]["points"] = points[..., frames.start : frames.end_exclusive].copy()
    meta_points = capture["data"].get("meta_points", {})
    for key in ("residuals", "camera_masks"):
        values = meta_points.get(key)
        if values is not None and getattr(values, "ndim", 0) == 3:
            meta_points[key] = values[..., frames.start : frames.end_exclusive].copy()
    capture["data"]["analogs"] = _crop_c3d_axis(
        capture["data"]["analogs"], frames, source_count, "analog"
    )
    rotations = capture["data"].get("rotations")
    if rotations is not None and getattr(rotations, "ndim", 0) == 4:
        capture["data"]["rotations"] = _crop_c3d_axis(rotations, frames, source_count, "rotation")
    capture["header"]["points"]["first_frame"] = (
        int(capture["header"]["points"]["first_frame"]) + frames.start
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target)
    try:
        with tempfile.TemporaryDirectory(prefix="robocap_c3d_write_") as temp_dir:
            local_target = Path(temp_dir) / "staged.c3d"
            try:
                capture.write(str(local_target))
            except (OSError, RuntimeError, ValueError) as exc:
                raise DatasetIntersectionError(
                    f"Unable to write cropped C3D file: {source}"
                ) from exc
            shutil.copy2(local_target, temporary)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    shutil.copystat(source, target)

    written = mocap_frame_count(target)
    if written != frames.count:
        raise DatasetIntersectionError(
            f"C3D crop failed for {source}: wrote {written}, expected {frames.count}."
        )
    return written


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def crop_sqlite_database(
    source: Path,
    target: Path,
    capture_start_ns: int,
    capture_end_ns_exclusive: int,
) -> dict[str, dict[str, int | None]]:
    temporary = _temporary_path(target)
    table_records: dict[str, dict[str, int | None]] = {}
    with (
        closing(sqlite3.connect(source)) as input_connection,
        closing(sqlite3.connect(temporary)) as output,
    ):
        input_connection.backup(output)
        tables = [
            str(row[0])
            for row in output.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            quoted = _quoted_identifier(table)
            columns = {str(row[1]) for row in output.execute(f"PRAGMA table_info({quoted})")}
            if "timestamp" not in columns:
                continue
            source_count, source_min, source_max = output.execute(
                f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM {quoted}"
            ).fetchone()
            output.execute(
                f"DELETE FROM {quoted} WHERE timestamp < ? OR timestamp >= ?",
                (capture_start_ns, capture_end_ns_exclusive),
            )
            staged_count, staged_min, staged_max = output.execute(
                f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM {quoted}"
            ).fetchone()
            table_records[table] = {
                "source_rows": int(source_count),
                "staged_rows": int(staged_count),
                "source_start_ns": int(source_min) if source_min is not None else None,
                "source_end_ns": int(source_max) if source_max is not None else None,
                "staged_start_ns": int(staged_min) if staged_min is not None else None,
                "staged_end_ns": int(staged_max) if staged_max is not None else None,
            }
        output.commit()
        output.execute("VACUUM")
        output.commit()
    temporary.replace(target)
    shutil.copystat(source, target)
    return table_records


def _aligned_video_filter(frames: FrameSlice, height: int | None) -> str:
    filters = [
        f"trim=start_frame={frames.start}:end_frame={frames.end_exclusive}",
        "setpts=PTS-STARTPTS",
    ]
    if height is not None:
        filters.append(f"scale=-2:{height}")
    return ",".join(filters)


def _run_aligned_video_encoder(
    source: Path,
    target: Path,
    ffmpeg: str,
    frames: FrameSlice,
    *,
    height: int | None,
    crf: int,
    bitrate: str,
    comment_us: int | None,
) -> None:
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map_metadata",
        "0",
        "-vf",
        _aligned_video_filter(frames, height),
    ]
    metadata = ["-metadata", f"comment={comment_us}"] if comment_us is not None else []
    output = [
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-movflags",
        "+faststart",
        "-an",
        *metadata,
        str(target),
    ]
    try:
        subprocess.run([*common, *encoder_args(crf, bitrate), *output], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(
            [
                *common,
                "-c:v",
                "libopenh264",
                "-b:v",
                bitrate,
                *output,
            ],
            check=True,
        )


def crop_video(
    source: Path,
    target: Path,
    ffmpeg: str,
    frames: FrameSlice,
    *,
    raw_video: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    source_capture_start_ns: int | None,
    staged_capture_start_ns: int | None,
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged_comment_us = (
        round(staged_capture_start_ns / 1_000)
        if source_capture_start_ns is not None and staged_capture_start_ns is not None
        else None
    )
    _run_aligned_video_encoder(
        source,
        target,
        ffmpeg,
        frames,
        height=None if raw_video else proxy_height,
        crf=0 if raw_video else proxy_crf,
        bitrate=proxy_bitrate,
        comment_us=staged_comment_us,
    )
    output_count = len(read_video_frame_timestamps_ns(target))
    if output_count != frames.count:
        raise DatasetIntersectionError(
            f"Frame-accurate video crop failed for {source}: wrote {output_count}, "
            f"expected {frames.count}."
        )
    return output_count


def stage_aligned_file(
    source: Path,
    session_dir: Path,
    target_dir: Path,
    plan: AlignedIntersectionPlan,
    *,
    raw_video: bool,
    ffmpeg: str,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
) -> tuple[PackagedFile, dict[str, object]]:
    relative = source.relative_to(session_dir)
    relative_name = relative.as_posix()
    original_size = source.stat().st_size
    if is_video(source):
        selection = plan.video_slice(relative_name)
        package_relative = relative.with_suffix(".mp4")
        target = target_dir / package_relative
        crop_video(
            source,
            target,
            ffmpeg,
            selection.frames,
            raw_video=raw_video,
            proxy_height=proxy_height,
            proxy_crf=proxy_crf,
            proxy_bitrate=proxy_bitrate,
            source_capture_start_ns=selection.source_capture_start_ns,
            staged_capture_start_ns=selection.staged_capture_start_ns,
        )
        packaged = PackagedFile(
            source=relative_name,
            packaged_as=package_relative.as_posix(),
            kind="video_aligned_lossless" if raw_video else "video_aligned_proxy",
            original_bytes=original_size,
            packaged_bytes=target.stat().st_size,
            compressed_video=not raw_video,
        )
        return packaged, selection.as_manifest()

    target = target_dir / relative
    if source.suffix.casefold() in MOCAP_SUFFIXES and _relative_is_under(
        source, session_dir, "mocap"
    ):
        selection = plan.motion_slice(relative_name)
        if source.suffix.casefold() == ".c3d":
            crop_c3d(source, target, selection.frames)
        else:
            crop_mocap_text(source, target, selection.frames)
        packaged = PackagedFile(
            source=relative_name,
            packaged_as=relative_name,
            kind="mocap_aligned",
            original_bytes=original_size,
            packaged_bytes=target.stat().st_size,
            compressed_video=False,
        )
        return packaged, selection.as_manifest()

    if source.suffix.casefold() in DATABASE_SUFFIXES:
        if plan.capture_start_ns is None or plan.capture_end_ns_exclusive is None:
            raise DatasetIntersectionError(
                f"Aligned capture-time window is unavailable for SQLite file: {source}"
            )
        tables = crop_sqlite_database(
            source,
            target,
            plan.capture_start_ns,
            plan.capture_end_ns_exclusive,
        )
        packaged = PackagedFile(
            source=relative_name,
            packaged_as=relative_name,
            kind="sensor_aligned",
            original_bytes=original_size,
            packaged_bytes=target.stat().st_size,
            compressed_video=False,
        )
        return packaged, {
            "source": relative_name,
            "role": "sqlite_sensor",
            "capture_start_ns": plan.capture_start_ns,
            "capture_end_ns_exclusive": plan.capture_end_ns_exclusive,
            "tables": tables,
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    packaged = PackagedFile(
        source=relative_name,
        packaged_as=relative_name,
        kind="data",
        original_bytes=original_size,
        packaged_bytes=target.stat().st_size,
        compressed_video=False,
    )
    return packaged, {"source": relative_name, "role": "copied_without_timeline"}
