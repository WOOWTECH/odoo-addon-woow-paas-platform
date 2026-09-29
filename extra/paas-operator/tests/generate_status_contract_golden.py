"""產生 ``GET /api/releases/{ns}/{name}/status`` 的 wire 契約 golden——**打真的端點**。

## 為什麼要這支（#1263／#1251 Q44）

#1251 的 readiness 判定靠 ``PodInfo`` 的 ``component``／``task``／``owner_kind`` 與
``pod_identity_included``。這些值由 ``KubernetesService.get_pods`` 從 kubectl JSON
抽出來；平台端測試若自己手寫 Pod dict，釘住的只是作者的信念——label key 打錯、
owner 取錯筆，平台測試照樣全綠而 prod 判定全錯。

## 判準：以**真的 HTTP body** 為權威

``TestClient`` 打 ``GET …/status``，只換掉兩個邊界：

- ``helm_service.get``（release 本體不是本契約的重點）；
- ``subprocess.run``（回傳 kubectl ``get pods -o json``，Pod 的 labels／ownerReferences
  照 woow-odoo chart ``758cdf6c``（0.2.2）的 template 寫）。

另外 stub ``_calculate_age``：``age`` 是隨 wall clock 變的人讀顯示值，不 stub 的話
golden 每跑一次就漂；其餘全部走真碼。

## 重現

    cd extra/paas-operator
    .venv/bin/python tests/generate_status_contract_golden.py

輸出寫到 ``src/tests/fixtures/status_contract_golden.json``（**Odoo 側**：``extra/`` 不會
被打進 addon）。operator 端由 ``tests/test_status_contract_golden_1263.py`` 重跑
``build_cases()`` 斷言 == golden；平台端 factory 的 key 集合由
``scripts/audit/test_operator_contract_shape.py`` 對到 golden。
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402
from src.models.schemas import ReleaseInfo, ReleaseStatus  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "src" / "tests" / "fixtures" / "status_contract_golden.json"

_NS = "paas-ws-test-1263"
_RELEASE = "svc-1263"
_INSTANCE = {"app.kubernetes.io/instance": _RELEASE, "app.kubernetes.io/name": "odoo"}
_CREATED = "2026-09-29T01:00:00Z"


def _owner(kind, controller=True):
    return {"apiVersion": "apps/v1", "kind": kind, "name": f"{_RELEASE}-{kind.lower()}",
            "uid": f"uid-{kind.lower()}", "controller": controller,
            "blockOwnerDeletion": True}


def _pod(name, component, owner, phase="Running", ready=(1, 1), task=None, **meta):
    labels = {**_INSTANCE, "app.kubernetes.io/component": component}
    if task:
        labels["woow.paas/task"] = task
    ready_count, total = ready
    return {
        "metadata": {
            "name": name, "namespace": _NS, "labels": labels,
            "ownerReferences": [owner], "creationTimestamp": _CREATED, **meta,
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {"ready": i < ready_count, "restartCount": 0} for i in range(total)
            ],
        },
    }


#: woow-odoo chart 一個 release 會出現的 Pod（label／owner 照 chart template）。
_WOOW_ODOO_PODS = [
    # deployment.yaml：component=server，odoo＋nginx 兩個容器，owner＝ReplicaSet。
    _pod("svc-1263-6d8f7c9b5-server", "server", _owner("ReplicaSet"), ready=(2, 2)),
    # Recreate 收尾中的舊 server（#1156：phase 仍 Running，靠 deletion_timestamp 辨識）。
    _pod("svc-1263-5c7e6b8a4-old", "server", _owner("ReplicaSet"), ready=(0, 2),
         deletionTimestamp="2026-09-29T01:05:00Z"),
    # cron-deployment.yaml（platform profile）：component=cron。
    _pod("svc-1263-cron-7f9d8c6b5-x", "cron", _owner("ReplicaSet")),
    # postgres.yaml：component=postgresql，owner＝StatefulSet。
    _pod("svc-1263-postgresql-0", "postgresql", _owner("StatefulSet")),
    # backup-cronjob.yaml：component=db-backup＋woow.paas/task，owner＝Job。
    _pod("svc-1263-backup-29000000-abcde", "db-backup", _owner("Job", controller=True),
         phase="Succeeded", ready=(0, 1), task="backup"),
]

#: 每一格宣告它**應該**長成的樣子；產生後自檢，沒走到的分支不會變成看似正常的 golden。
CASES = {
    "woow_odoo_chart": lambda body: (
        body["pod_identity_included"] is True
        and body["pod_retrieval_error"] is None
        and {p["name"]: (p["component"], p["task"], p["owner_kind"]) for p in body["pods"]} == {
            "svc-1263-6d8f7c9b5-server": ("server", None, "ReplicaSet"),
            "svc-1263-5c7e6b8a4-old": ("server", None, "ReplicaSet"),
            "svc-1263-cron-7f9d8c6b5-x": ("cron", None, "ReplicaSet"),
            "svc-1263-postgresql-0": ("postgresql", None, "StatefulSet"),
            "svc-1263-backup-29000000-abcde": ("db-backup", "backup", "Job"),
        }
        and all(p["creation_timestamp"] == _CREATED for p in body["pods"])
    ),
    # 抓 Pod 失敗：pods 為空、錯誤另外說，旗標仍為 True（能力宣告，與這次成敗無關）。
    "pod_retrieval_failed": lambda body: (
        body["pods"] == [] and bool(body["pod_retrieval_error"])
        and body["pod_identity_included"] is True
    ),
}


def _kubectl(returncode, items=None):
    stdout = json.dumps({"items": items}) if items is not None else ""
    return subprocess.CompletedProcess([], returncode, stdout=stdout,
                                       stderr="" if returncode == 0 else "forbidden")


def build_cases():
    """打真的端點，回傳 {情形: HTTP body}。golden 與 operator 端測試共用這一支。"""
    release = ReleaseInfo(
        name=_RELEASE, namespace=_NS, revision=3, status=ReleaseStatus.DEPLOYED,
        chart="woow-odoo", chart_version="0.2.2", app_version="18.0",
        updated="2026-09-29T01:00:00Z",
    )
    kubectl = {
        "woow_odoo_chart": _kubectl(0, _WOOW_ODOO_PODS),
        "pod_retrieval_failed": _kubectl(1),
    }
    cases = {}
    with patch("src.main.settings.api_key", "golden-1263"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("src.services.helm.KubernetesService._calculate_age",
                  staticmethod(lambda created: "5m")):
        client = TestClient(app, headers={"X-API-Key": "golden-1263"})
        for name in CASES:
            with patch("subprocess.run", return_value=kubectl[name]):
                response = client.get(f"/api/releases/{_NS}/{_RELEASE}/status")
            assert response.status_code == 200, (name, response.text)
            cases[name] = response.json()
    return cases


def _fingerprint():
    """從 repo-lint 取契約指紋（單一權威，同 route golden 的理由）。"""
    import importlib.util
    lint_path = REPO_ROOT / "scripts" / "audit" / "repo-lint.py"
    spec = importlib.util.spec_from_file_location("_repo_lint", lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fp, err = mod.status_contract_fingerprint(str(REPO_ROOT))
    if err:
        raise SystemExit("無法計算契約指紋：%s" % err)
    return fp


def main():
    cases = build_cases()
    bad = [name for name, ok in CASES.items() if not ok(cases[name])]
    if bad:
        raise SystemExit(
            "這幾格沒長成宣稱的樣子（分支沒被走到？）：%s\n%s"
            % (bad, json.dumps({k: cases[k] for k in bad}, indent=2, ensure_ascii=False)))
    cases["_contract_fingerprint"] = _fingerprint()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    print("== 寫出 golden:", OUT)


if __name__ == "__main__":
    main()
