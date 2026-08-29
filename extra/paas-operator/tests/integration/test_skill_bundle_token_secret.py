"""Integration tests for #955 / F-GC-1: SKILL_BUNDLE_TOKEN must ride a Secret.

真 K8s + 真 helm + 真 kubectl，走 operator 自己的 HTTP endpoint（spawn-or-attach /
reap / resume / sync-workspaces），再用 kubectl 讀叢集實況。unit 測試把 helm 與
kubectl 都 mock 掉，所以以下三件事在 unit 層量不到：

1. pod spec 裡 ``SKILL_BUNDLE_TOKEN`` 到底渲染成 literal ``value`` 還是
   ``valueFrom.secretKeyRef``（那是 chart render 的結果，不是 operator 的回傳值）；
2. ``optional: false`` 的 secretKeyRef 解不開時 pod 起不來（真 kubelet 行為）；
3. 🔴 ``helm upgrade --reuse-values`` 會不會把 resume 注入的新 token 蓋回 install
   當時那顆（真 helm 的三方合併行為，mock 裡不存在）——即計畫 §0.4(C)。

⚠️ **這裡驗的是 operator 這一半的契約，不是真的 agent-runtime chart。** 本檔在
``tmp_path`` 寫一顆最小 stub chart，它照 §5.2 的雙模 values schema 實作，所以同時
也是要交給 ``woow-paas-charts`` 的可執行規格。真 chart 的 wire 由對方 repo 的
chart tests ＋ stg 端到端（計畫 §6.4）負責，⛔ 不要把本檔綠了讀成「chart 也驗過」。

⚠️ **pod Ready ≠ token 真的有效**：stub chart 跑的是 nginx，沒有 agent-runtime image
的 skill-bundle fetch，也沒有 Odoo。真的打 ``skill-bundle-fetch`` 拿 200 是 stg 那一層
（計畫 §6.4 E4）的事。

INV-F 紀律：本檔**不 print、不寫檔、不放進 assertion message** 任何 token 本體，
一律只比對 sha256 摘要與 JWT claim。

Run::

    cd extra/paas-operator
    KUBECONFIG_FOR_TESTS=/tmp/kubeconfig-integration.yaml
    kubectl config view --raw --minify --flatten --context=docker-desktop \
        > "$KUBECONFIG_FOR_TESTS"
    HELM_VERSION=$(sed -n 's/^ARG HELM_VERSION=//p' Dockerfile)   # ⛔ 不用 $(which helm)
    KUBECONFIG="$KUBECONFIG_FOR_TESTS" \
        HELM_BINARY="/tmp/helm-${HELM_VERSION}/darwin-arm64/helm" \
        .venv/bin/pytest -m integration \
        tests/integration/test_skill_bundle_token_secret.py -v -rs
"""
import base64
import hashlib
import json
import subprocess
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

_TEST_PREFIX = "paas-ws-test-op-"
_TEST_AUTH_SECRET = "integration-skill-bundle-hs256-key"
_ENV_KEY = "SKILL_BUNDLE_TOKEN"


# --------------------------------------------------------------------------
# stub chart —— 同時是要交給 woow-paas-charts 的雙模契約可執行規格（計畫 §5.2）
# --------------------------------------------------------------------------

