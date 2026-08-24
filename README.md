# Robocap Rerun Tools

[中文文档](README.zh-CN.md)

Robocap Rerun Tools inspects and aligns Robocap, NOKOV motion capture, third-person video, and
optional robowrist streams, then exports synchronized Rerun `.rrd` recordings. Time-aligned exports
use `capture_time` as the primary timeline; frame-aligned exports use the integer `frame` timeline.

## Capabilities

- Inspect FPS, frame/sample counts, adjacent timestamp intervals, and inferred dropped frames across
  videos, motion capture, MAG, IMU, and robowrist data.
- Generate one self-contained `timestamp_anomaly_detail_table.html` that can be opened and shared
  offline.
- Export time-aligned or frame-aligned RRD files with automatic integer FPS ratio, signed Robocap
  frame offset, optional dropped-frame interpolation, and selectable frame ranges.
- Load every selected BVH/TRC/CSV/XRS source, including multiple bodies and rigid bodies in one 3D
  world, without creating empty placeholder views.
- Include selectable third-person video, Robocap video/sensors, robowrist, MAG, and IMU streams.
- Package sessions with compressed video and stage/upload the documented ModelScope dataset layout.
- Open generated HTML reports and RRD recordings directly from the Web UI.
- Scan a collection root recursively and select any detected session from one searchable dropdown.

## Session Layout

The tool targets session folders such as
`Z:\DATASETS\Frodobots\nokov\2026..._session...` containing:

- Robocap first-person, eye, front, and optional wrist videos plus sensor files.
- A canonical `mocap/` directory, or a legacy `test*/`/other GT directory, containing third-person
  video and BVH/TRC/CSV/XRS motion-capture exports.
- Optional `robowrist_<device_id>_<side>/` directories.
- Optional MANO model files for advanced CLI hand-mesh retargeting.

Missing streams are omitted from the Rerun blueprint instead of becoming text placeholders. If both
Robocap MAG and IMU are absent, the complete sensor row is omitted.

## Quick Start On Windows

