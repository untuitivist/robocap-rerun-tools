from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


EN_DOC = """# Robocap Rerun Tools

This is a local browser UI for Robocap/NOKOV inspection, data packaging, RRD export, and offset checks.

## Basic Workflow

1. Enter a session directory, for example `C:\\Users\\Administrator\\Desktop\\20260803_032401_session29`.
2. Enter the segment name, usually `segment1`.
3. Run `Inspect` first to check FPS, frame counts, and abnormal intervals.
4. Use `Package Data` to create a zip for sharing. Videos are compressed by default.
5. Use `Export RRD` to create time-aligned or frame-aligned Rerun files.
6. Use `Offset` when you need to inspect or sweep a video-to-NOKOV frame offset.

## Alignment

Frame alignment uses:

```text
video frame N -> NOKOV frame round(N * ratio) + offset
```

Use `ratio=auto` for measured FPS ratio, or `ratio=8` for the historical 240/30 mapping.
"""


ZH_DOC = """# Robocap Rerun Tools 中文说明

这是一个本地网页工具，用于 Robocap/NOKOV 数据检查、数据打包、RRD 导出和 offset 检查。

## 基本流程

1. 输入 session 目录，例如 `C:\\Users\\Administrator\\Desktop\\20260803_032401_session29`。
2. 输入 segment，通常是 `segment1`。
3. 先运行“检查”，查看视频 FPS、帧数和异常帧间隔。
4. 用“打包数据”生成 zip 给别人使用。默认会压缩视频。
5. 用“导出 RRD”生成时间对齐或帧对齐的 Rerun 文件。
6. 如果需要检查视频帧和 NOKOV 帧的偏移关系，用“Offset 检查”。

## 对齐公式

帧对齐使用：

```text
video frame N -> NOKOV frame round(N * ratio) + offset
```

`ratio=auto` 表示使用检测到的 FPS 比例；`ratio=8` 表示使用历史上的 240/30 固定映射。
"""


LANGUAGE_PACKS = {
    "English": {
        "title": "# Robocap Rerun Tools",
        "language": "Language",
        "session": "Session directory",
        "segment": "Segment",
        "output": "Output",
        "inspect_button": "Inspect",
        "package_output": "Output zip",
        "package_height": "Proxy height",
        "package_crf": "Proxy CRF",
        "package_button": "Package",
        "mode": "Alignment mode",
        "ratio": "Ratio",
        "offset": "Offset",
        "save_path": "Save path",
        "mano_model_dir": "MANO model dir",
        "use_proxy": "Use compressed proxy video",
        "display": "Display layout",
        "no_mano_mesh": "Disable MANO mesh",
        "scan_button": "Scan files",
        "gt_dir": "GT/NOKOV export dir",
        "gt_files": "GT files to include",
        "retarget_model": "Retarget model",
        "third_person_video": "Third-person video",
        "include_third_person": "Include third-person video",
        "include_robowrist": "Include robowrist data",
        "export_height": "Proxy height",
        "export_button": "Export RRD",
        "nokov_source": "NOKOV source",
        "offset_button": "Inspect Offset",
        "offset_min": "Offset min",
        "offset_max": "Offset max",
        "sweep_button": "Sweep Offset",
        "doc": EN_DOC,
    },
    "中文": {
        "title": "# Robocap Rerun Tools 中文界面",
        "language": "语言",
        "session": "Session 目录",
        "segment": "Segment",
        "output": "输出",
        "inspect_button": "检查",
        "package_output": "输出 zip",
        "package_height": "压缩视频高度",
        "package_crf": "压缩 CRF",
        "package_button": "打包数据",
        "mode": "对齐模式",
        "ratio": "比例 ratio",
        "offset": "Offset",
        "save_path": "RRD 保存路径",
        "mano_model_dir": "MANO 模型目录",
        "use_proxy": "使用压缩视频",
        "display": "展示版布局",
        "no_mano_mesh": "禁用 MANO mesh",
        "scan_button": "扫描文件",
        "gt_dir": "GT/NOKOV 导出目录",
        "gt_files": "参与导出的 GT 文件",
        "retarget_model": "重定向模型",
        "third_person_video": "第三人称视频",
        "include_third_person": "包含第三人称视频",
        "include_robowrist": "包含 robowrist 数据",
        "export_height": "压缩视频高度",
        "export_button": "导出 RRD",
        "nokov_source": "NOKOV 参考源",
        "offset_button": "检查 Offset",
        "offset_min": "Offset 最小值",
        "offset_max": "Offset 最大值",
        "sweep_button": "扫描 Offset",
        "doc": ZH_DOC,
    },
}


