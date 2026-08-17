# Expected Session Layout

The tools are designed around this loose layout:

```text
session/
  robocap_segment1_video_left.mp4
  robocap_segment1_video_right.mp4
  robocap_segment1_video_left_eye.mp4
  robocap_segment1_video_right_eye.mp4
  robocap_segment1_video_left_front.mp4
  robocap_segment1_video_right_front.mp4
  robowrist_*_left/
    robowrist_segment1_video_left_down.mp4
    *.csv
  robowrist_*_right/
    robowrist_segment1_video_right_down.mp4
    *.csv
  mocap/ or test*/
    *-1.mp4
    *-Tracker0.trc
    *-LHand.trc
    *-RHand.trc
    *.bvh
    *.csv
    *.xrs
```

GT directory discovery recognizes the canonical `mocap/` directory, `test*`, or a single other directory containing
`.bvh`, `.trc`, `.csv`, or `.xrs` files. `robowrist_*` and `_artifacts` are excluded from GT
discovery. Missing streams are allowed by the exporter. When a Robocap video or signal is absent,
the Rerun layout uses a text placeholder instead of failing the whole export.

Formats with the same filename stem are grouped as one capture, but each available format gets
its own skeleton and mesh tab. CSV/XRS rigid bodies remain in their common NOKOV world frame.
NOKOV positions default to millimetres and are converted to metres with scale `0.001`.
`BoneAxis=Z` is displayed as Y-up and `BoneAxis=Y` as Z-up.
