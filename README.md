# Robocap Rerun Tools

[中文说明](README.zh-CN.md)

Robocap Rerun Tools is a small Python project for inspecting Robocap/NOKOV sessions and exporting Rerun `.rrd` files with one shared `capture_time` timeline.

It is intended for session folders shaped like the current `Z:\DATASETS\Frodobots\nokov\2026..._session...` data:

- Robocap first-person/eye/front/wrist videos and sensor CSV files.
- A `test*`, `nokov`, or other GT folder with third-person video, BVH/TRC/CSV/XRS data, camera positions, and hand trajectories.
- Optional MANO model files for hand mesh generation.

Missing streams are omitted from the Rerun blueprint instead of becoming text placeholders. If both
Robocap MAG and IMU are absent, the complete sensor row is omitted.

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
video, and choose whether robowrist, MAG, and IMU streams are included. The Web exporter records
skeletons and rigid bodies without model retargeting; advanced retargeting remains available from
the CLI. Scanning also detects standard robowrist video and sensor streams. If none exist, the
robowrist checkbox is cleared and disabled, and the export is named with `rw0`. The `Environment`
tab checks Python/package/tool versions and ffmpeg/ffprobe without
showing or querying Git repository, branch, remote, or version information. It can also run
`uv pip install -e ".[web]"` in a separate `cmd` window, close the current web process, print
update logs there, and restart through `start_web.bat`.
The `Viewer` tab can scan generated `.rrd` files under the current session and open a selected file
in Rerun Web Viewer. The newest RRD is selected by default. The viewer runs in a separate `cmd`
window so its logs stay visible.
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

Inspection includes Robocap/robowrist videos, third-person videos, GT motion files, and timestamped
tables in IMU/MAG SQLite databases. ACC, gyro, and MAG tables are reported as separate streams.
Video `fps` is the full-stream average reported by ffprobe, while median/min/max interval and abnormal
counts come from the actual frame timestamps. When an MP4 has a numeric `comment` capture timestamp,
its start/end values are placed on that capture-time axis; otherwise the report marks them as media-relative.

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

Export only Robocap reference-video frames 100 through 500. Frame indexes are 0-based and both
endpoints are included:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 5 --robocap-start-frame 100 --robocap-end-frame 500 --use-proxy
```

The two range arguments must be supplied together. The exporter converts those reference-video
frames to one `capture_time` window and applies it to every Robocap video/sensor, NOKOV track, and
third-person stream. It then intersects that range with the normal common-data window. In the Web
UI, enable `Limit Robocap frame range` before entering the two frame indexes; leave it disabled for
the full recording.

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
GT frame offset = round(Robocap frame offset * main_ratio)
video frame N -> NOKOV frame round(N * main_ratio) + GT frame offset
```

Frame alignment defaults to `--ratio auto`. It reads the table in
`_artifacts/<segment>/inspection/frame_rate_report.md`, averages all valid GT motion-data FPS values
and all Robocap video FPS values separately, rounds each mean to the nearest multiple of 10, and
calculates `rounded GT FPS / rounded Robocap FPS`. That quotient is rounded again to the nearest
positive integer and becomes the actual auto ratio. If the report is missing or invalid, the tool
generates it before calculating the ratio. The report includes the sample counts, raw means,
rounded FPS values, quotient before final rounding, and resulting integer ratio. Use a numeric value
such as `--ratio 8` to override auto.

Offset is measured relative to Robocap video. Positive values advance NOKOV/GT relative to the
video, so GT appears earlier and the same video frame selects a later GT frame. Negative values
delay NOKOV/GT relative to the video, so GT appears later and the same video frame selects an
earlier GT frame. The exporter converts this value to `round(offset * ratio)` GT frames and then
uses the source script's alignment formula unchanged. At ratio 8, `--offset 5` becomes GT offset
`40`, while `--offset -5` places GT frame 0 at Robocap video frame 5.

Frame-aligned RRD files use `frame` as the primary timeline, expressed at the GT/NOKOV frame
rate. Robocap video frame `N` is logged at `frame = round(N * ratio)`, and source GT frame `K`
is logged at `frame = K - round(offset * ratio)`. All videos and sensor samples are mapped onto
that same integer frame axis from the reference video's timestamps. `capture_time` is retained as
a secondary timeline for inspection; it is not the default timeline in frame mode.

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

The MANO retarget follows the source script's naming convention: BVH `Finger0/1/2` and
TRC/CSV/XRS `Finger1/2/3` map to MANO MCP/PIP/DIP joints. It normalizes the MANO template,
initializes each frame from the wrist origin, palm basis, and hand scale, replaces the mapped joint
targets with the measured NOKOV positions, and applies linear blend skinning.

To export skeletons without a retargeted mesh from the CLI, pass the `none` retarget model. The
Rerun blueprint then omits the mesh view instead of adding a no-data text placeholder. Web exports
use this mode automatically and do not show model controls:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --retarget-model none
```

## Outputs

Commands launched from the Web UI do not set a process timeout. RRD export, inspection, packaging,
offset, and environment commands all wait for the underlying command to finish.

Default outputs are written under:

```text
<session>/_artifacts/<segment>/inspection/
```

Typical files:

- `*_time_aligned_fall_rt-none_raw_bp-default_data-..._cfg-....rrd`
- `*_frame_aligned_r8_o5_ref-left_f100-500_rt-none_p540_bp-display_data-..._cfg-....rrd`
- `time_alignment_report.tsv`
- `frame_rate_report.md`
- `frame_rate_report.tsv`
- `video_to_nokov_frame_alignment.tsv`
- `offset_inspection.md`

Generated `.rrd`, videos, raw captures, and MANO model files are ignored by Git.

RRD names are parameterized to prevent accidental overwrites. Readable tags include frame-mode
ratio (`r`), Robocap-frame offset (`o`), reference video (`ref`), frame range (`f`, or `fall`),
retarget model (`rt`), raw/proxy video, blueprint (`bp`), and stream switches. The final stable
`cfg-<10 hex>` fingerprint also covers exact proxy settings, sensor limits, trim/alignment options,
coordinate scales, GT inputs, MANO directory, and other content-affecting export arguments.
An explicit `--save` path receives the same suffix; supplying the already parameterized result does
not duplicate it.

## Notes For GitHub

Keep this repository code-only. Do not commit dataset folders, generated `_artifacts`, proxy videos, `.rrd` files, or private MANO pickle files.
