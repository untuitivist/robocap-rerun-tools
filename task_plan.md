# Batch completeness and Mocap backfill

## Goal
Reconcile every local Session against remote files before skipping, record Mocap names in
metadata, expose incomplete local data in statistics, and remove the fixed HH body-count rule.
Deliver code and BAT/UI workflow; the user will run the full local-data scan/upload later.

## Phases
1. Trace staging, remote skip audit, inspection and statistics — complete.
2. Implement shared inventories, additive remote backfill and local integrity reporting — complete.
3. Validate missing remote props, incomplete sources, metadata, retries and repeat runs — complete.
4. Update Chinese instructions and .NOTE.md, commit scoped changes — complete.

## Confirmed constraints
- Body0 and Tracker0 remain mandatory. No fixed minimum number of HH bodies.
- Record actual names (Body0, Tracker0, named objects), not inferred physical identities.
- Existing remote Sessions must not hide local files omitted from older manifests.
- Preserve remote-only data, original remote dates and metadata, and source files.
- Local missing/corrupt data must be visible; do not describe it as a successful complete upload.
- Keep double-click Windows BAT usage, Chinese instructions, UTF-8 without BOM.
- No actual bulk scan/upload or new push was requested in this turn.

## Errors
- cmd outer quoting split a regex and later a multiword commit message; use the explicitly
  installed PowerShell 7 for quoted command arguments.
- Initial tests caught relative default Mocap selections in the inventory builder; now resolved
  through the publisher's shared selection validator.
- Preserve read-only external-output inspection behavior: only save an in-Session integrity cache
  when the inspection output is inside that Session. Batch uploading deliberately keeps a cache.
