import pytest


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
