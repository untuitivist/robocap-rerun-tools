# Alignment Model

All exported Rerun data should live on one timeline named `capture_time`.

## Time Alignment

Time alignment keeps the source frame timestamps and applies a constant shift so the first NOKOV/GT frame starts at the first Robocap reference video capture timestamp.

This is useful when source timestamps are trusted and frame rates are close enough that long-run drift should remain visible.

## Frame Alignment

Frame alignment treats video frame indices as the primary clock:

```text
video frame N -> NOKOV frame round((N + offset) * main_ratio)
```

Where:

- `main_ratio` defaults to the nominal GT/Robocap FPS ratio computed from the inspection report.
- `--ratio 8` forces the classic 240/30 mapping.
- `--ratio auto` is the default.
- `--offset` is an integer Robocap video-frame shift for visual inspection.

Auto reads `_artifacts/<segment>/inspection/frame_rate_report.md`. It computes the arithmetic mean
of all valid GT motion-data FPS samples and the arithmetic mean of all Robocap video FPS samples,
rounds both means to the nearest multiple of 10, then divides the rounded GT value by the rounded
Robocap value. For example, `59.04 -> 60` and `28.60 -> 30`, so the ratio is `2`. The same rule maps
approximately 240 FPS GT and 30 FPS Robocap data to `8`. The inspection report records every input
and the resulting calculation. A numeric `--ratio` bypasses auto.

## Offset

Offset is not a time offset. It is a Robocap video-frame offset applied before the ratio mapping.
`--offset 5` means:

```text
video frame N -> NOKOV frame round((N + 5) * main_ratio)
```

At ratio 8 this is equivalent to shifting GT/NOKOV by 40 frames. With 30 FPS Robocap and 240 FPS
NOKOV, both describe about 166.67 ms. Existing Web settings without an offset-unit marker are
migrated from the historical GT-frame convention by dividing by 8, so the old default 40 becomes 5.
