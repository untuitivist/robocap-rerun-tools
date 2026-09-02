from __future__ import annotations

import re
from dataclasses import asdict, dataclass

COMPACT_ACTION_ID_TOKEN = r"[A-Z]\d+"
MOCAP_CAPTURE_DIRECTORY_PATTERN = re.compile(
    rf"^mocap-(?P<action>{COMPACT_ACTION_ID_TOKEN})-S(?P<session>\d+)-"
    r"(?P<participant>.+)-(?P<count>\d+)p\d*$",
    re.IGNORECASE,
)
MOCAP_ACTION_ID_PATTERN = re.compile(rf"{COMPACT_ACTION_ID_TOKEN}\Z", re.IGNORECASE)
INVALID_PARTICIPANT_PATTERN = re.compile(r"[\\/\x00-\x1f\x7f]")


@dataclass(frozen=True)
class MocapCaptureMetadata:
    source_directory: str
    action_id: str
    collection_session_index: int
    participant: str
    repetition_count: int

    def as_record(self) -> dict[str, object]:
        return asdict(self)


def validate_mocap_action_id(value: object) -> str:
    action_id = str(value).strip().upper()
    if MOCAP_ACTION_ID_PATTERN.fullmatch(action_id) is None:
        raise ValueError(
            "Mocap action ID must contain one letter followed by one or more digits, "
            "for example L1, L01, or L1234."
        )
    return action_id


def _integer_field(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be an integer greater than or equal to {minimum}.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}.") from exc
    if not number.is_integer() or number < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}.")
    return int(number)


def build_mocap_capture_metadata(
    source_directory: object,
    action_id: object,
    collection_session_index: object,
    participant: object,
    repetition_count: object,
) -> MocapCaptureMetadata:
    directory = str(source_directory).strip()
    if not directory or directory in {".", ".."} or "/" in directory or "\\" in directory:
        raise ValueError("Mocap source directory must be one direct directory name.")
    resolved_participant = str(participant).strip()
    if not resolved_participant or INVALID_PARTICIPANT_PATTERN.search(resolved_participant):
        raise ValueError("Mocap participant must be non-empty and cannot contain path separators.")
    return MocapCaptureMetadata(
        source_directory=directory,
        action_id=validate_mocap_action_id(action_id),
        collection_session_index=_integer_field(
            collection_session_index,
            "Mocap collection Session index",
            minimum=0,
        ),
        participant=resolved_participant,
        repetition_count=_integer_field(
            repetition_count,
            "Mocap repetition count",
            minimum=1,
        ),
    )


def parse_mocap_capture_directory(name: str) -> MocapCaptureMetadata | None:
    match = MOCAP_CAPTURE_DIRECTORY_PATTERN.fullmatch(name)
    if match is None:
        return None
    return build_mocap_capture_metadata(
        name,
        match.group("action"),
        match.group("session"),
        match.group("participant"),
        match.group("count"),
    )