def run_cli(args: list[str]) -> str:
    command = [sys.executable, "-m", "robocap_rerun_tools.cli", *args]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    output = []
    if proc.stdout:
        output.append(proc.stdout.rstrip())
    if proc.stderr:
        output.append(proc.stderr.rstrip())
    if proc.returncode != 0:
        output.append(f"Command failed with exit code {proc.returncode}.")
    return "\n".join(output) if output else "Done."


def session_path(value: str) -> str:
    path = Path(value.strip().strip('"'))
    if not path.exists():
        raise ValueError(f"Session path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Session path is not a directory: {path}")
    return str(path)


def optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def default_gt_dir(path: Path) -> Path | None:
    matches = sorted(child for child in path.glob("test*") if child.is_dir())
    if len(matches) == 1:
        return matches[0]
    return None


def scan_files(session_dir: str, gt_dir_value: str) -> tuple[str, object, str, str]:
    path = Path(session_path(session_dir))
    gt_dir = Path(gt_dir_value.strip().strip('"')) if optional_text(gt_dir_value) else default_gt_dir(path)
    if gt_dir is None or not gt_dir.exists():
        return "No single test* GT directory found. Fill GT/NOKOV export dir manually.", [], "", ""

    gt_suffixes = {".bvh", ".trc", ".csv", ".xrs"}
    gt_files = sorted(
        file
        for file in gt_dir.rglob("*")
        if file.is_file() and "_artifacts" not in file.relative_to(gt_dir).parts and file.suffix.lower() in gt_suffixes
    )
    choices = [str(file.relative_to(gt_dir)) for file in gt_files]
    videos = sorted(file for file in gt_dir.rglob("*.mp4") if file.is_file())
    third_person = str(videos[0]) if videos else ""
    robowrist_count = len(list(path.glob("robowrist_*")))
    summary = "\n".join(
        [
            f"GT dir: {gt_dir}",
            f"GT files: {len(choices)}",
            f"Third-person video candidates: {len(videos)}",
            f"Robowrist folders: {robowrist_count}",
        ]
    )
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError('Web UI requires Gradio. Install it with: uv pip install -e ".[web]"') from exc
    return summary, gr.update(choices=choices, value=choices), str(gt_dir), third_person


def inspect_session(session_dir: str, segment: str) -> str:
    args = ["inspect", session_path(session_dir)]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    return run_cli(args)


def package_data(session_dir: str, segment: str, output_zip: str, proxy_height: int, proxy_crf: int) -> str:
    args = ["package-data", session_path(session_dir)]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(output_zip):
        args.extend(["--output", output_zip.strip()])
    args.extend(["--proxy-height", str(int(proxy_height)), "--proxy-crf", str(int(proxy_crf))])
    return run_cli(args)


def inspect_offset(session_dir: str, segment: str, ratio: str, offset: int, nokov_source: str) -> str:
    args = ["inspect-offset", session_path(session_dir), "--ratio", ratio.strip() or "auto", "--offset", str(int(offset))]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(nokov_source):
        args.extend(["--nokov-source", nokov_source.strip()])
    return run_cli(args)


def sweep_offset(session_dir: str, segment: str, ratio: str, offset_min: int, offset_max: int, nokov_source: str) -> str:
    args = [
        "sweep-offset",
        session_path(session_dir),
        "--ratio",
        ratio.strip() or "auto",
        "--offset-min",
        str(int(offset_min)),
        "--offset-max",
        str(int(offset_max)),
    ]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(nokov_source):
        args.extend(["--nokov-source", nokov_source.strip()])
    return run_cli(args)


