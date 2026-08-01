# Alignment Model

All exported Rerun data should live on one timeline named `capture_time`.

## Time Alignment

Time alignment keeps the source frame timestamps and applies a constant shift so the first NOKOV/GT frame starts at the first Robocap reference video capture timestamp.

This is useful when source timestamps are trusted and frame rates are close enough that long-run drift should remain visible.

## Frame Alignment

Frame alignment treats video frame indices as the primary clock:

```text
video frame N -> NOKOV frame round(N * main_ratio) + offset
```

Where:

- `main_ratio` is usually `NOKOV FPS / video FPS`.
- `--ratio 8` forces the classic 240/30 mapping.
- `--ratio auto` uses measured FPS when available.
- `--offset` is an integer NOKOV frame shift for visual inspection.

If video FPS is `30.053` and NOKOV FPS is `240`, then the true ratio is about `7.986`. A fixed `8x` mapping can still look right for short clips, but the difference can accumulate as drift over long clips.

## Offset

Offset is not a time offset. It is a NOKOV frame offset applied after the ratio mapping. `--offset 40` means:

```text
video frame N -> NOKOV frame round(N * main_ratio) + 40
```

This is equivalent to shifting NOKOV by 40 frames at the NOKOV frame rate. At 240 FPS, 40 frames is about 166.67 ms.

