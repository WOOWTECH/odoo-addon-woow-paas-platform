"""#449 integration：reset-config 依 ``admin_password_carrier`` 分流（真 helm + 真 kubectl）。

unit 測試 mock 掉 ``k8s_service`` 與 ``helm_service``，只驗得到「handler 有沒有照
策略呼叫某些方法」。它驗不到本 issue 真正的兩個致命點，而這兩點都只在**真叢集**
現形（BUG-7 workload-type family，見 tests/integration/README.md）：

1. **多 Deployment**：hermes-agent / open-design 兩個 chart 都渲染 daemon + console
   兩顆 Deployment 共用同一個 ``app.kubernetes.io/instance``。單數版
   ``get_deployment_name`` 撞多筆 raise ``Ambiguous selector`` → 500。mock 回一個
   字串就永遠看不到這條分支。
2. **檔案佈局假設**：那些 chart 的 PVC 裡沒有 ``/data/config.json``，exec patch 必炸、
   備份未建成、補償 ``os.replace`` 跟著炸 → ``operator intervention required``。
   這正是 #424① E2E 實錄的症狀，本檔用一個「沒有 config.json 也沒有 python3」的
   多 Deployment release 原地重現它。

本地跑（無 CI runner）::

    HELM_BINARY=$(which helm) pytest -m integration \
        tests/integration/test_reset_config_carrier.py -v
"""
import base64
import json
import os
import subprocess
import time

import pytest

from src.api.releases import RESET_HARD_CAP_SECONDS

pytestmark = pytest.mark.integration

# 與 config.Settings.registry_secret_name 預設一致（operator 注入的 secret 名）。
_REGISTRY_SECRET = "paas-registry"

_CHART_YAML = """\
apiVersion: v2
name: carrier-probe
description: two Deployments + a chart-managed Secret, mirroring hermes-agent/open-design
type: application
version: 0.1.0
appVersion: "1.0"
"""

_VALUES_YAML = """\
config:
  sensitive:
    admin_password: chart-default-password
"""

# chart 自管 Secret＝密碼的權威來源（hermes-agent secret.yaml 的最小鏡射：
# 同樣吃 .Values.config.sensitive.admin_password，同樣是 stringData.admin_password）。
_SECRET_TMPL = """\
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-auth
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
type: Opaque
stringData:
  admin_password: {{ .Values.config.sensitive.admin_password | quote }}
"""

# ⚠ 刻意**沒有** checksum/secret annotation —— 與真 chart 一致（實查
# charts/hermes-agent 與 charts/open-design 的 deployment.yaml 只有
# checksum/config 與 checksum/authproxy-config）。因此改 Secret 不會讓 helm
# 滾 pod，密碼要生效**必須**由 operator 顯式 rollout restart。
_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-daemon
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/component: daemon
spec:
  replicas: 1
  # 真 chart 的四顆 Deployment 全是 Recreate（#459 §0 實查，與 RWO PVC 一致）：
  # 先殺舊 pod 再建新 pod。這個差異對 #461 是**實質的**——RollingUpdate 下舊 pod
  # 會撐到新 pod Ready，helm 的 rollback wait 因此瞬間通過、量不到「第二次
  # --timeout」；Recreate 才會讓 rollback 也真的等一次。
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: {{ .Release.Name }}-daemon
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}-daemon
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- if .Values.slowStart }}
      # #461：讓「重啟後的新 pod 要一段時間才 Ready」可控地重現。真 chart 的
      # 慢來源是跨節點 pull 954MB image ＋ RWO PVC detach/attach（#459 實測
      # 70s ＋ 19s），合成 chart 用 sleep 取代——要驗的是「handler 有沒有等」，
      # 不是「為什麼慢」。
      initContainers:
      - name: slow-start
        image: nginx:alpine
        command: ["sleep", "30"]
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
      {{- end }}
      containers:
      - name: app
        image: nginx:alpine
        env:
        - name: ADMIN_PASSWORD
          valueFrom:
            secretKeyRef:
              name: {{ .Release.Name }}-auth
              key: admin_password
        {{- if .Values.breakReadiness }}
        # #461：指向關閉的 port ⇒ 新 ReplicaSet 永遠不 Ready ⇒ `helm upgrade
        # --atomic --wait` 必逾時並自動 rollback。這是用來量測「atomic 失敗路徑
        # 吃兩次 --timeout」的手術刀。
        readinessProbe:
          tcpSocket:
            port: 9999
          initialDelaySeconds: 1
          periodSeconds: 2
          failureThreshold: 1
        {{- end }}
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""

