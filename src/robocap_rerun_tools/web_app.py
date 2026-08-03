from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


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
    if use_proxy:
        args.append("--use-proxy")
    if display:
        args.append("--display")
    if no_mano_mesh:
        args.append("--no-mano-mesh")
    if optional_text(mano_model_dir):
        args.extend(["--mano-model-dir", mano_model_dir.strip()])
    args.extend(["--proxy-height", str(int(proxy_height))])
    return run_cli(args)


def build_app():
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError('Web UI requires Gradio. Install it with: uv pip install -e ".[web]"') from exc

    with gr.Blocks(title="Robocap Rerun Tools") as app:
        gr.Markdown("# Robocap Rerun Tools")
        with gr.Row():
            session_dir = gr.Textbox(label="Session directory", placeholder=r"Z:\DATASETS\Frodobots\nokov\20260707_083023_session48", scale=3)
            segment = gr.Textbox(label="Segment", value="segment1", scale=1)
        output = gr.Textbox(label="Output", lines=16)

        with gr.Tab("Inspect"):
            inspect_button = gr.Button("Inspect", variant="primary")
            inspect_button.click(inspect_session, inputs=[session_dir, segment], outputs=output)

        with gr.Tab("Package Data"):
            with gr.Row():
                package_output = gr.Textbox(label="Output zip", placeholder=r"D:\share\session48_segment1.zip")
                package_height = gr.Number(label="Proxy height", value=540, precision=0)
                package_crf = gr.Number(label="Proxy CRF", value=28, precision=0)
            package_button = gr.Button("Package", variant="primary")
            package_button.click(package_data, inputs=[session_dir, segment, package_output, package_height, package_crf], outputs=output)

        with gr.Tab("Export RRD"):
            with gr.Row():
                mode = gr.Radio(label="Alignment mode", choices=["time", "frame"], value="frame")
                ratio = gr.Textbox(label="Ratio", value="auto")
                offset = gr.Number(label="Offset", value=40, precision=0)
            with gr.Row():
                save_path = gr.Textbox(label="Save path", placeholder=r"D:\share\session48_frame_offset40.rrd")
                mano_model_dir = gr.Textbox(label="MANO model dir", value=r"Z:\MODELS\hand_models\mano")
            with gr.Row():
                use_proxy = gr.Checkbox(label="Use compressed proxy video", value=True)
                display = gr.Checkbox(label="Display layout", value=False)
                no_mano_mesh = gr.Checkbox(label="Disable MANO mesh", value=False)
                export_height = gr.Number(label="Proxy height", value=540, precision=0)
            export_button = gr.Button("Export RRD", variant="primary")
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
                    mano_model_dir,
                    export_height,
                ],
                outputs=output,
            )

        with gr.Tab("Offset"):
            with gr.Row():
                offset_ratio = gr.Textbox(label="Ratio", value="auto")
                single_offset = gr.Number(label="Offset", value=40, precision=0)
                nokov_source = gr.Textbox(label="NOKOV source", placeholder=r"Z:\...\test1\test2-hand.bvh")
            offset_button = gr.Button("Inspect Offset", variant="primary")
            offset_button.click(inspect_offset, inputs=[session_dir, segment, offset_ratio, single_offset, nokov_source], outputs=output)
            with gr.Row():
                offset_min = gr.Number(label="Offset min", value=35, precision=0)
                offset_max = gr.Number(label="Offset max", value=45, precision=0)
            sweep_button = gr.Button("Sweep Offset")
            sweep_button.click(sweep_offset, inputs=[session_dir, segment, offset_ratio, offset_min, offset_max, nokov_source], outputs=output)

    return app


def main(args: argparse.Namespace) -> int:
    app = build_app()
    app.launch(server_name=args.host, server_port=args.port, inbrowser=args.open)
    return 0

