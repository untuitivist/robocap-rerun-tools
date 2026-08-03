# Robocap Rerun Tools

[中文说明](README.zh-CN.md)

Robocap Rerun Tools is a small Python project for inspecting Robocap/NOKOV sessions and exporting Rerun `.rrd` files with one shared `capture_time` timeline.

It is intended for session folders shaped like the current `Z:\DATASETS\Frodobots\nokov\2026..._session...` data:

- Robocap first-person/eye/front/wrist videos and sensor CSV files.
- A `test*` NOKOV folder with third-person video, BVH/TRC/CSV skeleton data, camera positions, and hand trajectories.
- Optional MANO model files for hand mesh generation.

## Install With uv

Install external tools first:

- `uv`
- `ffmpeg` and `ffprobe` on `PATH`
- A Python 3.11 interpreter visible to `uv`

Then create the local virtual environment:

```bat
cd /d Z:\DATASETS\Frodobots\robocap-rerun-tools
uv venv .venv --python 3.11
.venv\Scripts\activate.bat
uv pip install -e .
```

For development tools:

```bat
uv pip install -e ".[dev]"
```

Check the CLI:

```bat
robocap-rerun --help
```

## Common Usage

Package one session for sharing. Videos are compressed by default:

```bat
robocap-rerun package-data Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1
```

Inspect one session before exporting:

```bat
robocap-rerun inspect Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1
```

Export a time-aligned RRD:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode time --use-proxy
```

Export a frame-aligned RRD with automatic main ratio:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio auto --offset 0 --use-proxy
```

Export an offset-40 frame-aligned RRD:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 40 --use-proxy
```

Use the display layout requested for visual checking:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 40 --use-proxy --display
```

The display layout keeps:

- Top video row: `left/right`, `left_eye/right_eye`, `left_front/right_front`.
- Robocap sensors only.
- Bottom row tabs for BVH, CSV, and TRC skeleton/mesh views.

## Offset Inspection

Write a single mapping table:

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio 8 --offset 40
```

Specify the exact NOKOV BVH/TRC reference stream when the folder contains multiple skeleton tracks:

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio auto --offset 0 --nokov-source Z:\DATASETS\Frodobots\nokov\20260707_083023_session48\test1\test2-hand.bvh
```

Sweep a candidate range:

```bat
robocap-rerun sweep-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio 8 --offset-min -20 --offset-max 80
```

The mapping used by frame alignment is:

```text
video frame N -> NOKOV frame round(N * main_ratio) + offset
```

For the historical 30 FPS video and 240 FPS NOKOV case, `main_ratio = 8`. For true inspection, use `--ratio auto`; the tool computes `NOKOV FPS / video FPS` when both rates are discoverable.

## MANO Mesh

By default the exporter looks for:

```text
Z:\MODELS\hand_models\mano\MANO_LEFT.pkl
Z:\MODELS\hand_models\mano\MANO_RIGHT.pkl
```

Override it when needed:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --mano-model-dir Z:\MODELS\hand_models\mano
```

If a session should still export without MANO mesh:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --no-mano-mesh
```

## Outputs

Default outputs are written under:

```text
<session>/_artifacts/<segment>/inspection/
```

Typical files:

- `*_time_aligned.rrd`
- `*_frame_aligned.rrd`
- `time_alignment_report.tsv`
- `frame_rate_report.md`
- `frame_rate_report.tsv`
- `video_to_nokov_frame_alignment.tsv`
- `offset_inspection.md`

Generated `.rrd`, videos, raw captures, and MANO model files are ignored by Git.

## Notes For GitHub

Keep this repository code-only. Do not commit dataset folders, generated `_artifacts`, proxy videos, `.rrd` files, or private MANO pickle files.
