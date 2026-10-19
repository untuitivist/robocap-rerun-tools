"""Apply an operator-selected capture site to both upload metadata documents."""

import json
from pathlib import Path


def enrich_staged_sites(root: Path, entries, previous_entries, site_id, *, catalog=None, progress=None):
    previous = {(row["primitive_id"], row["session_id"]): row for row in previous_entries}
    result = []
    missing = 0
    for entry in entries:
        path = root / entry["manifest"]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        old = previous.get((entry["primitive_id"], entry["session_id"]), {})
        if site_id:
            site = {"id": site_id, "assignment_method": "env"}
            if catalog:
                site["catalog"] = catalog
        else:
            site = old.get("collection_site") or entry.get("collection_site") or manifest.get("collection_site")
        if site:
            entry = {**entry, "collection_site": site}
            manifest["collection_site"] = site
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            missing += 1
        result.append(entry)
    if progress:
        if site_id:
            progress(f"Collection site: {site_id} (ROBOCAP_COLLECTION_SITE), {len(entries)} Session(s).")
        if missing:
            progress(f"WARNING: {missing} Session(s) have no collection site; set ROBOCAP_COLLECTION_SITE in .env.")
    return result
