"""Resolve Session demographics from the destination dataset's participant catalog."""

from __future__ import annotations

import json
import math
from pathlib import Path

CATALOG_NAME = "participants.jsonl"
FIELDS = {
    "participant_gender": "gender",
    "participant_height_cm": "height_cm",
    "participant_weight_kg": "weight_kg",
}


def participant_id(record: dict) -> str:
    capture = record.get("mocap_capture") or {}
    value = capture.get("participant") if isinstance(capture, dict) else None
    return value.strip().casefold() if isinstance(value, str) else ""


def parse_catalog(document: str) -> dict[str, dict]:
    catalog = {}
    for number, line in enumerate(document.splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        name = record.get("participant") if isinstance(record, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{CATALOG_NAME}:{number}: missing participant identifier")
        key = name.strip().casefold()
        if key in catalog:
            raise ValueError(f"{CATALOG_NAME}:{number}: duplicate participant {key}")
        for field in ("height_cm", "weight_kg"):
            value = record.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{CATALOG_NAME}:{number}: invalid {field}")
        if record.get("gender") is not None and not isinstance(record["gender"], str):
            raise ValueError(f"{CATALOG_NAME}:{number}: invalid gender")
        catalog[key] = record
    return catalog


def load_catalog(
    api, repository: str, revision: str, *, fallback: str | None = None, progress=None
) -> dict[str, dict]:
    from modelscope_hub.errors import NotExistError

    for source in dict.fromkeys([repository, fallback]):
        if not source:
            continue
        try:
            path = api.download_file(source, "dataset", CATALOG_NAME, revision=revision, force=True)
        except NotExistError:
            continue
        catalog = parse_catalog(Path(path).read_text(encoding="utf-8-sig"))
        if progress:
            progress(f"Participant catalog: {source}/{CATALOG_NAME} ({len(catalog)} people)")
        return catalog
    if progress:
        progress(
            "WARNING: participants.jsonl is missing; retain known same-person metadata, otherwise use null."
        )
    return {}


def enrich_record(record: dict, catalog: dict[str, dict], previous: dict | None = None) -> dict:
    name = participant_id(record)
    person = catalog.get(name)
    if person is not None:
        return {**record, **{target: person.get(source) for target, source in FIELDS.items()}}
    previous = previous or {}
    same_person = bool(name) and participant_id(previous) == name
    # A changed participant must never inherit the previous person's measurements.
    return {
        **record,
        **{
            field: previous.get(field, record.get(field)) if same_person else None
            for field in FIELDS
        },
    }


def enrich_staged_participants(
    root: Path,
    entries: list[dict],
    previous_entries: list[dict],
    catalog: dict[str, dict],
    progress=None,
) -> list[dict]:
    previous = {(r["primitive_id"], r["session_id"]): r for r in previous_entries}
    result = []
    for entry in entries:
        name = participant_id(entry)
        if name not in catalog and progress:
            progress(
                f"WARNING: {entry['session_id']}: participant {name or '(missing)'} not in catalog; no inferred demographics."
            )
        enriched = enrich_record(
            entry, catalog, previous.get((entry["primitive_id"], entry["session_id"]))
        )
        path = root / entry["manifest"]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update({field: enriched[field] for field in FIELDS})
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result.append(enriched)
    return result
