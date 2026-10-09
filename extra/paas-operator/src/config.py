"""Application configuration."""
import os
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Security
    api_key: str = ""

    # Service Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # Kubernetes
    namespace_prefix: str = "paas-ws-"

    # Helm
    helm_binary: str = "/usr/local/bin/helm"
    helm_timeout: int = 300  # seconds

    # #1552：helm／kubectl 子行程都跑在 operator 同一個容器（同一個 memory cgroup）。
    # 2026-10-07 沙盒實測（同 image 3c037f9、limit 512Mi）：一個 helm install／upgrade
    # （含 --dry-run）因為要下載並解析整份 /openapi/v2（本叢集 213 個 CRD，約 11.6MB）
    # RSS 約 200MiB；同時 3 個就把容器 OOMKilled，進行中的請求全斷。
    #
    # HELM_DISABLE_OPENAPI_VALIDATION：install／upgrade 加 --disable-openapi-validation。
    # 對 k8s ≥ 1.27 是**純省資源**：helm 3.13 在 API server 支援 fieldValidation 時本來就
    # 不做 client 端 schema 驗證，只是照樣把 schema 抓下來解析（沙盒實測：開或關，未知欄位
    # 都只有同樣的 server 端 warning、照樣安裝）。關掉後每個 upgrade 約 45MiB、快一倍。
    # 只有明確 false 才回到舊行為（kill switch）。
    helm_disable_openapi_validation: bool = True
    # HELM_MAX_CONCURRENT_OPERATIONS：同時執行的 helm install／upgrade／rollback／uninstall
    # 上限（含 dry-run）；超過的請求排隊。查詢類（list／status／history／get）不受限。
    helm_max_concurrent_operations: int = Field(default=3, ge=1)
    # HELM_QUEUE_TIMEOUT_SECONDS：排隊最多等多久；等不到就回 503（error_code
    # operator_busy，什麼都沒套用，可以直接重送）。平台 helm 類請求的 HTTP 預算是 120s
    # （HELM_OPERATION_TIMEOUT），這裡要留給真正的 helm 作業。
    helm_queue_timeout_seconds: int = Field(default=45, gt=0)

    # CORS
    cors_origins: str = ""  # Comma-separated list of allowed origins, empty = block all external

    # PVC Lifecycle
    pvc_grace_period_days: int = 7  # Days before orphaned PVCs are deleted

    # #1415：開通前的叢集儲存（Longhorn）排程額度預檢（src/services/storage_capacity.py）。
    # 只有「確定放不下」才回 409；讀不到 Longhorn／渲染失敗一律放行。這個開關是 kill switch
    # （env STORAGE_CAPACITY_PRECHECK_ENABLED=false ⇒ 開通行為回到 #1415 之前）。
    storage_capacity_precheck_enabled: bool = True
    # 預檢用 `helm install --dry-run` 渲染（含 OCI 拉 chart）的 subprocess 上限；逾時＝放行。
    # 平台 install_release 的 HTTP 預算是 120s（HELM_OPERATION_TIMEOUT），這裡留給真正的 install。
    storage_capacity_render_timeout_seconds: int = Field(default=45, gt=0)
    longhorn_namespace: str = "longhorn-system"

    # #1434：租戶 ns LimitRange 的 ephemeral-storage 預設（只補「容器沒宣告」的那一格；
    # chart 自己有設就以 chart 為準）。容器寫 overlay（/、/tmp、/var/tmp）與容器 log 都
    # 算 ephemeral-storage，超過 limit 時 kubelet 只驅逐該 Pod，不會把節點系統碟寫到
    # DiskPressure 連坐同節點其他租戶與正式站。request 很小是刻意的：它會計入節點
    # allocatable 的排程帳（節點系統碟約 100G），只保留最低額度。
    tenant_ephemeral_storage_default_request: str = "128Mi"
    tenant_ephemeral_storage_default_limit: str = "4Gi"

    # #1441／#1436／#514「工作區＝自己的區域網路」（src/services/tenant_baseline.py）。
    # 兩個開關都是 kill switch，預設關＝行為與本變更之前完全相同，operator 可以先發版；
    # 由 Fleet values（paas-operator chart config.tenantBaselineEgress／tenantPodSecurity）
    # 在既有 namespace 回填並驗證之後才翻開。開了之後，每次建立／重跑 create_namespace
    # 與 POST /api/namespaces/{name}/baseline-netpol 都會套用。
    #
    # TENANT_BASELINE_EGRESS_ENABLED：租戶 ns 加 NetworkPolicy paas-baseline-egress
    # （同 ns 全通＋kube-dns 53＋公網排除私網／metadata／CGNAT／多播／240/4）。
    tenant_baseline_egress_enabled: bool = False
    # TENANT_POD_SECURITY_ENABLED：租戶 ns 加 pod-security.kubernetes.io/enforce=<level>
    # （＋warn／audit），例外清單內的 ns 不加、並移除殘留的 PSA 標籤。
    tenant_pod_security_enabled: bool = False
    tenant_psa_enforce_level: str = "baseline"   # env TENANT_PSA_ENFORCE_LEVEL
    tenant_psa_warn_level: str = "restricted"    # env TENANT_PSA_WARN_LEVEL（只警告）
    tenant_psa_audit_level: str = "restricted"   # env TENANT_PSA_AUDIT_LEVEL（只稽核）
    # 釘 Pod Security Standards 版本＝叢集的 k8s 小版號（woow-k3s v1.34），避免升 k8s 時
    # 標準悄悄變嚴把既有 pod 擋掉；升級叢集時一起檢查再改。
    tenant_psa_version: str = "v1.34"            # env TENANT_PSA_VERSION
    # 逗號分隔。只能放管理者自己的 ns，每加一個都要寫理由（見 tenant_baseline 的註解）。
    psa_exempt_namespaces: str = "paas-ws-ab6564da"  # env PSA_EXEMPT_NAMESPACES

    @field_validator("tenant_psa_enforce_level", "tenant_psa_warn_level",
                     "tenant_psa_audit_level")
    @classmethod
    def _validate_psa_level(cls, v: str) -> str:
        allowed = {"", "privileged", "baseline", "restricted"}
        if v not in allowed:
            raise ValueError(f"PSA level must be one of {sorted(allowed)}; got {v!r}")
        return v

    # Private Registry (for tenant namespace imagePullSecrets)
    registry_server: str = ""
    registry_username: str = ""
    registry_password: str = ""
    registry_secret_name: str = "paas-registry"

    # Agent Runtime (per-project pod) — design §2.11
    # Chart lives in a PRIVATE Gitea classic Helm repo (gap C decision): operator
    # fetches it statelessly via `helm install --repo <url> --version <ver>
    # --username/--password` (no `helm repo add`; OCI is unavailable in this env).
    # agent_runtime_chart is the bare chart NAME; repo URL / version / read creds
    # are separate env-overridable settings, set by the operator deployment (gap A).
    #
    # RESOLVED (#955, 2026-08-29) — the charts side reported "the platform pins the
    # OCI channel"; measured against the live stg operator, that is NOT true for
    # agent-runtime. `kubectl -n paas-stg-system get deploy paas-operator-stg` shows
    # `AGENT_RUNTIME_REPO_URL=https://git-prod.woowtech.io/api/packages/woow-paas/helm`
    # ⇒ stg resolves this chart through the CLASSIC Gitea Helm repo (`--repo`), and
    # `agent_runtime_chart` stays the bare name. The OCI ref is a real, separate
    # publish channel (the marketplace charts do use it, which is why
    # `_ensure_oci_login` exists) — it just is not the one agent-runtime travels on.
    # ⇒ NOTHING to change here, and the chart-version pin below must be satisfiable on
    # the CLASSIC repo. Verified 2026-08-29: 0.5.0 is present in BOTH channels.
    # ⚠️ The "OCI is unavailable in this env" clause above is a 2026-05 statement that
    # is now false in general (marketplace charts are OCI); it survives only as the
    # reason agent-runtime was wired to `--repo`. Which channel any given environment
    # uses is still decided by its Fleet CD values in paas-app-bundle-cd, not here.
    agent_runtime_chart: str = "agent-runtime"  # chart NAME (resolved via --repo)
    agent_runtime_repo_url: str = ""  # classic Helm repo URL (--repo), e.g. http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm
    # Chart version passed as `--version`. This is the CHART version (e.g. 0.5.0),
    # NOT the image tag. Empty = omit the flag = whatever helm calls latest in the
    # index (a floating target — that is why we pin).
    #
    # Pinned to 0.5.0 for F-GC-1 / #955: 0.5.0 is the first chart version that
    # renders `SKILL_BUNDLE_TOKEN` from `valueFrom.secretKeyRef`
    # (woow-paas-charts PR #78, 0.4.5 -> 0.5.0 minor). It is dual-mode with
    # `skillBundleToken.fromSecret` defaulting to FALSE, so 0.5.0 on its own is
    # behaviour-identical to 0.4.5 — pinning it does NOT flip anything.
    #
    # 🔴 ORDERING: 0.5.0 must be PUBLISHED before this default can resolve. A pin at
    # an unpublished version makes `helm install` fail for any environment that does
    # not override this env — so this default and the chart publish are one
    # lock-step, and #955's WIP gate exists to hold that line.
    # 🔴 AUTHORITY: the per-environment value is NOT decided here. It is set on the
    # operator Deployment env (`AGENT_RUNTIME_CHART_VERSION`) from the cross-repo
    # paas-app-bundle-cd Fleet CD values, which override this default. This constant
    # is the fallback for environments that set nothing (local/dev) and the record of
    # what the code expects.
    agent_runtime_chart_version: str = "0.5.0"  # env AGENT_RUNTIME_CHART_VERSION
    agent_runtime_repo_username: str = ""  # Gitea read:package username (--username); private repo auth
    agent_runtime_repo_password: str = ""  # Gitea read:package token (--password); private repo auth
    agent_runtime_image_tag: str = "1.4.4"  # pod image tag injected into values; prod/stg override via AGENT_RUNTIME_IMAGE_TAG. 1.0.0 and 1.4.2 were deleted from JCR (2026-09-30 cleanup, #1273) — keep this default on a tag that exists
    odoo_base_url: str = ""  # ODOO_BASE_URL injected into pod env (skill-bundle fetch); stg = internal svc, e.g. http://stg-odoo-dev-sandbox-odoo.dev-sandbox-stg.svc.cluster.local:8069
    # F-PILOT-10 (A): open the agent pod's egress to the EXTERNAL LLM endpoint
    # (e.g. eugene-minimax.woowtech.io:443). The chart gates this behind
    # `agentRuntime.externalEgress.enabled`; default OFF (closed egress = secure),
    # explicit per-environment opt-in via env AGENT_RUNTIME_EXTERNAL_EGRESS_ENABLED.
    agent_runtime_external_egress_enabled: bool = False  # forward to chart when real-LLM pods need outbound
    # Task 1a: probe SHAPE selector forwarded to the chart's top-level `probe.style`
    # when set. Empty (default) => operator forwards NOTHING => chart keeps its own
    # default (probe.style=tcpSocket), so image <= 1.1.x and existing pods are
    # unaffected. Set "httpGet" (per-environment opt-in via env
    # AGENT_RUNTIME_PROBE_STYLE) once the pod image is >= 1.2.0, which serves
    # /livez + /readyz; chart 0.2.0 fail-closes on any value other than
    # tcpSocket/httpGet. Mirrors the externalEgress forward pattern.
    agent_runtime_probe_style: str = ""  # "" = no forward; "httpGet" for image >= 1.2.0
    # skill bash sidecar (Drop B): forward the chart's `skillRunner.enabled` so the
    # agent-runtime pod cold-starts WITH the bash-runner sidecar. Default OFF — the
    # sidecar requires the Drop B agent-runtime image (bash_bridge daemon) + the
    # sidecar-aware chart; enabling it on an older image would crashloop. Explicit
    # per-environment opt-in via env AGENT_RUNTIME_SKILL_RUNNER_ENABLED, flipped at
    # the coordinated Drop B land. Mirrors the externalEgress forward pattern.
    agent_runtime_skill_runner_enabled: bool = False  # forward skillRunner.enabled to chart
    # issue #155 stopgap: SKILL_BUNDLE_TOKEN is signed at pod spawn/resume and baked
    # into the pod env; its exp was hard-coded to 3600s and no in-repo reap trigger
    # exists, so a pod outliving 1h 401'd forever on skill-bundle fetch. Make the TTL
    # config-driven; default 7 days (604800s) so a long-lived pod keeps a valid token.
    # The real fix (401 -> refresh) is a separate design. Env-overridable per
    # environment via SKILL_BUNDLE_TOKEN_TTL_SECONDS, set by the operator deployment
    # (the operator's own Deployment env lives cross-repo in paas-app-bundle-cd Fleet
    # CD values — NOT rendered by any chart in this repo).
    # SECURITY (F-GC-1): re-reviewed 2026-08-29 under #955 — CONCLUDED, severity
    # minor -> **medium**. The premise behind the original minor ("token lives ≤1h,
    # so the plaintext pod-spec exposure window is ≤1h") died when this TTL became
    # 7 days: a 168x wider window. It stays below major because the population is
    # limited to holders of in-cluster pod-read rights and the blast radius is a
    # single tenant. The fix is the secretKeyRef path gated by
    # `agent_runtime_skill_bundle_secret_enabled` below — not a TTL change (401 ->
    # refresh remains a separate design, issue #155).
    # gt=0 rejects non-positive TTLs (negative / 0 would sign an already-expired
    # token → every skill-bundle fetch 401s); fail closed at config load.
    skill_bundle_token_ttl_seconds: int = Field(
        default=604800, gt=0  # 7 days; env SKILL_BUNDLE_TOKEN_TTL_SECONDS
    )

    # F-GC-1 (#955): carry SKILL_BUNDLE_TOKEN in a per-release K8s Secret and let the
    # chart inject it via `valueFrom.secretKeyRef`, instead of a literal pod-spec env
    # value. Default False = today's behaviour byte-for-byte, so operator and chart can
    # land independently: a new operator against the old chart, or the new (dual-mode)
    # chart against the old operator, both keep working. The ONE bad combination
    # (flag on + a chart that has no secretKeyRef branch) is reachable only by flipping
    # this flag, which is explicit, per-environment and instantly reversible — it is
    # the kill switch. Flip only after `AGENT_RUNTIME_CHART_VERSION` already points at
    # a chart that renders the secretKeyRef branch. Once prod has been stable on it,
    # this flag and the plain-env branch get removed together (see #955 rollout step 7).
    agent_runtime_skill_bundle_secret_enabled: bool = False  # env AGENT_RUNTIME_SKILL_BUNDLE_SECRET_ENABLED

    # Cloudflare Tunnel Integration
    cloudflare_enabled: bool = False  # Set to true to enable automatic route management
    cloudflare_api_token: str = ""  # API token with Cloudflare Tunnel:Edit and DNS:Edit permission
    cloudflare_account_id: str = ""  # Cloudflare account ID
    cloudflare_tunnel_id: str = ""  # Tunnel ID from Zero Trust dashboard
    cloudflare_zone_id: str = ""  # Zone ID for DNS management (found in domain overview)
    cloudflare_domain: str = ""  # Base domain (e.g., woowtech.io)
    # #279 O4 leg-2 baseline NP: the dedicated namespace hosting THIS env's Cloudflare
    # Tunnel connector pod. Grounded on woow-k3s: prod=`paas-cloudflare-system`,
    # stg=`paas-stg-cloudflare-system` (hostNetwork=False, dedicated → a namespaceSelector
    # on it is safe + env-safe by construction). The CS-ns baseline NP allows ingress
    # from this ns so the user-facing CF web path survives the ns-wide default-deny.
    # Empty => derive from PAAS_ENV (see helm._cloudflared_namespace); a genuine dev/local
    # run with no CF connector renders no such allow.
    cloudflared_namespace: str = ""  # env CLOUDFLARED_NAMESPACE
    # woow_paas_platform #1563：停權說明頁（paas-app-bundle-cd 的 paas-suspended-page／-stg）。
    # Service 固定放在上面那個 cloudflared namespace（同 ns 直連、不必另開 NetworkPolicy）。
    # `POST /api/releases/{ns}/{name}/route/park` 的目標**只**從這兩個設定組
    # （http://<service>.<cloudflared ns>.svc.cluster.local:<port>），不收呼叫端傳入——呼叫端
    # 無法拿 park 把某個 hostname 指到任意位址。預設值對齊 CD 的 Service，所以不需要 chart
    # 新增 env；空字串＝關閉 park（409 suspended_page_not_configured），unpark 照常可用。
    suspended_page_service: str = "paas-suspended-page"  # env SUSPENDED_PAGE_SERVICE
    suspended_page_port: int = Field(default=8080, gt=0, lt=65536)  # env SUSPENDED_PAGE_PORT

    # woow_paas_platform #1540：停權／刪除後的限時唯讀資料取回入口（src/services/data_retrieval.py）。
    # image 與停權說明頁、平台 web 的 nginx sidecar 同一顆（叢集已在用，以 digest 釘）；預設值即正式值，
    # 不需要 chart 新增 env。
    data_retrieval_image: str = (
        "docker.io/nginxinc/nginx-unprivileged:1.27-alpine"
        "@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0"
    )  # env DATA_RETRIEVAL_IMAGE
    # 一個入口最長開多久（平台送的 ttl_hours 超過就截到這裡）。
    data_retrieval_max_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)  # env DATA_RETRIEVAL_MAX_TTL_HOURS
    # 已刪除服務的孤兒 PVC 剩不到這麼多分鐘就不開（開了也來不及下載完）。
    data_retrieval_min_remaining_minutes: int = Field(default=60, ge=0)  # env DATA_RETRIEVAL_MIN_REMAINING_MINUTES
    # 逗號分隔的節點名：取回 pod 不排到這些節點（nodeAffinity NotIn）。預設空＝不限制。
    data_retrieval_excluded_nodes: str = ""  # env DATA_RETRIEVAL_EXCLUDED_NODES

    # Deploy environment (L1): prod/stg discriminator stamped onto tenant namespace
    # labels (L2, #279) + future account-level reconciler env filtering. Injected
    # per-environment via the operator Deployment env (cross-repo paas-app-bundle-cd
    # Fleet CD values — NOT a chart in this repo, mirrors the SKILL_BUNDLE_TOKEN_TTL
    # note above). Whitelist-validated fail-closed; consumers omit the label when
    # empty. Do NOT DERIVE the stamp value from namespace_prefix (kept a separate
    # field) — but the F6 startup guard (assert_env_guard) DOES cross-check the two as
    # independent signals to reject an env/prefix swap.
    #
    # F6 (#279 cross-env leak guard) — REVERSES the earlier "operator must NOT crash
    # on an unset env" stance, which was the fail-open hole: the workspace int id in
    # the #279 NP labels is per-Odoo-DB, NOT cluster-unique, and prod+stg share one
    # woow-k3s cluster, so an env-LESS label collides workspace ids across envs (a stg
    # agent reaches a prod CloudService of the same numeric id). An unset PAAS_ENV on a
    # real deploy is therefore fail-OPEN and the operator now FAILS FAST at startup
    # (assert_env_guard, called from main.lifespan) unless the dev/local opt-out is set.
    paas_env: str = Field(default="")  # env PAAS_ENV; "" | "prod" | "stg"
    # Dev/local opt-out for the F6 startup guard: allow an empty PAAS_ENV to boot
    # (single-env dev has no cross-env collision, so an omitted label is safe). Real
    # prod/stg NEVER set this (they set PAAS_ENV). The guard additionally refuses to
    # honor the opt-out when NAMESPACE_PREFIX names a real env (contains stg/prod), so
    # a real overlay that both forgot PAAS_ENV *and* set this can't silently re-open
    # the hole.
    allow_unset_paas_env: bool = Field(default=False)  # env ALLOW_UNSET_PAAS_ENV (dev only)

    # Tunnel ownership claim (#531) — rollout stage switch for the MISSING-claim
    # case only. A claim that is present but does NOT match the tunnel's real
    # Cloudflare name is ALWAYS rejected, on every endpoint, regardless of this
    # flag; there is no stage in which a mismatch is tolerated.
    #
    # False (stage 1, current default) = a request with no claim is allowed
    # through with a WARNING. True (stage 2) = it is rejected.
    #
    # 為什麼預設是 False：operator 與平台 addon 是**兩條獨立發版線**，而送 claim 的
    # 那一半住在平台側。operator 先上時，尚未升版的平台一個 claim 都不會送 ——
    # 預設 True 等於在 operator rollout 的當下一次打爆三個端點（route sync 斷、
    # suspend/resume 斷、teardown 斷 ⇒ tunnel 拆不掉還在計費）。
    #
    # 翻旗的前提（缺一不可，見 PR #531 的 rollout 段）：
    #   1. 送 claim 的平台版本已部署到該環境，且
    #   2. 該環境的 operator log 已不再出現 tunnel_ownership_claim_missing。
    # 條件 2 正是 stage 1 那句 warning 存在的唯一理由 —— 無聲放行就沒有任何訊號
    # 判斷「可以翻了沒」。
    tunnel_ownership_require_claim: bool = Field(
        default=False
    )  # env TUNNEL_OWNERSHIP_REQUIRE_CLAIM

    @field_validator("paas_env")
    @classmethod
    def _validate_paas_env(cls, v: str) -> str:
        allowed = {"", "prod", "stg"}
        if v not in allowed:
            raise ValueError(
                f"PAAS_ENV must be one of {sorted(allowed)} (exact, lowercase, "
                f"no surrounding whitespace); got {v!r}"
            )
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# --- F6 (#279) startup env guard ----------------------------------------------
# The workspace int id baked into the #279 NetworkPolicy labels is per-Odoo-DB, NOT
# cluster-unique, and prod+stg share ONE woow-k3s cluster. So a tenant namespace whose
# #279 labels omit / carry the wrong paas.woowtech.io/env would let a stg agent reach a
# prod CloudService of the same numeric workspace id (cross-env tenant leak). This guard
# fails the operator fast at startup on a PAAS_ENV misconfig, rather than silently
# stamping colliding labels once O4 makes those labels load-bearing.