_CONSOLE_DEPLOYMENT_TMPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-console
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/component: console
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ .Release.Name }}-console
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}-console
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
      - name: console
        image: nginx:alpine
        resources:
          requests:
            cpu: 10m
            memory: 16Mi
"""


@pytest.fixture
def carrier_chart(tmp_path):
    """兩顆 Deployment 共用 instance label ＋ chart 自管 Secret 的最小 chart。"""
    chart_dir = tmp_path / "carrier-probe"
    (chart_dir / "templates").mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(_CHART_YAML)
    (chart_dir / "values.yaml").write_text(_VALUES_YAML)
    (chart_dir / "templates" / "secret.yaml").write_text(_SECRET_TMPL)
    (chart_dir / "templates" / "deployment.yaml").write_text(_DEPLOYMENT_TMPL)
    (chart_dir / "templates" / "console-deployment.yaml").write_text(
        _CONSOLE_DEPLOYMENT_TMPL)
    return str(chart_dir)


def _install(app_client, namespace, release, chart, values=None):
    resp = app_client.post(
        "/api/releases",
        json={
            "namespace": namespace,
            "name": release,
            "chart": chart,
            "values": values or {},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "deployed"


def _reset_body(password, chart, carrier=None, values=None):
    body = {
        "chart": chart,
        # reset_values=True ⇒ chart values.yaml 為 base。呼叫端（平台）一律傳全量
        # values；測試若省略，pod template 會跟著變（例如 slowStart 消失），那就
        # 不再是「Secret-only 升級」的形狀了。
        "values": values or {},
        "sensitive_values": {"config": {"sensitive": {"admin_password": password}}},
        "reset_values": True,
        "reuse_values": False,
    }
    if carrier is not None:
        body["admin_password_carrier"] = carrier
    return body


def _wait_rollout(namespace, deployment, timeout_seconds=180):
    """等 Deployment 真的滾完（install 不帶 --wait，所以要自己等）。"""
    subprocess.run(
        ["kubectl", "rollout", "status", f"deployment/{deployment}",
         "-n", namespace, f"--timeout={timeout_seconds}s"],
        check=True, capture_output=True,
    )


def _rollout_in_progress(namespace, deployment, probe_seconds=2):
    """True ⇒ 此刻 rollout **尚未**完成（用 kubectl 自己的判準，不自研）。"""
    probe = subprocess.run(
        ["kubectl", "rollout", "status", f"deployment/{deployment}",
         "-n", namespace, f"--timeout={probe_seconds}s"],
        capture_output=True,
    )
    return probe.returncode != 0


def _release_status(namespace, release):
    """helm 眼中的 release 狀態（`deployed` / `failed` / `pending-*` …）。

    用 operator 自己解析到的同一個 helm binary，避免本機 PATH 上是另一版。
    """
    helm_bin = os.environ.get("HELM_BINARY") or "helm"
    raw = subprocess.check_output(
        [helm_bin, "status", release, "-n", namespace, "-o", "json"],
    ).decode()
    return json.loads(raw)["info"]["status"]


def _secret_password(namespace, release):
    encoded = subprocess.check_output(
        ["kubectl", "get", "secret", f"{release}-auth", "-n", namespace,
         "-o", "jsonpath={.data.admin_password}"],
    ).decode()
    return base64.b64decode(encoded).decode()


def _restarted_at(namespace, deployment):
    return subprocess.check_output([
        "kubectl", "get", "deployment", deployment, "-n", namespace,
        "-o",
        "jsonpath={.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}",
    ]).decode().strip()


def _pod_spec_pull_secrets(namespace, deployment):
    return subprocess.check_output(
        ["kubectl", "get", "deployment", deployment, "-n", namespace,
         "-o", "jsonpath={.spec.template.spec.imagePullSecrets[*].name}"],
    ).decode()


def test_helm_secret_carrier_rotates_secret_and_restarts_every_deployment(
    app_client, test_namespace, carrier_chart
):
    """helm_secret 路：Secret 換新 ＋ 兩顆 Deployment 都真的被滾過。

    「兩顆都滾」不是潔癖：密碼是**啟動期**消費的（真 chart 用 initContainer 把它
    算成 nginx htpasswd、webui/dashboard 走 env），少滾一顆＝那個入口還吃舊密碼。
    而 chart 沒有 checksum/secret annotation，helm 自己不會滾。
    """
    release = "carrier-hs-rel"
    _install(app_client, test_namespace, release, carrier_chart)

    assert _secret_password(test_namespace, release) == "chart-default-password"
    for suffix in ("daemon", "console"):
        assert not _restarted_at(test_namespace, f"{release}-{suffix}"), (
            "install 後不該有 restartedAt")

    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-pw-449", carrier_chart, carrier="helm_secret"),
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["admin_password_carrier"] == "helm_secret"
    assert payload["config_json_patched"] is False
    assert sorted(payload["restarted_deployments"]) == [
        f"{release}-console", f"{release}-daemon",
    ]

    assert _secret_password(test_namespace, release) == "rotated-pw-449"
    for suffix in ("daemon", "console"):
        assert _restarted_at(test_namespace, f"{release}-{suffix}"), (
            f"{suffix} Deployment 沒有 restartedAt —— 新密碼不會生效（helm 不會"
            f"因為 Secret 變了就滾 pod）")


def test_helm_secret_carrier_keeps_image_pull_secrets(
    app_client, test_namespace, carrier_chart
):
    """#448 的 gap 在 reset 路也要收掉（reset_values=True ⇒ 前一版 values 全丟）。"""
    release = "carrier-ips-rel"
    _install(app_client, test_namespace, release, carrier_chart)

    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-pw-ips", carrier_chart, carrier="helm_secret"),
    )
    assert resp.status_code == 200, resp.text

    for suffix in ("daemon", "console"):
        rendered = _pod_spec_pull_secrets(test_namespace, f"{release}-{suffix}")
        assert _REGISTRY_SECRET in rendered, (
            f"{suffix} podSpec 掉了 imagePullSecrets —— 私有 image 會 "
            f"ImagePullBackOff 401；got {rendered!r}")


