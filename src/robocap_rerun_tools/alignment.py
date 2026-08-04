from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FrameAlignment:
    """Convert the user-facing Robocap offset into the source script's GT-frame offset."""

    ratio: float
    video_frame_offset: int = 0

    def __post_init__(self) -> None:
        ratio = float(self.ratio)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("Frame alignment ratio must be finite and positive.")
        if isinstance(self.video_frame_offset, bool) or int(self.video_frame_offset) != (
            self.video_frame_offset
        ):
            raise ValueError("Robocap video-frame offset must be an integer.")
        object.__setattr__(self, "ratio", ratio)
        object.__setattr__(self, "video_frame_offset", int(self.video_frame_offset))

    @property
    def gt_frame_offset(self) -> int:
        return round(self.video_frame_offset * self.ratio)

    def gt_to_video_frame_float(self, gt_frame: int) -> float:
        return (gt_frame - self.gt_frame_offset) / self.ratio

    def video_to_gt_frame_float(self, video_frame: int) -> float:
        return video_frame * self.ratio + self.gt_frame_offset

    def video_to_gt_frame(self, video_frame: int) -> int:
        return round(video_frame * self.ratio) + self.gt_frame_offset

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

    def describe(self) -> str:
        return (
            "Frame alignment active (source-script formula): "
            "video_float=(gt_index-gt_frame_offset)/ratio; "
            f"ratio={self.ratio:.9f}, Robocap offset={self.video_frame_offset:+d} frames -> "
            f"GT offset={self.gt_frame_offset:+d} frames; {self.relative_shift_description()}; "
            f"video frame 0 -> GT frame {self.video_to_gt_frame(0)}, GT frame 0 -> "
            f"video frame {self.gt_to_video_frame_float(0):.9f}."
        )
