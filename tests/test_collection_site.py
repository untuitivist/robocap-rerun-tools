import json

import pytest

from robocap_rerun_tools.collection_site import enrich_staged_sites
from robocap_rerun_tools.modelscope_publisher import ensure_env_file, load_modelscope_settings


def test_settings_read_site_and_preserve_env_when_saving_defaults(tmp_path):
    path = tmp_path / ".env"
    path.write_text("ROBOCAP_COLLECTION_SITE=site_01\n", encoding="utf-8")
    ensure_env_file(path)
    assert load_modelscope_settings(path, environ={}).collection_site == "site_01"
    assert load_modelscope_settings(path, environ={"ROBOCAP_COLLECTION_SITE": "site_02"}).collection_site == "site_02"
    with pytest.raises(ValueError, match="site ID"):
        load_modelscope_settings(path, environ={"ROBOCAP_COLLECTION_SITE": "typo"})


@pytest.mark.parametrize("configured", [None, "site_02"])
def test_site_sync_preserves_remote_or_uses_explicit_setting(tmp_path, configured):
    previous_site = {"id": "site_01", "assignment_method": "reviewed"}
    entries = []
    for number in range(2):
        path = tmp_path / f"manifest{number}.json"
        path.write_text(json.dumps({"duration_s": 42}), encoding="utf-8")
        entries.append({"primitive_id": "P01", "session_id": str(number), "manifest": path.name})
    previous = [{**entries[0], "collection_site": previous_site}]
    logs = []
    result = enrich_staged_sites(tmp_path, entries, previous, configured,
                                 catalog="collection_sites.json", progress=logs.append)
    for number, row in enumerate(result):
        manifest = json.loads((tmp_path / row["manifest"]).read_text(encoding="utf-8"))
        assert manifest["duration_s"] == 42
        assert row.get("collection_site") == manifest.get("collection_site")
        if configured:
            assert row["collection_site"] == {
                "id": configured, "assignment_method": "env", "catalog": "collection_sites.json"
            }
        elif number == 0:
            assert row["collection_site"] == previous_site
        else:
            assert "collection_site" not in row
            assert any("WARNING" in line for line in logs)