def _env_hint_from_prefix(namespace_prefix: str) -> str | None:
    """Independent env hint derived from NAMESPACE_PREFIX, used ONLY to cross-check
    PAAS_ENV in :func:`assert_env_guard` (never as the stamp source — that is a
    separate field by design). Grounded in the live operator deployments:
    ``paas-ws-`` = prod, ``paas-ws-stg-`` = stg, ``paas-ws-test*`` = integration/dev.

    Returns ``'dev' | 'stg' | 'prod' | None`` (None = genuinely ambiguous). dev is
    checked first so ``paas-ws-test-op-`` is never mistaken for a real env; the bare
    ``paas-ws-`` default maps to prod (the prod deployment's actual prefix).
    """
    p = namespace_prefix.lower()
    if "test" in p or "dev" in p:
        return "dev"
    if "stg" in p:
        return "stg"
    if "prod" in p or p == "paas-ws-":
        return "prod"
    return None


def assert_env_guard(paas_env: str, namespace_prefix: str, allow_unset: bool) -> None:
    """F6 fail-fast on a PAAS_ENV misconfig that would produce cross-env-colliding
    #279 NP labels. Raises :class:`RuntimeError` to abort boot; returns None if OK.

    - PRESENCE: an empty PAAS_ENV on a real deploy is fail-open → abort, UNLESS the
      dev/local opt-out (``allow_unset``) is set. The opt-out is itself refused when
      NAMESPACE_PREFIX explicitly names a real env (contains ``stg``/``prod``), so a
      real overlay that both forgot PAAS_ENV and set the opt-out can't re-open the hole.
      (Bare ``paas-ws-`` is tolerated on the opt-out path because local/dev commonly
      runs the default prefix — see the documented residual for prod-bare + opt-out.)
    - CORRECTNESS: a set PAAS_ENV that contradicts the prefix's independent env hint
      (e.g. PAAS_ENV=prod on a ``paas-ws-stg-`` deploy, or PAAS_ENV=stg on the bare
      prod prefix) is an env/prefix swap → abort.
    - PREFIX PRESENCE: an empty NAMESPACE_PREFIX makes ``validate_namespace``'s
      ``startswith('')`` always true, disabling the single app-layer tenant boundary
      (RBAC does not enforce the prefix). Abort boot regardless of PAAS_ENV/opt-out.
    """
    if not namespace_prefix:
        raise RuntimeError(
            "NAMESPACE_PREFIX is empty — validate_namespace would accept every "
            "namespace (startswith('') is always true), disabling the single "
            "app-layer tenant boundary. Refusing to start."
        )
    if not paas_env:
        if not allow_unset:
            raise RuntimeError(
                "PAAS_ENV is empty. On the shared prod/stg cluster an env-less #279 "
                "tenant-isolation label collides workspace ids across environments "
                "(cross-env leak). Set PAAS_ENV=prod|stg, or ALLOW_UNSET_PAAS_ENV=true "
                "for dev/local (single-env, no collision)."
            )
        p = namespace_prefix.lower()
        if "stg" in p or "prod" in p:
            raise RuntimeError(
                f"ALLOW_UNSET_PAAS_ENV is set but NAMESPACE_PREFIX={namespace_prefix!r} "
                f"names a real environment — refusing to disable the F6 env guard on a "
                f"real deploy. Set PAAS_ENV explicitly instead."
            )
        return  # genuine dev/local: empty env + non-real-named prefix
    hint = _env_hint_from_prefix(namespace_prefix)
    if hint and hint != "dev" and hint != paas_env:
        raise RuntimeError(
            f"PAAS_ENV={paas_env!r} contradicts NAMESPACE_PREFIX={namespace_prefix!r} "
            f"(env hint {hint!r}) — an env/prefix swap would stamp #279 labels for the "
            f"wrong environment (cross-env leak). Refusing to start."
        )


settings = Settings()