_DEPLOYMENT_TPL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
  labels:
    app.kubernetes.io/instance: {{ .Release.Name }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app.kubernetes.io/instance: {{ .Release.Name }}
    spec:
      containers:
      - name: runtime
        image: nginx:alpine
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 128Mi
        env:
        - name: PROJECT_HASH
          value: {{ .Values.projectHash | quote }}
        - name: SKILL_BUNDLE_TOKEN
{{- if .Values.skillBundleToken.fromSecret }}
          valueFrom:
            secretKeyRef:
              name: {{ .Values.skillBundleToken.secretName | default (printf "%s-skill-bundle" .Release.Name) }}
              key: {{ .Values.skillBundleToken.key }}
              optional: false
{{- else }}
          value: {{ .Values.env.SKILL_BUNDLE_TOKEN | quote }}
{{- end }}
"""

_VALUES_YAML = """\
projectHash: ""
env:
  SKILL_BUNDLE_TOKEN: ""
# #955 雙模開關。false（預設）= 舊行為，literal env value；true = secretKeyRef。
# 預設 false 讓「新 chart + 舊 operator」與今天 byte-identical。
skillBundleToken:
  fromSecret: false
  secretName: ""
  key: SKILL_BUNDLE_TOKEN
"""


def _write_stub_chart(chart_dir) -> str:
    """最小 agent-runtime stub：雙模渲染 SKILL_BUNDLE_TOKEN，其餘 values 一律忽略。"""
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: agent-runtime-stub\nversion: 0.1.0\n"
    )
    (chart_dir / "values.yaml").write_text(_VALUES_YAML)
    templates = chart_dir / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "deployment.yaml").write_text(_DEPLOYMENT_TPL)
    return str(chart_dir)


# --------------------------------------------------------------------------
# kubectl helpers
# --------------------------------------------------------------------------


def _kubectl_json(*args):
    out = subprocess.run(
        ["kubectl", *args, "-o", "json"], capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def _deployment_env_entry(namespace: str, release: str):
    """Return the container's SKILL_BUNDLE_TOKEN env entry dict (or None)."""
    data = _kubectl_json("get", "deployment", release, "-n", namespace)
    assert data is not None, f"deployment {release} not found in {namespace}"
    containers = data["spec"]["template"]["spec"]["containers"]
    for container in containers:
        for entry in container.get("env") or []:
            if entry.get("name") == _ENV_KEY:
                return entry
    return None


def _secret_token(namespace: str, secret_name: str, key: str):
    """Base64-decode one Secret key. ⛔ 回傳值只可拿去 sha256 / 解 claim。"""
    data = _kubectl_json("get", "secret", secret_name, "-n", namespace)
    if data is None:
        return None
    raw = (data.get("data") or {}).get(key)
    if raw is None:
        return None
    return base64.b64decode(raw).decode()


def _effective_token_digest(namespace: str, release: str) -> str:
    """sha256 of the token the pod would actually receive — shape-agnostic.

    刻意同時吃兩種設計：舊設計的 literal ``value``、新設計的
    ``valueFrom.secretKeyRef``。這樣「resume 之後 --reuse-values 有沒有把 token 蓋回
    install 當時那顆」這個問題，在兩種設計上問的是同一件事、答案可直接比較——
    否則 RED 會紅在「找不到 Secret」而不是紅在待驗的那條性質上。
    """
    entry = _deployment_env_entry(namespace, release)
    assert entry is not None, f"{_ENV_KEY} env entry missing on deployment {release}"
    if "value" in entry:
        token = entry["value"]
    else:
        ref = entry["valueFrom"]["secretKeyRef"]
        token = _secret_token(namespace, ref["name"], ref["key"])
        assert token, (
            f"secretKeyRef points at secret {ref['name']}/{ref['key']} but it has no value"
        )
    return hashlib.sha256(token.encode()).hexdigest()


def _jwt_claims(token: str) -> dict:
    """Decode the JWT payload segment WITHOUT verifying (claims only, never printed)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _wait_for_pod_ready(namespace: str, release: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _kubectl_json(
            "get", "pods", "-n", namespace,
            "-l", f"app.kubernetes.io/instance={release}",
        )
        for pod in (data or {}).get("items", []):
            for cond in pod.get("status", {}).get("conditions", []):
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    return True
        time.sleep(2)
    return False


def _wait_for_replicas(namespace: str, release: str, target: int, timeout: int = 60) -> int:
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        data = _kubectl_json("get", "deployment", release, "-n", namespace)
        if data is not None:
            last = data["spec"].get("replicas", -1)
            if last == target:
                return last
        time.sleep(1)
    return last


# --------------------------------------------------------------------------
# fixture: an agent-runtime release installed through the operator's own API
# --------------------------------------------------------------------------


@pytest.fixture
def agent_runtime_env(app_client, tmp_path):
    """Point the operator at the stub chart + the test ns prefix, then clean up.

    ⚠️ flag 的設定刻意用 try/except：#955 的實作 commit 之前這個欄位還不存在，
    而本檔必須能在 **實作前的樹** 上跑出 RED（TDD 硬 gate）。舊樹上設不進去 ⇒
    測試量到的就是舊行為 ⇒ 在待驗的那條斷言上紅，而不是紅在 fixture 壞掉。
    """
    from src.config import settings

    project_hash = uuid.uuid4().hex[:16]
    chart = _write_stub_chart(tmp_path / "agent-runtime-stub")

    saved = {
        name: getattr(settings, name, None)
        for name in (
            "agent_runtime_chart", "agent_runtime_repo_url",
            "agent_runtime_chart_version", "agent_runtime_repo_username",
            "agent_runtime_repo_password", "namespace_prefix", "odoo_base_url",
            "agent_runtime_skill_bundle_secret_enabled",
        )
    }
    import os
    saved_auth = os.environ.get("AGENT_RUNTIME_AUTH_SECRET_CURRENT")
    os.environ["AGENT_RUNTIME_AUTH_SECRET_CURRENT"] = _TEST_AUTH_SECRET

    settings.agent_runtime_chart = chart
    settings.agent_runtime_repo_url = ""
    settings.agent_runtime_chart_version = ""
    settings.agent_runtime_repo_username = ""
    settings.agent_runtime_repo_password = ""
    settings.namespace_prefix = _TEST_PREFIX
    settings.odoo_base_url = ""
    flag_present = True
    try:
        settings.agent_runtime_skill_bundle_secret_enabled = True
    except (ValueError, AttributeError):  # pre-#955 tree: the flag does not exist yet
        flag_present = False

    namespace = f"{_TEST_PREFIX}pa-{project_hash}"
    release = f"agent-pa-{project_hash}"
    yield {
        "project_hash": project_hash,
        "namespace": namespace,
        "release": release,
        "flag_present": flag_present,
    }

    # teardown —— 走 operator 自己的 DELETE（helm uninstall + delete ns），再兜底刪 ns
    try:
        app_client.delete(f"/api/agent-runtimes/{project_hash}")
    except Exception:  # noqa: BLE001 - teardown must never mask the test verdict
        pass
    subprocess.run(
        ["kubectl", "delete", "namespace", namespace, "--wait=false",
         "--ignore-not-found"],
        capture_output=True,
    )
    for name, value in saved.items():
        if value is not None or hasattr(settings, name):
            try:
                setattr(settings, name, value)
            except (ValueError, AttributeError):
                pass
    if saved_auth is None:
        os.environ.pop("AGENT_RUNTIME_AUTH_SECRET_CURRENT", None)
    else:
        os.environ["AGENT_RUNTIME_AUTH_SECRET_CURRENT"] = saved_auth


def _spawn_body(project_hash: str) -> dict:
    return {
        "project_hash": project_hash,
        "project_id": 4242,
        "channel_id": 7,
        "invoker_user_id": 2,
        "invoker_role": "owner",
        "project_workspace_ids": [11],
    }


def _cold_start(app_client, project_hash: str):
    r = app_client.post("/api/agent-runtimes/spawn-or-attach",
                        json=_spawn_body(project_hash))
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# S10 —— 🔴 the --reuse-values drift regression (計畫 §0.4(C))
# --------------------------------------------------------------------------


def test_reuse_values_upgrade_does_not_revert_skill_bundle_token(
    app_client, agent_runtime_env
):
    """resume 重簽的 token 不得被後續的 ``helm upgrade --reuse-values`` 蓋回舊值。

    這支是「新設計比舊設計好」的最強單一證據，也是最容易做錯的一支，所以判準刻意
    寫成 **shape-agnostic**：只問「pod 現在拿得到的那顆 token 是哪一顆」，不問它是
    literal 還是 secretKeyRef。

    舊設計（token 進 helm values ＋ resume 用 ``kubectl set env`` 就地改 Deployment）：
    ``--reuse-values`` 拿 install 當時存的 values 重新 render pod template ⇒ 覆蓋回
    install 那顆（可能早已過期）⇒ **本測試紅在最後那條斷言**。
    新設計（token 只住 Secret，values 不含它）：helm 再怎麼 re-render 都不動 Secret。
    """
    namespace = agent_runtime_env["namespace"]
    release = agent_runtime_env["release"]
    project_hash = agent_runtime_env["project_hash"]

    _cold_start(app_client, project_hash)
    install_digest = _effective_token_digest(namespace, release)

    r = app_client.post(f"/api/agent-runtimes/{project_hash}/reap")
    assert r.status_code == 200, r.text
    assert _wait_for_replicas(namespace, release, 0) == 0

    r = app_client.post(
        f"/api/agent-runtimes/{project_hash}/resume",
        json={"project_id": 4242, "project_workspace_ids": [11]},
    )
    assert r.status_code == 200, r.text
    resume_digest = _effective_token_digest(namespace, release)
    assert resume_digest != install_digest, (
        "resume 必須重簽一顆新 token（jti 每次不同），否則本測試後半失去鑑別力"
    )

    # sync-workspaces 的 leg-1 就是 `helm upgrade --reuse-values`（agent_runtimes.py
    # 的 sync_workspaces）。replicas=1 時不會走 reaped skip 分支。
    r = app_client.post(
        f"/api/agent-runtimes/{project_hash}/sync-workspaces",
        json={"project_workspace_ids": [11, 12], "require_release": True},
    )
    assert r.status_code == 200, r.text

    after_sync_digest = _effective_token_digest(namespace, release)
    assert after_sync_digest == resume_digest, (
        "helm upgrade --reuse-values 把 pod 的 SKILL_BUNDLE_TOKEN 蓋回 install 當時"
        "那顆（#955 §0.4(C) drift）。install/resume/after-sync 三個 sha256 摘要："
        f"{install_digest[:12]}… / {resume_digest[:12]}… / {after_sync_digest[:12]}…"
        "（⛔ 只印摘要前綴，不印 token）"
    )


# --------------------------------------------------------------------------
# S2–S6 —— cold-start 的三面 closure
# --------------------------------------------------------------------------


def test_cold_start_injects_token_via_secret_key_ref_only(
    app_client, agent_runtime_env
):
    """cold-start：token 只以 Secret 存在，三個明文面全關。"""
    namespace = agent_runtime_env["namespace"]
    release = agent_runtime_env["release"]
    project_hash = agent_runtime_env["project_hash"]

    _cold_start(app_client, project_hash)

    # INV-D: Secret 存在、key 正確、內容是本單那顆 token
    secret_name = f"{release}-skill-bundle"
    token = _secret_token(namespace, secret_name, _ENV_KEY)
    assert token, f"Secret {secret_name}/{_ENV_KEY} 不存在或為空"
    claims = _jwt_claims(token)
    assert claims["token_use"] == "skill_bundle"
    assert claims["project_hash"] == project_hash
    from src.config import settings
    assert claims["exp"] - claims["iat"] == settings.skill_bundle_token_ttl_seconds

    # INV-C: Deployment 的該 env 項只有 secretKeyRef，沒有 literal value
    entry = _deployment_env_entry(namespace, release)
    assert entry is not None
    assert "value" not in entry, (
        f"{_ENV_KEY} 仍有 literal env value（明文落 Deployment/Pod spec）"
    )
    ref = entry["valueFrom"]["secretKeyRef"]
    assert ref["name"] == secret_name
    assert ref["key"] == _ENV_KEY
    assert ref.get("optional") is False, (
        "secretKeyRef 必須 optional: false —— 沒有 token 時 skill-bundle fetch 一律 401、"
        "pod 照起、行為錯且無 render error 可循（optional 漏轉比 required 更陰）"
    )

    # INV-C（全文）：Deployment 與 Pod 的 YAML 全文都找不到 token 明文
    for kind in ("deployment", "pods"):
        raw = subprocess.run(
            ["kubectl", "get", kind, "-n", namespace, "-o", "yaml"],
            capture_output=True, text=True,
        ).stdout
        assert token not in raw, f"token 明文出現在 {kind} 的 YAML 全文裡"
        assert base64.b64encode(token.encode()).decode() not in raw, (
            f"token 的 base64 出現在 {kind} 的 YAML 全文裡"
        )

    # INV-A: helm 存的 values 與 manifest 都不含 token
    import os
    helm = os.environ.get("HELM_BINARY", "helm")
    for sub in (["get", "values", release, "-n", namespace, "--all"],
                ["get", "manifest", release, "-n", namespace]):
        out = subprocess.run([helm, *sub], capture_output=True, text=True).stdout
        assert token not in out, f"token 出現在 `helm {' '.join(sub[:2])}` 的輸出裡"

    # INV-D 硬證據：optional:false 下 pod 起得來 ⇒ secretKeyRef 真的解得開
    assert _wait_for_pod_ready(namespace, release), (
        "pod 沒有進入 Ready —— optional:false 的 secretKeyRef 解不開會停在 "
        "CreateContainerConfigError"
    )


def test_resume_rotates_secret_without_plaintext_in_spec(
    app_client, agent_runtime_env
):
    """resume：Secret 內容換新、Deployment 仍無明文、且不再用 kubectl set env。"""
    namespace = agent_runtime_env["namespace"]
    release = agent_runtime_env["release"]
    project_hash = agent_runtime_env["project_hash"]
    secret_name = f"{release}-skill-bundle"

    _cold_start(app_client, project_hash)
    before = _secret_token(namespace, secret_name, _ENV_KEY)
    assert before, f"Secret {secret_name}/{_ENV_KEY} 不存在或為空"
    before_digest = hashlib.sha256(before.encode()).hexdigest()

    r = app_client.post(f"/api/agent-runtimes/{project_hash}/reap")
    assert r.status_code == 200, r.text
    assert _wait_for_replicas(namespace, release, 0) == 0

    r = app_client.post(
        f"/api/agent-runtimes/{project_hash}/resume",
        json={"project_id": 4242, "project_workspace_ids": [11]},
    )
    assert r.status_code == 200, r.text

    after = _secret_token(namespace, secret_name, _ENV_KEY)
    assert after, "resume 之後 Secret 消失或為空"
    after_digest = hashlib.sha256(after.encode()).hexdigest()
    assert after_digest != before_digest, (
        f"resume 沒有換掉 Secret 裡的 token（sha256 皆為 {before_digest[:12]}…）"
    )
    assert _jwt_claims(after)["token_use"] == "skill_bundle"

    # env 項仍是 secretKeyRef —— resume 不得退回 `kubectl set env`（那會把值寫進
    # Deployment spec 的 literal env[].value，也會落 kubectl argv）
    entry = _deployment_env_entry(namespace, release)
    assert entry is not None and "value" not in entry, (
        f"resume 之後 {_ENV_KEY} 又有 literal env value —— resume 路徑仍在走 "
        "kubectl set env"
    )
    assert _wait_for_replicas(namespace, release, 1) == 1
    assert _wait_for_pod_ready(namespace, release), "resume 後 pod 沒有重新 Ready"


# --------------------------------------------------------------------------
# 陽性對照 —— optional:false 真的 fail-loud
# --------------------------------------------------------------------------


def test_missing_secret_fails_loud_instead_of_starting_tokenless(
    app_client, agent_runtime_env
):
    """Secret 不見時 pod 必須起不來，而不是帶著空 token 起來。

    這支是上面那些「pod Ready ⇒ secretKeyRef 解得開」斷言的**陽性對照**：沒有它，
    「Ready」可能只是因為根本沒有人在檢查 Secret（例如 chart 誤用 ``optional: true``，
    或 env 項根本沒渲染出來）。刻意製造缺席、確認叢集**真的**拒絕，才證明前面那些
    綠燈量到的是我以為的那條性質。

    這也正是選 ``optional: false`` 的代價與收益：缺 Secret 是 loud 的
    ``CreateContainerConfigError``（服務中斷、一眼看得到），而不是 pod 健康、行為錯、
    每一次 skill-bundle fetch 都 401 卻沒有任何 render error 可循。
    """
    namespace = agent_runtime_env["namespace"]
    release = agent_runtime_env["release"]
    project_hash = agent_runtime_env["project_hash"]

    _cold_start(app_client, project_hash)
    assert _wait_for_pod_ready(namespace, release), "前提不成立：pod 一開始就沒 Ready"

    # 拿掉 Secret，再殺掉 pod 讓 ReplicaSet 重建一顆
    subprocess.run(
        ["kubectl", "delete", "secret", f"{release}-skill-bundle", "-n", namespace],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["kubectl", "delete", "pod", "-n", namespace,
         "-l", f"app.kubernetes.io/instance={release}", "--wait=false"],
        capture_output=True, check=True,
    )

    # ⚠ ``ContainerCreating`` 是**過渡**狀態，每顆新 pod 都會先經過它。第一個非空的
    # waiting.reason 就收手 ⇒ 一定讀到它 ⇒ 這支測試永遠紅、而且紅得像是守線失效。
    # 所以要輪詢到目標狀態出現（或逾時），並回報最後看到的那個。
    deadline = time.time() + 90
    reason = None
    while time.time() < deadline:
        data = _kubectl_json(
            "get", "pods", "-n", namespace,
            "-l", f"app.kubernetes.io/instance={release}",
        )
        for pod in (data or {}).get("items", []):
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            for st in pod.get("status", {}).get("containerStatuses", []) or []:
                waiting = (st.get("state") or {}).get("waiting") or {}
                if waiting.get("reason"):
                    reason = waiting["reason"]
        if reason == "CreateContainerConfigError":
            break
        time.sleep(2)

    assert reason == "CreateContainerConfigError", (
        "Secret 缺席時 pod 必須停在 CreateContainerConfigError（optional: false 的 "
        f"fail-closed 語意）；實得 waiting.reason={reason!r}。若這裡是 None 或 pod "
        "照樣 Ready，代表 secretKeyRef 根本沒被強制 ⇒ 本檔其他綠燈都失去意義"
    )
