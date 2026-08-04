# Robocap Rerun Tools

[中文说明](README.zh-CN.md)

Robocap Rerun Tools is a small Python project for inspecting Robocap/NOKOV sessions and exporting Rerun `.rrd` files with one shared `capture_time` timeline.

It is intended for session folders shaped like the current `Z:\DATASETS\Frodobots\nokov\2026..._session...` data:

- Robocap first-person/eye/front/wrist videos and sensor CSV files.
- A `test*`, `nokov`, or other GT folder with third-person video, BVH/TRC/CSV/XRS data, camera positions, and hand trajectories.
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

For the local browser UI:

```bat
uv pip install -e ".[web]"
```

Check the CLI:

```bat
robocap-rerun --help
```

## Common Usage

Start the local browser UI:

```bat
robocap-rerun web --open
```

Or use the Windows launcher:

```bat
start_web.bat
```

Then use `http://127.0.0.1:7860` for inspect, package, offset inspection, and RRD export.
The web UI has a Chinese/English language switch and a built-in Docs tab.
The `Set as default` button beside either Offset control saves the current integer Robocap-video-frame offset,
synchronizes it across the Export and Offset tabs, and restores it after Web UI restarts. On Windows,
the setting is stored in `%LOCALAPPDATA%\robocap-rerun-tools\web_settings.json`.
On the export tab, use `Scan files` to populate the GT/NOKOV file list. You can then choose which
`.bvh`, `.trc`, `.csv`, and `.xrs` files enter the RRD, choose whether to include a third-person
video, choose whether robowrist streams are included, and choose the retarget target. MANO is
implemented; SMPL/SMPLH are currently explicit placeholders in the recording notes.
The `Environment` tab checks Python/package/tool versions, ffmpeg/ffprobe, git status, and the
local/remote commit relationship. It can also run `uv pip install -e ".[web]"` in a separate
`cmd` window, close the current web process, print update logs there, and restart through
`start_web.bat`. Source updates are done through the normal Git workflow described below.
The `Viewer` tab can scan generated `.rrd` files under the current session and open a selected file
in Rerun Web Viewer. The viewer runs in a separate `cmd` window so its logs stay visible.
Its port defaults to `0` (auto). A requested port is tested before launch; if Windows reserves it or
another process already uses it, the UI selects an available localhost port and fills in the actual
port. Rerun itself opens the connected recording URL in the default browser; the bare HTTP root only
shows Rerun's welcome page.

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

Export a frame-aligned RRD with the default automatic ratio:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --offset 0 --use-proxy
```

Export an offset-5 frame-aligned RRD. At ratio 8 this equals the old 40-GT-frame shift:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 5 --use-proxy
```

Use the display layout requested for visual checking:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 5 --use-proxy --display
```

The display layout keeps:

- Top video row: `left/right`, `left_eye/right_eye`, `left_front/right_front`.
- Robocap sensors: `middle_mag` spans both rows; left `acc/gyro` share one row and right `acc/gyro` share the other.
- Bottom row views for the GT formats that are actually present. Multiple rigid bodies from one
  XRS/CSV file are shown in the same 3D space for that format. Skeleton and mesh sources use
  separate format tabs, with the third-person video beside them. Missing formats do not create
  empty placeholder tabs.

NOKOV coordinates are treated as millimetres by default and converted to Rerun metres with a
`0.001` scale. The exporter also reads `BoneAxis`: `BoneAxis=Z` selects Y-up and `BoneAxis=Y`
selects Z-up. All rigid bodies from one CSV/XRS source keep their shared world coordinate frame.

Use `--no-mag` or `--no-imu` to exclude those sensor groups from logging and the Rerun layout. The
Web export tab exposes the same controls as checkboxes.

## Offset Inspection

Write a single mapping table:

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --offset 5
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
video frame N -> NOKOV frame round((N + offset) * main_ratio)
```

Frame alignment defaults to `--ratio auto`. It reads the table in
`_artifacts/<segment>/inspection/frame_rate_report.md`, averages all valid GT motion-data FPS values
and all Robocap video FPS values separately, rounds each mean to the nearest multiple of 10, and
uses `rounded GT FPS / rounded Robocap FPS`. If the report is missing or invalid, the tool generates
it before calculating the ratio. The report includes the sample counts, raw means, rounded values,
and resulting ratio. Use a numeric value such as `--ratio 8` to override auto.

Offset is an integer Robocap video-frame shift applied before the ratio. At ratio 8, `--offset 5`
adds 40 GT frames. This replaces the historical GT-frame-unit `--offset 40` convention.

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

The MANO retarget maps NOKOV `Finger1/2/3` to MANO MCP/PIP/DIP joints and uses `Finger4/End`
for fingertip direction. It estimates one robust hand scale from corresponding bone lengths,
fits the wrist orientation from palm MCP directions, then applies hierarchical joint rotations
through linear blend skinning for every frame.

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