def export_rrd(
    session_dir: str,
    segment: str,
    mode: str,
    ratio: str,
    offset: int,
    save_path: str,
    use_proxy: bool,
    display: bool,
    no_mano_mesh: bool,
    gt_dir: str,
    selected_gt_files: list[str] | None,
    retarget_model: str,
    include_third_person: bool,
    third_person_video: str,
    include_robowrist: bool,
    mano_model_dir: str,
    proxy_height: int,
) -> str:
    args = ["export", session_path(session_dir), "--mode", mode]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if mode == "frame":
        args.extend(["--ratio", ratio.strip() or "auto", "--offset", str(int(offset))])
    if optional_text(save_path):
        args.extend(["--save", save_path.strip()])
    if optional_text(gt_dir):
        args.extend(["--gt-dir", gt_dir.strip()])
    for gt_file in selected_gt_files or []:
        args.extend(["--gt-file", gt_file])
    if include_third_person and optional_text(third_person_video):
        args.extend(["--gt-third-person-video", third_person_video.strip()])
    if use_proxy:
        args.append("--use-proxy")
    if display:
        args.append("--display")
    if no_mano_mesh:
        args.append("--no-mano-mesh")
    args.extend(["--retarget-model", retarget_model])
    if not include_robowrist:
        args.append("--no-robowrist")
    if optional_text(mano_model_dir):
        args.extend(["--mano-model-dir", mano_model_dir.strip()])
    args.extend(["--proxy-height", str(int(proxy_height))])
    return run_cli(args)


def language_values(language: str) -> dict[str, str]:
    return LANGUAGE_PACKS.get(language, LANGUAGE_PACKS["English"])


def language_updates(language: str):
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError('Web UI requires Gradio. Install it with: uv pip install -e ".[web]"') from exc

    labels = language_values(language)
    return [
        gr.update(value=labels["title"]),
        gr.update(label=labels["language"]),
        gr.update(label=labels["session"]),
        gr.update(label=labels["segment"]),
        gr.update(label=labels["output"]),
        gr.update(value=labels["inspect_button"]),
        gr.update(label=labels["package_output"]),
        gr.update(label=labels["package_height"]),
        gr.update(label=labels["package_crf"]),
        gr.update(value=labels["package_button"]),
        gr.update(label=labels["mode"]),
        gr.update(label=labels["ratio"]),
        gr.update(label=labels["offset"]),
        gr.update(label=labels["save_path"]),
        gr.update(label=labels["mano_model_dir"]),
        gr.update(label=labels["use_proxy"]),
        gr.update(label=labels["display"]),
        gr.update(label=labels["no_mano_mesh"]),
        gr.update(value=labels["scan_button"]),
        gr.update(label=labels["gt_dir"]),
        gr.update(label=labels["gt_files"]),
        gr.update(label=labels["retarget_model"]),
        gr.update(label=labels["include_third_person"]),
        gr.update(label=labels["third_person_video"]),
        gr.update(label=labels["include_robowrist"]),
        gr.update(label=labels["export_height"]),
        gr.update(value=labels["export_button"]),
        gr.update(label=labels["nokov_source"]),
        gr.update(value=labels["offset_button"]),
        gr.update(label=labels["offset_min"]),
        gr.update(label=labels["offset_max"]),
        gr.update(value=labels["sweep_button"]),
        gr.update(value=labels["doc"]),
    ]