def test_config_json_carrier_reproduces_the_449_failure_on_such_a_chart(
    app_client, test_namespace, carrier_chart
):
    """修復前的行為，對著真叢集釘住：這就是為什麼需要分流。

    對一個沒有 ``/data/config.json``（也沒有 python3）的多 Deployment release 走
    config_json 路 → exec patch 炸 → 備份沒建成 → 補償 ``os.replace`` 跟著炸 →
    500 ``operator intervention required``。#424① E2E 實錄的症狀。

    這條測試同時是「載體標錯的代價」的證據：把 hermes-agent 留在預設載體，租戶
    的密碼永遠輪替不了。
    """
    release = "carrier-cfg-rel"
    _install(app_client, test_namespace, release, carrier_chart)

    # 不帶 admin_password_carrier ＝ 舊平台的 payload（發版順序安全性的實測）。
    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-pw-cfg", carrier_chart),
    )

    assert resp.status_code == 500, resp.text
    assert "reset-config" in resp.json()["detail"]

    # fail-safe 邊界：helm 已回滾，Secret 回到舊密碼 —— 租戶不會被鎖死，只是
    # 換不了密碼（#449 描述的「fail-safe 有效不鎖死」）。
    assert _secret_password(test_namespace, release) == "chart-default-password"


# ── #461：把「不等 rollout」與「atomic 失敗吃兩次 --timeout」變成量測 ──────────
#
# 這兩條是 #461 的核心機制，而 unit 測試永遠測不到它們：mock 掉 kubectl 之後
# 「有沒有等」只是「有沒有呼叫某個方法」，量不到牆鐘；`--atomic` 的自動 rollback
# 更是 helm 自己的行為，mock 裡根本不存在。#459 就是因此讓 120s 預算的問題一路
# 活到 stg 真機才現形（合成 chart：小 image、無 PVC、同節點 ⇒ 永遠不逾時）。


