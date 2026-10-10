# Findings

- Baseline commit: 7fea1db. Existing unrelated untracked scripts/files must remain untouched.
- `mocap_integrity.py` currently imposes HH >= 2 bodies; user explicitly removed this rule.
- Batch upload in `web_app.py` audits remote manifests before local validation. Need trace
  whether the comparison includes local files that were never listed remotely.
- `stage_session_dataset` creates manifest and metadata; object naming should be shared with
  `obj_inventory.filename_parts` rather than a new incompatible parser.
- The local per-action rigid-name roster is mostly unknown. Explicitly unverified is distinct
  from missing actual files; do not block useful backfills solely on absent prop rosters.
- Confirmed cause: `audit_remote_sessions` validates only the files listed by the old remote
  manifest. A local object absent from that manifest cannot make the remote audit fail.
- Batch upload skips those "complete" Sessions before generating local reports, even when
  rebuild-all is selected. This prevents both object backfill and reporting local defects.
- Default upload selection already includes all named BVH/CSV/TRC/MP4 exports, not just Body0
  and Tracker0; the primary omission mechanism is skip/reconciliation logic.
- Current `SessionStatistic` records frame counts/durations only, ignoring integrity payloads.
- Staging creates fresh manifest/index rows; replacing them wholesale risks losing records of
  remote-only files. Backfill must merge with current remote data and preserve its path/date.

## Decisions
- Keep frame-count statistics and local structural/content integrity as separate dimensions.
- Implement repository changes and tests; the user intends to run full source scan later.
- Full local validation precedes remote skipping, including Sessions with missing reference video.
- Block structurally broken normal uploads; record failures and warnings separately. Unknown prop
  requirements remain warnings, not evidence that an actual file is missing.
- Reconcile uploads additively at the existing remote path. Preserve remote-only manifest records
  and unknown metadata, and verify retained remote paths without requiring local copies.
- Keep the existing BAT-launched Web batch workflow; no new full-dataset run in this turn.
- Snapshot freshness uses all source paths/sizes/mtime and the requirements CSV digest. Old reports
  are never accepted as current integrity passes. Batch checks always inspect sources first.
- Remote backfill validates remote-only bytes before retaining them; failed post-upload verification
  cannot attempt to replace a remote-only file with a nonexistent local source.
- New manifest entries include SHA-256; remote skip checks compare actual remote hashes (download
  fallback when unavailable), so same-size data changes are also detected.