The only runtime prerequisite is [uv](https://docs.astral.sh/uv/getting-started/installation/).
Git is needed only to clone or update the repository. Clone over HTTPS, then run the launcher:

```bat
git clone https://github.com/untuitivist/robocap-rerun-tools.git
cd robocap-rerun-tools
start_web.bat
```

`start_web.bat` runs `uv sync --extra web`, creates `.venv`, downloads a compatible Python 3.11+
interpreter when needed, installs all Python dependencies, and opens the Web UI at
`http://127.0.0.1:7860`. The first sync also downloads the approximately 87 MiB
`ffmpeg-binaries-compat` wheel containing FFmpeg and FFprobe. No system Python, FFmpeg installation,
administrator access, virtual-environment activation, or `PATH` editing is required.

The bundled binaries are preferred for reproducible processing. A complete system FFmpeg/FFprobe
pair is only used as a fallback on platforms without a bundled wheel. Current bundled wheels cover
Windows x64, Linux x64, and macOS universal.

## CLI And Development

For CLI-only use:

```bat
uv sync
uv run robocap-rerun --help
```

Start the Web UI without the launcher:

```bat
uv run --extra web robocap-rerun web --open
```

For development and tests, include both extras:

```bat
uv sync --extra web --extra dev
uv run python -m pytest -q
```

The command examples below use `robocap-rerun` directly. Either activate once with
`.venv\Scripts\activate.bat` or prefix a command with `uv run`.

## Web UI

The bilingual Web UI provides inspection, packaging, time/frame RRD export, offset inspection,
ModelScope staging/upload, report opening, RRD viewing, environment checks, and code updates.

At the top of the page, enter the directory that contains the recording collection and click
`Scan sessions`. The scanner finds direct children and nested layouts such as
`EgoMotionActions/PXX/<session_id>`, then fills a searchable Session dropdown with relative labels.
A session is recognized when its root directly contains at least one `robocap_*` source file.
Generated analysis/artifact/dataset trees, calibration data, virtual environments, and build trees
are excluded. The collection root and last selected Session are restored after a Web UI restart.
Changing Session clears file selections derived from the previous Session before any new operation.

CLI-backed actions stream combined stdout/stderr into the Output box about twice per second. The
box shows status, elapsed time, recent logs, and either parsed `[n/total]`/percentage progress or an
animated unknown-total bar. Carriage-return updates from tqdm are handled without waiting for the
command to exit. Logs retain the latest 1000 lines or approximately 256 KiB to bound Web memory.

Inspection writes only `timestamp_anomaly_detail_table.html`, with all data, styles, and JavaScript
embedded for offline sharing. The `Reports` tab scans these files and opens the selected report in
the default browser; the output box prints the generated path instead of duplicating the report.

The `Set as default` button beside either Offset control saves the current integer Robocap-video-frame offset,
synchronizes it across the Export and Offset tabs, and restores it after Web UI restarts. On Windows,
the setting is stored in `%LOCALAPPDATA%\robocap-rerun-tools\web_settings.json`.

On the export tab, use `Scan files` to populate the GT/NOKOV file list. You can then choose which
`.bvh`, `.trc`, `.csv`, and `.xrs` files enter the RRD, choose whether to include a third-person
video, and choose whether robowrist, MAG, and IMU streams are included. The Web exporter records
skeletons and rigid bodies without model retargeting; advanced retargeting remains available from
the CLI. Scanning also detects standard robowrist video and sensor streams. If none exist, the
robowrist checkbox is cleared and disabled, and the export is named with `rw0`. The `Environment`
tab checks Python/package/tool versions, ffmpeg/ffprobe, and Git repository state. It shows branch,
commit, HTTPS origin, upstream, local changes, and ahead/behind counts. `Check code updates` fetches
`origin`. If fetching fails, the remote status is reported as unknown instead of treating a cached
`origin/master` as current. Untracked local files are listed separately and do not by themselves
mean the local commit differs from GitHub. `Update code and restart` requires a clean working tree
and runs `git pull --ff-only`.
Code and dependency updates use a separate `cmd` window, stop Web only after preflight, print logs,
run `uv sync --extra web`, and restart through `start_web.bat`. Local changes are never stashed or
overwritten.

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

## ModelScope Dataset Publishing

The tool publishes direct dataset files rather than a ZIP-only sample. Each prepared recording uses:

```text
<dataset_root>/
  README.md                              # required: ModelScope Dataset Card
  metadata.jsonl                         # required: one row per session
  raw_calibration/                       # required: maintained by calibration workflow
    <device_id>/                         # files are resolved by explicit device ID
  EgoMotionActions/                      # generated action data
    PXX/                                 # P01-P29 action primitive
      <session_id>/
        robocap_<segment>_video_*.mp4       # required: six first-person cameras
        robocap_<segment>_imu_*.db          # required: Robocap IMU
        robocap_<segment>_mag_*.db          # required: Robocap MAG
        mocap/
          *.{bvh,trc,csv,xrs,c3d,...}       # [optional format]: at least one is required
                                            # include all bodies and rigid bodies in each format
          *.mp4                             # required: third-person video(s)
        robowrist_<device_id>_<side>/       # required: left/right video, IMU, and MAG
        rerun/<segment>/inspection/*.rrd    # [optional]: only when explicitly selected
        manifest.json                       # generated; includes device IDs
        timestamp_anomaly_detail_table.html # generated and required
```

The capture streams and motion-capture content are required. Only the concrete NOKOV export
format(s) and RRD files are optional; at least one motion-capture format must be present.

Raw calibration is outside every action session and is stored once at
`<dataset_root>/raw_calibration/<device_id>/`. The session staging command does not copy a source
session's local `raw_calibration/`; that root collection is maintained separately. Session
`manifest.json` and `metadata.jsonl` reference it only with `device_ids: {main, left, right}`.
Local paths, API responses, and signed URLs are never packaged.

Copy `.env.example` to `.env`, or save the token from the Web `ModelScope` tab. The local file is
ignored by Git and excluded from every data package:

```dotenv
MODELSCOPE_API_TOKEN=
MODELSCOPE_ENDPOINT=https://modelscope.cn
MODELSCOPE_REPO_ID=owner/egomocap
```

The saved token is never added to a CLI argument or printed. Verify it with:

```bat
robocap-rerun modelscope-auth
```

Prepare one session. The staging root is automatically `<session parent>/_modelscope_dataset`; this
regenerates the inspection HTML, compresses video by default, removes local absolute paths from the
copied report, and updates the dataset-level metadata:

```bat
robocap-rerun modelscope-stage Z:\DATASETS\Frodobots\nokov\20260803_081935_session39 --primitive-id P01 --segment segment1 --refresh-inspection
```

The Web tab scans the selected Segment and lets each RRD file be selected independently. The CLI
equivalent is a repeatable `--rrd-file` option with either a Session-relative or absolute path;
`--include-rrd` remains available when every RRD in the Segment should be staged. Preparing the
same Session again synchronizes its generated `rerun/` directory with the current selection:

```bat
robocap-rerun modelscope-stage Z:\DATASETS\Frodobots\nokov\20260803_081935_session39 --primitive-id P01 --segment segment1 --rrd-file _artifacts\segment1\inspection\frame.rrd
```

Upload every session referenced by the prepared `metadata.jsonl`. The resumable cache skips files
that have not changed:

```bat
robocap-rerun modelscope-upload Z:\DATASETS\Frodobots\nokov\_modelscope_dataset
```

The command uses `MODELSCOPE_REPO_ID` from `.env`. Pass `--repo-id owner/another-dataset` only to
override the saved repository for one upload.

Add `--create-if-missing --visibility private` only when the tool should create a missing dataset
repository from the CLI. Uploads use the official `modelscope-hub` resumable cache by default. The
Web tab always uses the compressed-video defaults and requires an existing repository; it does not
expose raw-video, proxy encoding, repository creation, visibility, or license controls.

Inspect one session before exporting:

```bat
robocap-rerun inspect Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1
```

Inspection includes Robocap/robowrist videos, third-person videos, GT motion files, and timestamped
tables in IMU/MAG SQLite databases. ACC, gyro, and MAG tables are reported as separate streams.
Video `fps` is the full-stream average reported by ffprobe, while median/min/max interval and abnormal
counts come from the actual frame timestamps. When an MP4 has a numeric `comment` capture timestamp,
its start/end values are placed on that capture-time axis; otherwise the report marks them as media-relative.
Timestamp anomaly thresholds are fixed at 30 FPS for video and 240 FPS for motion capture. Thus normal
integer-millisecond diffs are 33/34 ms and 4/5 ms. Diffs are computed only for adjacent rows whose two
timestamps are valid; missing rows are listed but never bridged. The frame-count baseline is
`mocap = 8*(n+1)` and `third-person video = n+1` for `n` Robocap frames.

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

Optionally reconstruct short dropped-frame gaps in NOKOV/GT trajectories before alignment:

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --interpolate-dropped-frames --use-proxy
```

This uses the fixed NOKOV source rate of 240 FPS. Normal integer timestamp jitter of 4/5 ms is
kept unchanged; an approximately 8 ms gap inserts one linearly interpolated pose, 12 ms inserts
two, and so on. Gaps longer than one second are treated as capture discontinuities. This option
only reconstructs GT trajectory samples and never synthesizes video frames or sensor readings.
Equivalent BVH/CSV/XRS exports use a same-stem TRC as their authoritative sampling clock when
available, even if that TRC is not selected for display, because CSV placeholder rows may contain
zero timestamps.
In Rerun, an interpolated skeleton/rigid-body frame is rendered in solid red and receives an
always-visible label containing its 0-based GT source frame and, in frame mode, its `frame` timeline
index. The next original frame restores the configured point/line colors and clears the label.

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

Every RRD uses one fixed layout:

- Top video grid: Robocap views use four columns; detected robowrist videos are included when
  enabled.
- Middle sensor section: one multi-row, one-column grid. Its first row is the complete Robocap
  sensor block; optional second and third rows contain the left and right wrist MAG/IMU streams.
  Missing rows are omitted. Inside the Robocap block, `middle_mag` spans both IMU rows; left
  `acc/gyro` share one row and right `acc/gyro` share the other.
- Bottom row views for the GT formats that are actually present. Multiple rigid bodies from one
  XRS/CSV file are shown in the same 3D space for that format. Skeleton format views are arranged
  from left to right; mesh sources and third-person video remain beside them. Missing
  formats do not create empty placeholder views.

NOKOV coordinates are treated as millimetres by default and converted to Rerun metres with a
`0.001` scale. The exporter also reads `BoneAxis`: `BoneAxis=Z` selects Y-up and `BoneAxis=Y`
selects Z-up. All rigid bodies from one CSV/XRS source keep their shared world coordinate frame.

Use `--no-mag` or `--no-imu` to exclude those sensor groups from logging and the Rerun layout. The
Web export tab exposes the same controls as checkboxes. Wrist stream discovery first checks the
standard robowrist folders and then recursively finds matching segment files, so an extra packaging
directory level does not hide left/right wrist MAG databases.

## Offset Inspection

Write a single mapping table:

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --offset 5
```

Specify the exact NOKOV BVH/TRC reference stream when the folder contains multiple skeleton tracks:

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio auto --offset 0 --nokov-source Z:\DATASETS\Frodobots\nokov\20260707_083023_session48\mocap\test2-hand.bvh
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

Frame alignment defaults to `--ratio auto`. It scans the current session, averages all valid GT
motion-data FPS values and all Robocap video FPS values separately, rounds each mean to the nearest
multiple of 10, and calculates `rounded GT FPS / rounded Robocap FPS`. That quotient is rounded again
to the nearest positive integer and becomes the actual auto ratio. Use a numeric value such as
`--ratio 8` to override auto. No inspection report is read or generated by this alignment scan.

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
offset, and ModelScope commands all wait for the underlying command to finish while streaming their
progress and logs. Environment checks remain immediate in-process operations.

Default outputs are written under:

```text
<session>/_artifacts/<segment>/inspection/
```

Typical files:

- `*_time_aligned_fall_interp0_rt-none_raw_data-..._cfg-....rrd`
- `*_frame_aligned_r8_o5_ref-left_f100-500_interp1_rt-none_p540_data-..._cfg-....rrd`
- `time_alignment_report.tsv`
- `timestamp_anomaly_detail_table.html`
- `video_to_nokov_frame_alignment.tsv`
- `offset_inspection.md`

Generated `.rrd`, videos, raw captures, and MANO model files are ignored by Git.

RRD names are parameterized to prevent accidental overwrites. Readable tags include frame-mode
ratio (`r`), Robocap-frame offset (`o`), reference video (`ref`), frame range (`f`, or `fall`),
interpolation (`interp`), retarget model (`rt`), raw/proxy video, and stream switches. The final
stable `cfg-<10 hex>` fingerprint also covers exact proxy settings, sensor limits, trim/alignment options,
coordinate scales, GT inputs, MANO directory, and other content-affecting export arguments.
An explicit `--save` path receives the same suffix; supplying the already parameterized result does
not duplicate it.

## Update An Existing Clone

Pull code with fast-forward-only policy, then run the launcher. The launcher synchronizes changed
dependencies automatically:

```bat
git pull --ff-only
start_web.bat
```

The Web UI offers the same clean-worktree update flow in its `Environment` tab. It never stashes or
overwrites local changes.

## Migrate A Legacy Motion-Capture Directory

To migrate a collection whose session child directories still use the previous motion-capture name,
preview the changes first and then apply them:

```bat
uv run python scripts\migrate_mocap_layout.py Z:\DATASETS\Frodobots\nokov --rewrite-zip
uv run python scripts\migrate_mocap_layout.py Z:\DATASETS\Frodobots\nokov --rewrite-zip --apply
```

The command renames session directories, updates generated report/manifest path references in
`_analysis`, `_artifacts`, and `_modelscope_dataset`, and updates same-length path fields in
top-level ZIP archives without recompressing their data. RAR archives must be renamed with a RAR
archive tool and verified with its archive test command.

## Repository Hygiene

Keep this repository code-only. Do not commit dataset folders, generated `_artifacts`, proxy videos, `.rrd` files, or private MANO pickle files.