def build_app():
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError('Web UI requires Gradio. Install it with: uv pip install -e ".[web]"') from exc

    labels = language_values("中文")
    with gr.Blocks(title="Robocap Rerun Tools") as app:
        title = gr.Markdown(labels["title"])
        with gr.Row():
            language = gr.Radio(label=labels["language"], choices=["中文", "English"], value="中文", scale=1)
            session_dir = gr.Textbox(label=labels["session"], placeholder=r"Z:\DATASETS\Frodobots\nokov\20260707_083023_session48", scale=3)
            segment = gr.Textbox(label=labels["segment"], value="segment1", scale=1)
        output = gr.Textbox(label=labels["output"], lines=16)

        with gr.Tab("检查 / Inspect"):
            inspect_button = gr.Button(labels["inspect_button"], variant="primary")
            inspect_button.click(inspect_session, inputs=[session_dir, segment], outputs=output)

        with gr.Tab("打包 / Package"):
            with gr.Row():
                package_output = gr.Textbox(label=labels["package_output"], placeholder=r"D:\share\session48_segment1.zip")
                package_height = gr.Number(label=labels["package_height"], value=540, precision=0)
                package_crf = gr.Number(label=labels["package_crf"], value=28, precision=0)
            package_button = gr.Button(labels["package_button"], variant="primary")
            package_button.click(package_data, inputs=[session_dir, segment, package_output, package_height, package_crf], outputs=output)

        with gr.Tab("导出 RRD / Export"):
            scan_button = gr.Button(labels["scan_button"])
            with gr.Row():
                gt_dir = gr.Textbox(label=labels["gt_dir"], placeholder=r"Z:\...\test1")
                retarget_model = gr.Radio(label=labels["retarget_model"], choices=["mano", "none", "smpl", "smplh"], value="mano")
            gt_files = gr.CheckboxGroup(label=labels["gt_files"], choices=[], value=[])
            with gr.Row():
                mode = gr.Radio(label=labels["mode"], choices=["time", "frame"], value="frame")
                ratio = gr.Textbox(label=labels["ratio"], value="auto")
                offset = gr.Number(label=labels["offset"], value=40, precision=0)
            with gr.Row():
                save_path = gr.Textbox(label=labels["save_path"], placeholder=r"D:\share\session48_frame_offset40.rrd")
                mano_model_dir = gr.Textbox(label=labels["mano_model_dir"], value=r"Z:\MODELS\hand_models\mano")
            with gr.Row():
                use_proxy = gr.Checkbox(label=labels["use_proxy"], value=True)
                display = gr.Checkbox(label=labels["display"], value=False)
                no_mano_mesh = gr.Checkbox(label=labels["no_mano_mesh"], value=False)
                include_robowrist = gr.Checkbox(label=labels["include_robowrist"], value=True)
                export_height = gr.Number(label=labels["export_height"], value=540, precision=0)
            with gr.Row():
                include_third_person = gr.Checkbox(label=labels["include_third_person"], value=True)
                third_person_video = gr.Textbox(label=labels["third_person_video"], placeholder=r"Z:\...\test1\test1-1.mp4")
            scan_button.click(scan_files, inputs=[session_dir, gt_dir], outputs=[output, gt_files, gt_dir, third_person_video])
            export_button = gr.Button(labels["export_button"], variant="primary")
            export_button.click(
                export_rrd,
                inputs=[
                    session_dir,
                    segment,
                    mode,
                    ratio,
                    offset,
                    save_path,
                    use_proxy,
                    display,
                    no_mano_mesh,
                    gt_dir,
                    gt_files,
                    retarget_model,
                    include_third_person,
                    third_person_video,
                    include_robowrist,
                    mano_model_dir,
                    export_height,
                ],
                outputs=output,
            )

        with gr.Tab("Offset"):
            with gr.Row():
                offset_ratio = gr.Textbox(label=labels["ratio"], value="auto")
                single_offset = gr.Number(label=labels["offset"], value=40, precision=0)
                nokov_source = gr.Textbox(label=labels["nokov_source"], placeholder=r"Z:\...\test1\test2-hand.bvh")
            offset_button = gr.Button(labels["offset_button"], variant="primary")
            offset_button.click(inspect_offset, inputs=[session_dir, segment, offset_ratio, single_offset, nokov_source], outputs=output)
            with gr.Row():
                offset_min = gr.Number(label=labels["offset_min"], value=35, precision=0)
                offset_max = gr.Number(label=labels["offset_max"], value=45, precision=0)
            sweep_button = gr.Button(labels["sweep_button"])
            sweep_button.click(sweep_offset, inputs=[session_dir, segment, offset_ratio, offset_min, offset_max, nokov_source], outputs=output)

        with gr.Tab("文档 / Docs"):
            docs = gr.Markdown(labels["doc"])

        language.change(
            language_updates,
            inputs=[language],
            outputs=[
                title,
                language,
                session_dir,
                segment,
                output,
                inspect_button,
                package_output,
                package_height,
                package_crf,
                package_button,
                mode,
                ratio,
                offset,
                save_path,
                mano_model_dir,
                use_proxy,
                display,
                no_mano_mesh,
                scan_button,
                gt_dir,
                gt_files,
                retarget_model,
                include_third_person,
                third_person_video,
                include_robowrist,
                export_height,
                export_button,
                nokov_source,
                offset_button,
                offset_min,
                offset_max,
                sweep_button,
                docs,
            ],
        )

    return app


def main(args: argparse.Namespace) -> int:
    app = build_app()
    app.launch(server_name=args.host, server_port=args.port, inbrowser=args.open)
    return 0
