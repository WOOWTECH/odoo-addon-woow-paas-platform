"""測試專用 StorageClass override；未設定不改既有 default 選擇。"""

import pytest
import yaml

from tests import namespace_scope
from tests.integration import test_pvc_keep_before_uninstall as keep_fixtures
from tests.integration import test_pvc_orphan_labeling as orphan_fixtures


@pytest.mark.parametrize("override", [None, "longhorn-delete"])
def test_storage_override_renders_pvc_without_changing_other_spec(monkeypatch, tmp_path, override):
    monkeypatch.delenv("OPERATOR_TEST_STORAGE_CLASS", raising=False)
    if override is not None:
        monkeypatch.setenv("OPERATOR_TEST_STORAGE_CLASS", override)
    expected = {} if override is None else {"storageClassName": override}
    assert namespace_scope.integration_storage_class() == expected
    pvc = yaml.safe_load(orphan_fixtures._make_pvc("dev-sandbox-1129-test", "data"))
    assert pvc["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "8Mi"}},
        **expected,
    }
    keep_fixtures._write_chart_owned_pvc_chart(tmp_path / "chart")
    rendered = (tmp_path / "chart/templates/pvc.yaml").read_text().replace(
        "{{ .Release.Name }}", "probe",
    )
    assert yaml.safe_load(rendered)["spec"] == pvc["spec"]


@pytest.mark.parametrize("value", ["", "bad/path", "Upper", "a\nkind: Namespace", "x" * 254])
def test_invalid_storage_override_fails_loudly(monkeypatch, value):
    monkeypatch.setenv("OPERATOR_TEST_STORAGE_CLASS", value)
    with pytest.raises(ValueError, match="StorageClass"):
        namespace_scope.integration_storage_class()
