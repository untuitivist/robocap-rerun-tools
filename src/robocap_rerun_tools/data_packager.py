from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}
DEFAULT_EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
DEFAULT_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


@dataclass
class PackagedFile:
    source: str
    packaged_as: str
    kind: str
    original_bytes: int
    packaged_bytes: int
    compressed_video: bool


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


def is_excluded(path: Path, session_dir: Path, include_artifacts: bool, include_rrd: bool) -> bool:
    relative = path.relative_to(session_dir)
    if any(part in DEFAULT_EXCLUDED_DIRS for part in relative.parts):
        return True
    if not include_artifacts and "_artifacts" in relative.parts:
        return True
    if not include_rrd and path.suffix.lower() == ".rrd":
        return True
    return path.suffix.lower() in DEFAULT_EXCLUDED_SUFFIXES


def matches_segment(path: Path, session_dir: Path, segment: str | None) -> bool:
    if segment is None:
        return True
    relative = path.relative_to(session_dir)
    if relative.parts and relative.parts[0].lower().startswith("test"):
        return True
    if "segment" not in path.name.lower():
        return True
    return segment.lower() in path.name.lower()


def discover_package_files(
    session_dir: Path,
    segment: str | None,
    include_artifacts: bool,
    include_rrd: bool,
) -> list[Path]:
    files: list[Path] = []
    for path in sorted(session_dir.rglob("*")):
        if not path.is_file():
            continue
        if is_excluded(path, session_dir, include_artifacts, include_rrd):
            continue
        if not matches_segment(path, session_dir, segment):
            continue
        files.append(path)
    return files


def encoder_args(crf: int, bitrate: str) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]


def compress_video(source: Path, target: Path, ffmpeg: str, height: int, crf: int, bitrate: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-map_metadata",
        "0",
        "-vf",
        f"scale=-2:{height},fps=30",
        *encoder_args(crf, bitrate),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(target),
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError:
        fallback = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "0",
            "-vf",
            f"scale=-2:{height},fps=30",
            "-c:v",
            "libopenh264",
            "-b:v",
            bitrate,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(target),
        ]
        subprocess.run(fallback, check=True)


def copy_or_compress_file(
    source: Path,
    session_dir: Path,
    staging_root: Path,
    raw_video: bool,
    ffmpeg: str,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
) -> PackagedFile:
    relative = source.relative_to(session_dir)
    original_size = source.stat().st_size
    if is_video(source) and not raw_video:
        package_relative = relative.with_suffix(".mp4")
        target = staging_root / package_relative
        compress_video(source, target, ffmpeg, proxy_height, proxy_crf, proxy_bitrate)
        return PackagedFile(
            source=str(relative),
            packaged_as=str(package_relative),
            kind="video_proxy",
            original_bytes=original_size,
            packaged_bytes=target.stat().st_size,
            compressed_video=True,
        )

    target = staging_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return PackagedFile(
        source=str(relative),
        packaged_as=str(relative),
        kind="video_raw" if is_video(source) else "data",
        original_bytes=original_size,
        packaged_bytes=target.stat().st_size,
        compressed_video=False,
    )


def write_manifest(
    staging_root: Path,
    session_dir: Path,
    files: list[PackagedFile],
    args: argparse.Namespace,
) -> None:
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_session": str(session_dir),
        "package_root": session_dir.name,
        "options": {
            "segment": args.segment,
            "raw_video": args.raw_video,
            "proxy_height": args.proxy_height,
            "proxy_crf": args.proxy_crf,
            "proxy_bitrate": args.proxy_bitrate,
            "include_artifacts": args.include_artifacts,
            "include_rrd": args.include_rrd,
        },
        "files": [asdict(item) for item in files],
    }
    (staging_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with (staging_root / "manifest.tsv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["source", "packaged_as", "kind", "original_bytes", "packaged_bytes", "compressed_video"])
        for item in files:
            writer.writerow(
                [
                    item.source,
                    item.packaged_as,
                    item.kind,
                    item.original_bytes,
                    item.packaged_bytes,
                    item.compressed_video,
                ]
            )


def make_zip(staging_root: Path, output: Path, package_root_name: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(staging_root.rglob("*")):
            if path.is_file():
                archive.write(path, Path(package_root_name) / path.relative_to(staging_root))


def default_output(session_dir: Path, segment: str | None) -> Path:
    suffix = f"_{segment}" if segment else ""
    return session_dir / "_artifacts" / "packages" / f"{session_dir.name}{suffix}_data_package.zip"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Package one Robocap/NOKOV session for sharing.")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--segment", default=None)
    parser.add_argument("--raw-video", action="store_true", help="Copy original videos instead of compressed proxy MP4.")
    parser.add_argument("--proxy-height", type=int, default=540)
    parser.add_argument("--proxy-crf", type=int, default=28)
    parser.add_argument("--proxy-bitrate", default="1400k")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--include-artifacts", action="store_true")
    parser.add_argument("--include-rrd", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def package_session(args: argparse.Namespace) -> Path:
    session_dir = args.session_dir.resolve()
    if not session_dir.exists() or not session_dir.is_dir():
        raise FileNotFoundError(session_dir)
    output = args.output or default_output(session_dir, args.segment)
    files = discover_package_files(session_dir, args.segment, args.include_artifacts, args.include_rrd)
    videos = sum(1 for path in files if is_video(path))
    print(f"Discovered {len(files)} files, including {videos} videos.")
    print(f"Output: {output}")
    if args.dry_run:
        for path in files:
            print(path.relative_to(session_dir))
        return output

    with tempfile.TemporaryDirectory(prefix="robocap_package_") as temp_dir:
        staging_root = Path(temp_dir)
        packaged: list[PackagedFile] = []
        for index, source in enumerate(files, start=1):
            relative = source.relative_to(session_dir)
            print(f"[{index}/{len(files)}] {'compress' if is_video(source) and not args.raw_video else 'copy'} {relative}")
            packaged.append(
                copy_or_compress_file(
                    source,
                    session_dir,
                    staging_root,
                    args.raw_video,
                    args.ffmpeg,
                    args.proxy_height,
                    args.proxy_crf,
                    args.proxy_bitrate,
                )
            )
        write_manifest(staging_root, session_dir, packaged, args)
        make_zip(staging_root, output, session_dir.name)
    print(f"Wrote package: {output}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    package_session(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

