import pytest


@pytest.fixture
def fake_metadata_transport(monkeypatch, tmp_path):
    """Keep publisher tests offline while executing real index/manifest merge logic."""
    import glob

    from robocap_rerun_tools import metadata_commit

    def commit(build, *, api, repository, revision, **kwargs):
        changes = build(revision, lambda path: True)
        root = tmp_path / "metadata-transaction"
        root.mkdir(exist_ok=True)
        for path, data in changes.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if changes:
            api.upload_folder(repository, "dataset", root, revision=revision,
                              allow_patterns=[glob.escape(p) for p in changes])
        return "test-commit"

    monkeypatch.setattr(metadata_commit, "require_git", lambda: None)
    monkeypatch.setattr(metadata_commit, "_publish_changes", commit)


@pytest.fixture
def stub_batch_checks(monkeypatch):
    """Isolate batch scheduling tests from media decoding of their placeholder files."""
    from robocap_rerun_tools import batch_completeness
    from robocap_rerun_tools.session_integrity import source_signature

    inspected = []

    def inspect(sessions, output, progress):
        inspected.extend(sessions)
        return {
            session: {
                "status": "passed",
                "errors": 0,
                "warnings": 0,
                "checks": [],
                "source_signature": source_signature(session),
            }
            for session in sessions
        }

    monkeypatch.setattr(batch_completeness, "inspect_local_batch", inspect)
    monkeypatch.setattr(
        batch_completeness, "inspection_is_current", lambda ref, ratio: ref.report_path.is_file()
    )
    return inspected
