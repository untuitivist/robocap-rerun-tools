from __future__ import annotations

import math
from dataclasses import dataclass


def round_positive_ratio(value: float) -> int:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Ratio must be finite and positive, got {value!r}.")
    return max(1, math.floor(value + 0.5))


@dataclass(frozen=True)
class FrameAlignment:
    """Map Mocap and third-person source frames onto a Robocap reference.

    ``video_frame_offset`` is retained as the storage name for compatibility. It is the
    Mocap offset measured in Robocap frames, not an offset shared by every video source.
    ``third_person_video_frame_offset`` is an independent 30 FPS source-frame offset.
    """

    ratio: float
    video_frame_offset: int = 0
    third_person_video_frame_offset: int = 0

    def __post_init__(self) -> None:
        ratio = float(self.ratio)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("Frame alignment ratio must be finite and positive.")
        for value, label in (
            (self.video_frame_offset, "Mocap Robocap-frame offset"),
            (self.third_person_video_frame_offset, "Third-person Robocap-frame offset"),
        ):
            if isinstance(value, bool) or int(value) != value:
                raise ValueError(f"{label} must be an integer.")
        object.__setattr__(self, "ratio", ratio)
        object.__setattr__(self, "video_frame_offset", int(self.video_frame_offset))
        object.__setattr__(
            self,
            "third_person_video_frame_offset",
            int(self.third_person_video_frame_offset),
        )

    @property
    def mocap_source_frame_offset(self) -> int:
        return round(self.video_frame_offset * self.ratio)

    @property
    def gt_frame_offset(self) -> int:
        """Compatibility alias for the offset expressed in Mocap/GT source frames."""
        return self.mocap_source_frame_offset

    def mocap_to_robocap_frame_float(self, mocap_frame: int) -> float:
        return (mocap_frame - self.mocap_source_frame_offset) / self.ratio

    def robocap_to_mocap_frame_float(self, robocap_frame: int) -> float:
        return robocap_frame * self.ratio + self.mocap_source_frame_offset

    def robocap_to_mocap_frame(self, robocap_frame: int) -> int:
        return round(robocap_frame * self.ratio) + self.mocap_source_frame_offset

    def robocap_to_third_person_frame(self, robocap_frame: int) -> int:
        return robocap_frame + self.third_person_video_frame_offset

    def robocap_to_timeline_frame(self, robocap_frame: int) -> int:
        """Return the shared RRD frame index, expressed at the Mocap frame rate."""
        return round(robocap_frame * self.ratio)

    def mocap_to_timeline_frame(self, mocap_frame: int) -> int:
        return mocap_frame - self.mocap_source_frame_offset

    def third_person_to_timeline_frame(self, third_person_frame: int) -> int:
        robocap_frame = third_person_frame - self.third_person_video_frame_offset
        return self.robocap_to_timeline_frame(robocap_frame)

    def gt_to_video_frame_float(self, gt_frame: int) -> float:
        """Compatibility alias for :meth:`mocap_to_robocap_frame_float`."""
        return self.mocap_to_robocap_frame_float(gt_frame)

    def video_to_gt_frame_float(self, video_frame: int) -> float:
        """Compatibility alias for :meth:`robocap_to_mocap_frame_float`."""
        return self.robocap_to_mocap_frame_float(video_frame)

    def video_to_gt_frame(self, video_frame: int) -> int:
        """Compatibility alias for :meth:`robocap_to_mocap_frame`."""
        return self.robocap_to_mocap_frame(video_frame)

    def video_to_third_person_frame(self, video_frame: int) -> int:
        """Compatibility alias for :meth:`robocap_to_third_person_frame`."""
        return self.robocap_to_third_person_frame(video_frame)

    def video_to_timeline_frame(self, video_frame: int) -> int:
        """Compatibility alias for :meth:`robocap_to_timeline_frame`."""
        return self.robocap_to_timeline_frame(video_frame)

    def gt_to_timeline_frame(self, gt_frame: int) -> int:
        """Compatibility alias for :meth:`mocap_to_timeline_frame`."""
        return self.mocap_to_timeline_frame(gt_frame)

    def relative_shift_description(self) -> str:
        if self.video_frame_offset > 0:
            return (
                "NOKOV/GT is advanced (shifted earlier) relative to Robocap video by "
                f"{self.video_frame_offset} Robocap frames"
            )
        if self.video_frame_offset < 0:
            return (
                "NOKOV/GT is delayed (shifted later) relative to Robocap video by "
                f"{abs(self.video_frame_offset)} Robocap frames"
            )
        return "NOKOV/GT has no frame offset relative to Robocap video"

    def third_person_relative_shift_description(self) -> str:
        offset = self.third_person_video_frame_offset
        if offset > 0:
            return (
                "third-person video is advanced (shifted earlier) relative to Robocap video by "
                f"{offset} frames"
            )
        if offset < 0:
            return (
                "third-person video is delayed (shifted later) relative to Robocap video by "
                f"{abs(offset)} frames"
            )
        return "third-person video has no frame offset relative to Robocap video"

    def describe(self) -> str:
        return (
            "Frame alignment active (Robocap reference): "
            "mocap=round(robocap*ratio)+round(mocap_offset*ratio); "
            "third_person=robocap+third_person_offset; "
            f"ratio={self.ratio:.9f}, Mocap offset={self.video_frame_offset:+d} Robocap frames -> "
            f"Mocap source offset={self.mocap_source_frame_offset:+d} frames; "
            f"{self.relative_shift_description()}; Robocap frame 0 -> Mocap frame "
            f"{self.robocap_to_mocap_frame(0)}, Mocap frame 0 -> Robocap frame "
            f"{self.mocap_to_robocap_frame_float(0):.9f}; "
            f"third-person offset={self.third_person_video_frame_offset:+d} frames; "
            f"{self.third_person_relative_shift_description()}; Robocap frame 0 -> third-person "
            f"frame {self.robocap_to_third_person_frame(0)}."
        )
