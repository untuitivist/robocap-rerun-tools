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
  test*/
    *-1.mp4
    *-Tracker0.trc
    *-LHand.trc
    *-RHand.trc
    *.bvh
    *.csv
```

Missing streams are allowed by the exporter. When a Robocap video or signal is absent, the Rerun layout uses a text placeholder instead of failing the whole export.