def test_helm_secret_reset_returns_before_pods_are_ready(
    app_client, test_namespace, carrier_chart
):
    """#461：reset 必須在 rollout 還沒完成時就回 200。

    fixture 前置條件（**必要**）：先等首裝滾完。若 pod 當下不是 Ready，
    `helm upgrade --atomic --wait` 會等它 ⇒ 逾時 ⇒ 測到的是 atomic 失敗路徑
    （下一條），而不是「不等 rollout」。

    slowStart 讓**重啟後**的新 pod 要 ~30s 才 Ready，但升級當下的舊 pod 是
    Ready 的 ⇒ Secret-only 的 `--wait` 立即通過 ⇒ 剩下的等待就只有 rollout。
    修前（等 rollout status）這裡會花 >30s；修後應該 <20s。
    """
    release = "carrier-nowait-rel"
    values = {"slowStart": True}
    _install(app_client, test_namespace, release, carrier_chart, values=values)
    _wait_rollout(test_namespace, f"{release}-daemon")
    _wait_rollout(test_namespace, f"{release}-console")

    started = time.monotonic()
    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        # values 必須帶上 slowStart：reset_values=True 之下省略它會讓 initContainer
        # 從 pod template 消失＝pod template 有變，`--wait` 就會等新 pod（形狀不對）。
        json=_reset_body("rotated-nowait", carrier_chart, carrier="helm_secret",
                         values=values),
    )
    elapsed = time.monotonic() - started
    print(f"\n[#461] helm_secret reset wall-clock = {elapsed:.1f}s (門檻 20s)")

    assert resp.status_code == 200, resp.text
    assert resp.json()["rollout_awaited"] is False
    assert elapsed < 20, (
        f"reset 花了 {elapsed:.1f}s —— 超過門檻代表它在等 rollout（新 pod 要 ~30s "
        f"才 Ready）。這正是 #461：等待落在 request 內 ⇒ 冷 image cache 時必逾時"
    )

    # 提交點已完成：Secret 換新、兩顆都下了 restart。
    assert _secret_password(test_namespace, release) == "rotated-nowait"
    for suffix in ("daemon", "console"):
        assert _restarted_at(test_namespace, f"{release}-{suffix}"), (
            f"{suffix} 沒有 restartedAt ⇒ 新密碼不會生效（chart 沒有 "
            f"checksum/secret，helm 不會自己滾 pod）"
        )

    # 而且此刻 rollout **確實還沒完成** —— 否則 <20s 只是「pod 起太快」，
    # 斷言就沒有牙（fixture 的 slowStart 失效時這裡會抓到）。
    assert _rollout_in_progress(test_namespace, f"{release}-daemon"), (
        "daemon rollout 已完成 ⇒ slowStart fixture 沒生效，上面的時間斷言不成立"
    )


def test_atomic_failure_rolls_secret_back_within_hard_cap(
    app_client, test_namespace, carrier_chart
):
    """#461：`--atomic` 失敗路徑要吃兩次 `--timeout`，subprocess 上限必須蓋住。

    `helm --timeout` 是 per-operation：`--atomic` 失敗時 helm 會再跑一次帶 wait 的
    rollback。subprocess 上限只給「一次 timeout + 零頭」會在 rollback **進行中**
    SIGKILL helm ⇒ release 卡 `pending-*`、Secret 停在沒人持有的新密碼 ⇒ 打破
    「2xx 之前的失敗保證叢集仍是舊密碼」（C11′ 的前提）。

    breakReadiness 讓新 ReplicaSet 永遠不 Ready ⇒ 強制一次真的 atomic 失敗。
    **slowStart 同時開著是刻意的**：Recreate ＋ 慢啟動讓 rollback 之後的那顆新 pod
    也要 ~30s 才 Ready ⇒ helm 的 rollback wait 也會用滿一次 `--timeout` ⇒ 這條測試
    才真的量到 `2n`（只開 breakReadiness 時舊 pod 會撐著、rollback 瞬間通過，
    量到的是 1n，斷言就沒有牙）。

    紅燈對照：把 RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS 改回 `n+10`(25) 重跑，
    helm 會在 rollback 途中被 SIGKILL ⇒ release status 卡 `pending-rollback`。
    """
    release = "carrier-atomic-rel"
    values = {"slowStart": True}
    _install(app_client, test_namespace, release, carrier_chart, values=values)
    _wait_rollout(test_namespace, f"{release}-daemon")
    _wait_rollout(test_namespace, f"{release}-console")

    started = time.monotonic()
    resp = app_client.post(
        f"/api/releases/{test_namespace}/{release}/reset-config",
        json=_reset_body("rotated-atomic", carrier_chart, carrier="helm_secret",
                         values={**values, "breakReadiness": True}),
    )
    elapsed = time.monotonic() - started
    print(
        f"\n[#461] atomic 失敗路徑 wall-clock = {elapsed:.1f}s "
        f"(hard cap {RESET_HARD_CAP_SECONDS}s；2n+O 的實測驗算)"
    )

    assert resp.status_code == 500, resp.text
    assert "chart Secret" in resp.json()["detail"]
    assert elapsed < RESET_HARD_CAP_SECONDS, (
        f"失敗路徑花了 {elapsed:.1f}s，超過 operator 自己宣告的硬上限 "
        f"{RESET_HARD_CAP_SECONDS}s ⇒ 外層預算階梯（平台 75s／前端 90s）失效"
    )

    # 最關鍵的一條：rollback **跑完了**（沒被腰斬），所以叢集回到舊密碼。
    assert _secret_password(test_namespace, release) == "chart-default-password", (
        "Secret 沒有回到舊密碼 ⇒ atomic rollback 被 subprocess 上限腰斬 ⇒ "
        "租戶會被鎖在一把沒人持有的密碼上"
    )
    status = _release_status(test_namespace, release)
    assert not status.startswith("pending"), (
        f"release 卡在 {status} ⇒ helm 在收尾途中被 kill（同上，預算不足）"
    )
