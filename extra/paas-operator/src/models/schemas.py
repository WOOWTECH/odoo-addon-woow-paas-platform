"""Pydantic schemas for API requests and responses."""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class ReleaseStatus(str, Enum):
    """Helm release status."""

    DEPLOYED = "deployed"
    FAILED = "failed"
    PENDING_INSTALL = "pending-install"
    PENDING_UPGRADE = "pending-upgrade"
    PENDING_ROLLBACK = "pending-rollback"
    SUPERSEDED = "superseded"
    UNINSTALLED = "uninstalled"
    UNINSTALLING = "uninstalling"
    UNKNOWN = "unknown"


class PodPhase(str, Enum):
    """Kubernetes pod phase."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


# Request Models


class ExposeConfig(BaseModel):
    """Configuration for exposing a service via Cloudflare Tunnel."""

    enabled: bool = Field(default=False, description="Enable public access via Cloudflare Tunnel")
    subdomain: Optional[str] = Field(
        None,
        description="Custom subdomain (auto-generated if not provided)",
    )
    service_name: Optional[str] = Field(
        None,
        description="K8s service name to expose (auto-detected if not provided)",
    )
    service_port: Optional[int] = Field(
        None,
        description="Service port to expose (auto-detected if not provided)",
    )


class ReleaseCreateRequest(BaseModel):
    """Request to create a new Helm release."""

    namespace: str = Field(..., description="Target namespace for the release")
    name: str = Field(..., description="Release name")
    chart: str = Field(..., description="Chart reference (repo/chart or path)")
    version: Optional[str] = Field(None, description="Chart version")
    values: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Helm values override"
    )
    sensitive_values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Sensitive Helm values merged in memory only",
    )
    create_namespace: bool = Field(
        default=False, description="Create namespace if not exists"
    )
    expose: Optional[ExposeConfig] = Field(
        default=None,
        description="Configuration for exposing service via Cloudflare Tunnel",
    )


class ReleaseUpgradeRequest(BaseModel):
    """Request to upgrade an existing Helm release."""

    chart: Optional[str] = Field(None, description="Chart reference (if changing)")
    version: Optional[str] = Field(None, description="Chart version")
    values: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Helm values override"
    )
    sensitive_values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Sensitive Helm values merged in memory only",
    )
    reset_values: bool = Field(
        default=False, description="Reset values to chart defaults"
    )
    reuse_values: bool = Field(
        default=True, description="Reuse last release values"
    )
    check_compute_quota: bool = Field(
        default=False,
        description="#1647: also refuse (409 workspace_compute_quota_exceeded) when the "
        "release's pods would certainly not fit the namespace ResourceQuota after the "
        "upgrade. Opt-in per request; absent (older platform) = no check",
    )
    pending_quota: Optional["NamespaceQuotaUpdateRequest"] = Field(
        default=None,
        description="#1647: quota the platform will push right after this check (plan change). "
        "Only honoured by upgrade-preflight; PATCH ignores it.",
    )


#: #1433：``preserve_value_paths`` 每一條的合法形態——點分式、每段是 values 鍵名。
#: 與平台 ``cloud_app_template.admin_username_helm_path`` 的驗證同形；不收萬用字元、
#: 不收陣列索引（憑證通道都是 dict 葉節點或子樹）。
_VALUE_PATH_RE = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$"


class ReleaseRollbackRequest(BaseModel):
    """Request to rollback a Helm release.

    #1433：``helm rollback`` 重放的是目標 revision **整份** stored values——包含
    ``config.sensitive.admin_password`` 等平台管的憑證 ⇒ 租戶換掉（撤銷）的舊密碼會
    跟著復活、目前的密碼失效。帶 ``preserve_value_paths`` 時 operator 改走
    「讀目標 revision 的 values ＋ 用**目前** revision 的值覆寫這些路徑 ＋ helm upgrade
    釘目標 revision 的 chart 版本」，只回復 chart 與一般設定、憑證維持現值。

    不帶（或空清單）＝原本的純 ``helm rollback``，舊平台零行為變化（operator 可先發版）。

    #1526：兩種模式都有 chart 版本下限——目標 revision 的 chart 版本低於目前部署中的
    版本 ⇒ 409（``detail.error_code == "rollback_chart_downgrade"``）。
    """

    revision: Optional[int] = Field(None, description="Target revision number. If not provided, rolls back to the previous revision.")
    chart: Optional[str] = Field(
        default=None,
        description="#1433: chart reference — required when preserve_value_paths is "
        "non-empty (the preserving rollback is a helm upgrade, and Helm does not store "
        "the original chart URL)",
    )
    preserve_value_paths: List[str] = Field(
        default_factory=list,
        max_length=16,
        description="#1433: dot-paths whose CURRENT values must survive the rollback "
        "(e.g. config.sensitive, auth.username). A path absent from the current values "
        "is removed from the target values as well — current state wins, including absence.",
    )
    check_compute_quota: bool = Field(
        default=False,
        description="#1647: same opt-in check as ReleaseUpgradeRequest.check_compute_quota. "
        "With preserve_value_paths it judges the dry-run of that helm upgrade; a plain helm "
        "rollback (#1685) judges the target revision's stored manifest (helm get manifest)",
    )

    @field_validator("preserve_value_paths")
    @classmethod
    def _validate_preserve_paths(cls, paths: List[str]) -> List[str]:
        import re

        for path in paths:
            if not isinstance(path, str) or not re.match(_VALUE_PATH_RE, path):
                raise ValueError(f"invalid value path: {path!r}")
        return paths

    @model_validator(mode="after")
    def _preserve_requires_chart(self) -> "ReleaseRollbackRequest":
        if self.preserve_value_paths and not self.chart:
            raise ValueError("preserve_value_paths requires chart")
        return self


class ReleaseConfigProjectionRequest(BaseModel):
    """僅供平台內部同步；不得要求 credential 或任意子樹。"""
    revision: int = Field(ge=1)
    paths: List[str] = Field(default_factory=list, max_length=128)
    excluded_paths: List[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _validate_projection(self):
        from src.services.config_projection import validate_paths
        validate_paths(self.paths, self.excluded_paths)
        return self


class ReleaseResetConfigRequest(BaseModel):
    """#357：reset admin_password。載體由 ``admin_password_carrier`` 分流（#449）。

    **config_json 路（預設，mcp-server 家族）**：外科式 read-modify-write
    ``/data/config.json``，只替換 ``admin_password`` leaf，保留租戶 GUI 自填的
    connection / tools / mcp_server.env / token_history。比「砍整份 + re-seed」
    少一次完整 PVC 重置；敏感值走 ``sensitive_values``，不落持久化 values；
    main container 不掛 Secret、僅 seed-config 一次性讀取 → 應用永遠讀 config.json。

    **helm_secret 路（hermes-agent / open-design）**：密碼的權威載體是 chart 自己
    的 Secret，pod 以 env / initContainer 讀取，**沒有** ``/data/config.json``。
    走純 helm upgrade + 全部 Deployment rollout restart，不做 config.json exec
    patch —— 對這些 chart 做 exec patch 必定失敗（#449：patch 炸 → 備份未建成 →
    rollback 也炸 → 500，密碼永遠輪替不了）。

    任一子步失敗 → operator 報 5xx、不做半套。chart values 經 --values tmp file
    不印明文，cmdline secret value 走既有 _redact_cmd 遮罩；config_json 路的
    密碼只進 pod stdin、不寫檔、不 log。

    **#461：request 欄位刻意沒有變化，但回應語意變了。** 兩條路都只「下達」
    rollout restart、**不等它完成**（回應新增 ``rollout_awaited: false``）：提交點
    是 helm upgrade（config_json 路再加 config.json 寫入），此後新密碼即權威值、
    不再自動回退，所以等 pod 只是把呼叫端預算綁在 image pull／RWO PVC 上（機率性
    逾時，#461）。**沒有新增 wait 旋鈕**——保留「等」的分支就得同時保留兩套補償
    語意，而「不等 ⇒ 不需要補償 rollout」正是本次收斂的支點。舊平台不讀新欄位，
    只會感受到 200 來得更快 ⇒ 照既有邏輯落庫並 stamp ``upgrading`` ⇒ 安全。
    """

    values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Helm values to merge with the existing release (包含新 "
        "admin_password 走 sensitive_values，不落持久化 values).",
    )
    sensitive_values: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Sensitive Helm values merged in memory only",
    )
    chart: Optional[str] = Field(None, description="Chart reference（如需升版）")
    version: Optional[str] = Field(None, description="Chart version（如需升版）")
    # #411 A 案接通（#449 一併補上）：接通前 client 傳的 reset_values 被 pydantic
    # 靜默忽略，只靠 reuse_values=False 讓 helm 走「無-flag 預設」（chart
    # values.yaml 為 base ≈ reset）。接通後升級為顯式 --reset-values；兩者對
    # helm 的實際語意相同（helm 3 的 default == ResetValues），故對既有呼叫端
    # 是行為中性的向後相容改動。
    reset_values: bool = Field(
        default=False, description="Reset values to chart defaults（顯式 --reset-values）"
    )
    reuse_values: bool = Field(default=True, description="複用最近 release values")
    # #449：密碼載體策略。**預設 config_json ＝ 既有行為**，這是「operator 先發版、
    # 平台後發版」順序安全的支點：舊平台不帶本欄 → 走原 saga，零行為變化。
    admin_password_carrier: Literal["config_json", "helm_secret"] = Field(
        default="config_json",
        description=(
            "密碼載體：config_json＝PVC 內 /data/config.json（mcp-server 家族，"
            "既有 saga）；helm_secret＝chart Secret（hermes-agent / open-design，"
            "純 helm upgrade + rollout restart）。未帶＝config_json。"
        ),
    )


class NamespaceCreateRequest(BaseModel):
    """Request to create a namespace with resource quota."""

    name: str = Field(..., description="Namespace name (must start with paas-ws-)")
    cpu_limit: str = Field(default="2", description="CPU limit (e.g., '2' or '2000m')")
    memory_limit: str = Field(default="4Gi", description="Memory limit")
    storage_limit: str = Field(default="20Gi", description="Storage limit")
    ephemeral_storage_request: Optional[str] = Field(
        default=None,
        description="#1434: requests.ephemeral-storage quota. None (older platform) "
        "keeps the quota free of ephemeral-storage keys",
    )
    ephemeral_storage_limit: Optional[str] = Field(
        default=None,
        description="#1434: limits.ephemeral-storage quota. None keeps the key absent",
    )
    workspace_id: Optional[int] = Field(
        default=None,
        description="Odoo workspace id of the owning CloudService; stamped as the "
        "woow.tw/workspace-id NetworkPolicy label (#279 leg-1/leg-2 tenant isolation)",
    )


class NamespaceQuotaUpdateRequest(BaseModel):
    """#278 A1：整組覆寫 ns ResourceQuota（值由 Odoo 依存活 service 集算好傳入）。"""

    cpu_limit: str = Field(..., description="requests.cpu / limits.cpu")
    memory_limit: str = Field(..., description="requests.memory / limits.memory, e.g. 8Gi")
    storage_limit: str = Field(..., description="requests.storage, e.g. 100Gi")
    ephemeral_storage_request: Optional[str] = Field(
        default=None, description="#1434: requests.ephemeral-storage, e.g. 4Gi")
    ephemeral_storage_limit: Optional[str] = Field(
        default=None, description="#1434: limits.ephemeral-storage, e.g. 80Gi")


# #1647：``ReleaseUpgradeRequest.pending_quota`` 以字串前向參照上面這個 model。
ReleaseUpgradeRequest.model_rebuild()


class BaselineNetpolBackfillRequest(BaseModel):
    """#279 P5 backfill: re-apply the baseline NP bundle + re-stamp managed labels on
    a PRE-EXISTING CloudService namespace created before O4/O1."""

    workspace_id: int = Field(
        ..., description="Odoo workspace id of the owning CloudService (required — the "
        "baseline NP4 + leg-1 egress selectors key on woow.tw/workspace-id)",
    )


class RestampLabelsRequest(BaseModel):
    """#279 P5 backfill: re-stamp managed labels (paas-managed + env + optional
    workspace-id / reach-ws-*) on a pre-existing namespace — add-only (never deletes a
    reach label). Chiefly adds the env label a legacy agent ns lacks so it matches
    NP4's env predicate once #280 removes the chart from-less."""

    workspace_id: Optional[int] = Field(default=None)
    reach_workspace_ids: List[int] = Field(default_factory=list)


class TunnelBareCreateRequest(BaseModel):
    """Prototype: create only; no token, ingress, DNS or Access mutation."""

    model_config = {"extra": "forbid"}
    name: str = Field(..., min_length=1, max_length=100)
    expected_account_id: str = Field(..., min_length=1, max_length=128)


class TunnelBareCreateResponse(BaseModel):
    """Non-secret anchor returned immediately after provider create."""

    tunnel_id: str
    tunnel_name: str
    provider_account_id: str


class TunnelCreateRequest(BaseModel):
    """Request to create a new dedicated Cloudflare Tunnel."""

    name: str = Field(
        ...,
        description="Tunnel name (e.g., 'smarthome-livingroom')",
    )
    hostname: Optional[str] = Field(
        None,
        description="Public hostname for the tunnel. Omit to create a bare "
        "tunnel (catch-all 404 ingress, no DNS record).",
    )
    service_url: str = Field(
        default="http://localhost:8123",
        description="Backend service URL the tunnel forwards to",
    )


class TunnelConfigUpdateRequest(BaseModel):
    """Request to update a dedicated tunnel's single-hostname ingress config.

    #769: these two fields used to be bare ``str`` parameters on the endpoint,
    which FastAPI binds as **required query params** — while the only caller
    (the Odoo platform client) sends them as a JSON body, so every call was a
    422. Both sides' tests stayed green throughout (the operator suite never hit
    this endpoint; the platform test mocks the client). Declaring them here puts
    the endpoint back on the same shape as its neighbours
    (``TunnelCreateRequest`` / ``TunnelRoutesSyncRequest``).
    """

    hostname: str = Field(
        ...,
        description="Public hostname (e.g., 'ha-xxx.woowtech.io')",
    )
    service_url: str = Field(
        ...,
        description="Target service URL or 'http_status:NNN' (503 = suspended)",
    )
    #: #531 ownership claim —— 與 ``TunnelRoutesSyncRequest.tunnel_name`` 同名同義。
    #: ⚠ 這條 wire 由 repo-lint 的 ``tunnel-config-wire-shape``（#769）跨層對帳，
    #: 加/改欄位要同步動 ``TUNNEL_CONFIG_FIELDS`` **以及該 gate 的自檢合成輸入**
    #: （自檢會逐欄位跑，漏改會讓 gate 自陳「分類器壞掉」而不是漏判）。
    tunnel_name: Optional[str] = Field(
        None,
        description=(
            "Ownership claim: the caller's own record of this tunnel's Cloudflare "
            "name. Verified against the live tunnel — see _assert_tunnel_ownership."
        ),
    )


# Response Models


class ContainerWaitingInfo(BaseModel):
    """#1417：Pod 卡在哪個容器、為什麼（``state.waiting``）。

    先看 initContainerStatuses 再看 containerStatuses；``PodInitializing``／
    ``ContainerCreating`` 這類「只是還沒輪到」的 reason 排在有實質內容的 reason
    （``ImagePullBackOff``、``CrashLoopBackOff``…）之後。
    """

    container: str
    init_container: bool = False
    reason: str
    message: Optional[str] = None  # 截短過（見 KubernetesService.DIAGNOSTIC_TEXT_LIMIT）


class PodWarningEvent(BaseModel):
    """#1417：這顆 Pod 最近一筆 Warning 事件（``FailedAttachVolume``、``FailedScheduling``…）。

    #1647：也用於 workload 的 controller 層事件（Deployment／ReplicaSet／StatefulSet 的
    ``FailedCreate``），見 ``WorkloadInfo.last_warning``。
    """

    reason: str
    message: Optional[str] = None  # 截短過
    count: Optional[int] = None
    last_seen: Optional[str] = None  # RFC 3339（lastTimestamp／eventTime 原值）


class PodInfo(BaseModel):
    """Information about a pod."""

    name: str
    phase: PodPhase
    ready: str  # e.g., "1/1"
    restarts: int
    age: str
    # 舊呼叫端不必供值；讓平台辨識 phase 仍為 Running 的收尾 Pod。
    deletion_timestamp: Optional[str] = None
    # ---- #1263／#1251 Q19：Pod 身分證據（label＋直接 owner）---------------------
    # 語意沿用 #1129 ``validate_fallback_pod``，不追到 Deployment UID。None＝該
    # label／controller owner 不存在；「operator 有沒有提供」看 status 回應的
    # ``pod_identity_included``，不要從 None 推斷。
    component: Optional[str] = None  # label app.kubernetes.io/component
    task: Optional[str] = None  # label woow.paas/task
    owner_kind: Optional[str] = None  # controller=true 那筆 ownerReference 的 kind
    # #1222 升級提早收斂：metadata.creationTimestamp 原值（RFC 3339 UTC）。``age`` 是
    # 人讀字串且隨時間變，不能拿來判斷「Pod 建立於某時刻之後」。
    creation_timestamp: Optional[str] = None
    # #1408：主要容器（``spec.containers``，不含 init）的 image 參照，順序同 spec。
    # 舊 operator 不帶這個 key ⇒ 平台看到 None 就跳過 Redeploy 後的 image 核對。
    images: Optional[List[str]] = None
    # ---- #1417：未就緒 Pod 的診斷（選填；舊平台忽略即可）----------------------------
    # 部署逾時訊息要說得出「卡在哪」：Pending 的 Pod 是沒排程、磁碟掛不上，還是拉不到
    # image。None＝沒有可報的（容器都沒在 waiting／沒有 Warning 事件／事件查詢失敗），
    # 不是錯誤。``last_warning`` 只在 status 端點、且這顆 Pod 未就緒時才查。
    waiting: Optional[ContainerWaitingInfo] = None
    last_warning: Optional[PodWarningEvent] = None


class WorkloadReplicaFailure(BaseModel):
    """#1647：workload 的 ``ReplicaFailure=True`` condition（controller 建不出 Pod 的原因）。

    Deployment 在 ReplicaSet 建 Pod 被拒（quota、admission…）時會帶這個 condition；
    ``reason`` 通常是 ``FailedCreate``，``message`` 是 API server 的拒絕原文。
    """

    reason: str
    message: Optional[str] = None  # 截短過（見 KubernetesService.WORKLOAD_DIAGNOSTIC_TEXT_LIMIT）
    last_transition: Optional[str] = None  # RFC 3339（lastTransitionTime 原值）


class WorkloadInfo(BaseModel):
    """#1647：release 的一個 Deployment／StatefulSet 的副本數與「建不出 Pod」的證據。

    存在的理由：Pod 被 quota 擋下時**根本沒有 Pod 物件**（2026-10-10 事故：ReplicaSet
    ``FailedCreate: exceeded quota``），``pods`` 清單只會少一顆、看不出「本來該有」。
    平台要發現「期望有 Pod 但一顆都沒有」，就得看 controller 這一層。

    ``desired_replicas`` 是 ``spec.replicas``（缺值當 1，同 API server 的預設）；
    ``replicas`` 是 controller 目前實際擁有的 Pod 數（``status.replicas``，缺值當 0）。
    ``replica_failure``／``last_warning`` 為 None＝沒有可報的（或事件查詢失敗），不是錯誤；
    ``last_warning`` 只在這個 workload 副本不足時才查。
    """

    kind: str  # Deployment | StatefulSet
    name: str
    desired_replicas: int
    replicas: int = 0
    ready_replicas: int = 0
    updated_replicas: int = 0
    # Deployment 取 spec.strategy.type（Recreate／RollingUpdate），StatefulSet 取
    # spec.updateStrategy.type；Recreate 被擋時舊 Pod 已經先砍掉，是中斷最徹底的一種。
    strategy: Optional[str] = None
    replica_failure: Optional[WorkloadReplicaFailure] = None
    last_warning: Optional[PodWarningEvent] = None


class VmiInfo(BaseModel):
    """KubeVirt VirtualMachineInstance status (#423 W4.1).

    ``ready`` is the VMI's ``Ready`` condition, NOT ``phase == Running``:
    a VMI reaches Running as soon as QEMU starts, while Ready additionally
    reflects that the virt-launcher probes are satisfied. The platform's
    readiness gate (Odoo ``workload_type == 'vm'`` branch) requires BOTH —
    see design §4.1.
    """

    name: str
    phase: str  # Pending / Scheduling / Scheduled / Running / Succeeded / Failed
    ready: bool
    node_name: Optional[str] = None
    # HAOS 18.1 ships qemu-guest-agent (W1 PoC R9). Not load-bearing for v1
    # readiness, but surfacing it lets the platform tell "QEMU up" from
    # "guest OS up" without another API round-trip.
    agent_connected: bool = False


class DataVolumeInfo(BaseModel):
    """CDI DataVolume status (#423 W4.1).

    ``progress`` is CDI's own import percentage string (e.g. ``"42.31%"``)
    or ``"N/A"`` before the importer starts. Surfaced so the UI can show a
    real progress bar during the long first-provision import instead of an
    opaque ``deploying`` (design §7.4).
    """

    name: str
    phase: str  # Pending / ImportScheduled / ImportInProgress / Succeeded / Failed
    progress: Optional[str] = None


class ReleaseRevision(BaseModel):
    """Helm release revision information."""

    revision: int
    updated: str
    status: ReleaseStatus
    chart: str
    app_version: str
    description: str


class RouteStatus(str, Enum):
    """How the expose request for a release turned out (#648 (a))."""

    OK = "ok"
    FAILED = "failed"


#: woow_paas_platform #1563：park／unpark 的 subdomain 必須是**一個** DNS label（平台的
#: ``paas-cs-<ws>-<svc>`` 與 operator 自動產生的 ``<release>-<ns 尾>`` 都符合）。
ROUTE_SUBDOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"


class RouteParkStatus(str, Enum):
    """woow_paas_platform #1563：park／unpark 成功時（HTTP 200）的結果。

    失敗一律是 4xx／5xx＋``detail.error_code``，**刻意不用 404**：平台把 404 當成
    「operator 還沒有這兩個端點」（舊 image），只記 log。
    """

    PARKED = "parked"              # 這次把規則改指停權說明頁
    ALREADY_PARKED = "already_parked"  # 早就指向說明頁（冪等重送）
    NO_SERVICE_RULE = "no_service_rule"  # 有規則，但沒有一條指向本 namespace 的服務（只有 http_status:*）
    UNPARKED = "unparked"          # 這次把規則改回服務
    NOT_PARKED = "not_parked"      # 沒有指向說明頁的規則（冪等重送／從沒 park 過）
    NOT_FOUND = "not_found"        # 這個 hostname 在 tunnel 上沒有任何規則
    DISABLED = "disabled"          # 這個 operator 沒開 Cloudflare 整合，什麼都不做


class RouteParkRequest(BaseModel):
    """把一個 release 的對外 hostname 指向停權說明頁（目標不由呼叫端指定）。"""

    subdomain: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=ROUTE_SUBDOMAIN_PATTERN,
        description="Subdomain the release was exposed on (one DNS label).",
    )


class RouteUnparkRequest(BaseModel):
    """把 hostname 改回服務：用與安裝時同一個 expose 設定重算 service_url。"""

    expose: ExposeConfig = Field(
        ...,
        description=(
            "The same expose config the release was installed with; the operator "
            "re-derives the service URL with the install-time logic."
        ),
    )


class RouteParkResponse(BaseModel):
    hostname: Optional[str] = None
    status: RouteParkStatus
    matched: int = Field(0, description="Ingress rules found for this hostname")
    changed: int = Field(0, description="Ingress rules rewritten by this call")
    service_url: Optional[str] = Field(
        None, description="Where the rewritten rules point after this call")


class RetrievalOpenRequest(BaseModel):
    """woow_paas_platform #1540：開一個限時唯讀取回入口。

    平台只指定 profile（operator 白名單）與時效；要掛哪顆 PVC、用哪個 uid、匯出哪些路徑全由
    operator 依 profile 決定。
    """

    profile: str = Field(
        ..., min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Retrieval profile name (operator whitelist, e.g. 'nextcloud').",
    )
    ttl_hours: int = Field(
        72, ge=1, le=24 * 30,
        description="Requested lifetime; capped by DATA_RETRIEVAL_MAX_TTL_HOURS and by the "
                    "retention of a deleted service's volume.",
    )


class RetrievalRouteStatus(str, Enum):
    """取回入口的 tunnel route 處理結果。"""

    CREATED = "created"        # 開：建好了
    DISABLED = "disabled"      # 這個 operator 沒開 Cloudflare 整合（沒有對外網址）
    DELETED = "deleted"        # 關：刪掉了
    NOT_FOUND = "not_found"    # 關：tunnel 上已經沒有這個 hostname
    NOT_OWNED = "not_owned"    # 關：這個 hostname 有規則指向別處，不碰
    NONE = "none"              # 關：沒有 hostname 可處理


class RetrievalOpenResponse(BaseModel):
    """開成功（201）。``password`` 只在這裡出現一次，operator 不存明文。"""

    status: str = "opened"
    hostname: Optional[str] = None
    url: Optional[str] = None
    username: str
    password: str
    opened_at: datetime
    expires_at: datetime
    data_expires_at: Optional[datetime] = Field(
        None, description="When the retained volume of a deleted service will be reclaimed.")
    pvc: str
    profile: str
    route: RetrievalRouteStatus
    dns_failed: bool = False


class RetrievalState(str, Enum):
    ACTIVE = "active"          # pod Ready
    STARTING = "starting"      # pod 還沒 Ready（掛 volume、拉 image）
    STOPPED = "stopped"        # pod 不在或已結束（到期、被刪），錨點還在
    NOT_FOUND = "not_found"    # 沒有取回入口


class RetrievalStatusResponse(BaseModel):
    status: RetrievalState
    hostname: Optional[str] = None
    url: Optional[str] = None
    opened_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    pod_phase: Optional[str] = None
    ready: bool = False
    pvc: Optional[str] = None
    profile: Optional[str] = None


class RetrievalCloseResponse(BaseModel):
    status: str = Field(..., description="'closed' or 'not_found' (nothing was open)")
    hostname: Optional[str] = None
    route: RetrievalRouteStatus


class RetrievalSweepResponse(BaseModel):
    checked: int
    closed: List[str] = Field(default_factory=list, description="<namespace>/<release>")
    failed: List[str] = Field(default_factory=list, description="<namespace>/<release>")


class RouteInfo(BaseModel):
    """Information about a Cloudflare Tunnel route.

    ⚠ **``ReleaseInfo.route`` being ``None`` is NOT a failure signal.** It means the
    release did not ask to be exposed, OR the Cloudflare integration is switched off
    in this deployment (``CLOUDFLARE_ENABLED`` defaults to **false**, and it is also
    forced off when any of account id / tunnel id / api token / domain is missing).
    A caller that treats a missing ``route`` as "expose failed" turns **every**
    deployment on a Cloudflare-less operator into an error — see
    ``docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md``
    in the platform repo.

    When expose *was* attempted, this object is always returned — with ``status``
    saying whether it worked. That is the signal callers must branch on.
    """

    hostname: Optional[str] = Field(
        None,
        description=(
            "Public hostname (e.g., myapp.example.com). Always set when status is "
            "'ok'. May be None on a 'failed' route when the failure happened before "
            "the subdomain was resolved — None means **not determined**, which is "
            "deliberately distinct from an empty string (a value someone chose)."
        ),
    )
    service_url: Optional[str] = Field(
        None,
        description=(
            "Internal service URL. Always set when status is 'ok'. May be None on a "
            "'failed' route: the two failure paths that fire before service "
            "discovery completes (no services found / no usable port) genuinely do "
            "not have one. Same rule as hostname — None is 'not determined', not ''."
        ),
    )
    enabled: bool = Field(default=True, description="Whether the route is active")
    status: RouteStatus = Field(
        default=RouteStatus.OK,
        description=(
            "'ok' when the tunnel ingress route was configured; 'failed' when expose "
            "was requested but no route could be created (Cloudflare API error, no "
            "service found to expose, or no usable port). Absent/'ok' from an older "
            "operator that predates this field means 'ok' — callers MUST NOT read a "
            "missing field as a failure."
        ),
    )
    dns_failed: bool = Field(
        default=False,
        description=(
            "True when DNS record creation was attempted and failed, so the hostname "
            "will not resolve even though the tunnel ingress rule exists. Mirrors "
            "TunnelCreateResponse.dns_failed: the caller MUST NOT treat the route as "
            "reachable while this is set. Always False when DNS management is "
            "intentionally disabled (CLOUDFLARE_ZONE_ID unset) — 'disabled' is not "
            "'failed', and conflating them makes every deployment on such a "
            "deployment look broken."
        ),
    )


class ReleaseInfo(BaseModel):
    """Detailed information about a Helm release."""

    name: str
    namespace: str
    revision: int
    status: ReleaseStatus
    chart: str
    # #552: the chart **version** actually resolved by Helm. Both parsers used to
    # throw it away (`_parse_release_info` read only `metadata.name`;
    # `_parse_list_release_info` split it off and dropped the tail), so the
    # platform could never record what it really installed — and with an empty
    # `--version` Helm resolves to the registry's *latest*, which nobody logged.
    #
    # ⚠ MUST stay `Optional[str] = None`: making it required would break every
    # direct `ReleaseInfo(...)` construction in the test-suite AND the
    # backward-compatibility contract with an older operator deployment (Odoo
    # reads `None` ⇒ keeps its current value rather than blanking the ledger).
    chart_version: Optional[str] = None
    app_version: str
    updated: str
    description: Optional[str] = None
    values: Optional[Dict[str, Any]] = None
    route: Optional[RouteInfo] = Field(None, description="Cloudflare Tunnel route info if exposed")


class ReleaseStatusResponse(BaseModel):
    """Release status including pod information."""

    release: ReleaseInfo
    pods: List[PodInfo]
    pod_retrieval_error: Optional[str] = Field(
        None, description="Error message if pod retrieval failed"
    )
    # ---- #1263／#1251 Q44：Pod 身分欄位的能力旗標（比照 ``vm_included``）----------
    # 舊 operator 沒有這個 key；平台缺旗標就 fail-closed。它宣告的是「本 operator
    # 會填 PodInfo 的身分欄位」，與這次抓 Pod 成不成功無關（那由
    # ``pod_retrieval_error`` 表達）——否則暫時性的讀取失敗會被平台誤判成 HARD。
    # 同一版起 ``PodInfo.creation_timestamp`` 也一定會填（#1222），同樣以本旗標判斷。
    pod_identity_included: bool = Field(
        default=False,
        description=(
            "True when this operator populates PodInfo.component/task/owner_kind. "
            "Absent/False means identity is unknown — do NOT read None as 'no label'."
        ),
    )
    # ---- #423 W4.2: VM-type workloads (opt-in via ?include_vm=true) --------
    # ``None`` means NOT QUERIED; ``[]`` means queried and genuinely empty.
    # The distinction is load-bearing: the platform is fail-CLOSED on VM
    # readiness, so a caller that forgets ``include_vm=true`` would otherwise
    # see the same empty list as "still importing" and park the service in
    # `deploying` forever. With None, that bug is diagnosable (and
    # ``vm_included`` below makes it assertable).
    vm_included: bool = Field(
        default=False,
        description=(
            "Echo of the include_vm request flag. False means the VM fields "
            "were never queried — do NOT interpret their emptiness as 'no VM'."
        ),
    )
    vmis: Optional[List[VmiInfo]] = Field(
        default=None,
        description=(
            "KubeVirt VMIs for this release. None = not queried "
            "(include_vm was false); [] = queried, none exist."
        ),
    )
    datavolumes: Optional[List[DataVolumeInfo]] = Field(
        default=None,
        description=(
            "CDI DataVolumes for this release. None = not queried; "
            "[] = queried, none exist."
        ),
    )
    # Split per resource kind (MINOR-1): a DataVolume hiccup must not make an
    # explicitly-Ready VMI look untrustworthy. W5a can then be fail-closed on
    # vmi_retrieval_error alone while treating a DataVolume error as "progress
    # bar unavailable" rather than "readiness unknown".
    vmi_retrieval_error: Optional[str] = Field(
        None,
        description=(
            "Error message if VMI retrieval failed. Mirrors pod_retrieval_error: "
            "the caller MUST NOT read an empty vmis list as 'no VM' while this "
            "is set — see the fail-closed rule in design §4.1."
        ),
    )
    datavolume_retrieval_error: Optional[str] = Field(
        None,
        description=(
            "Error message if DataVolume retrieval failed. Non-fatal for "
            "readiness — it only costs the import progress indicator."
        ),
    )
    # ---- #1647：workload（Deployment／StatefulSet）副本數（opt-in：?include_workloads=true）
    # 與 VM 欄位同一套三態：``None``＝沒查（沒帶旗標）或查詢失敗（看
    # ``workload_retrieval_error``），``[]``＝查了、這個 release 沒有 workload。舊 operator
    # 沒有這三個 key；平台必須把缺席當成「不支援」，不得當成「沒有 workload」或「健康」。
    workloads_included: bool = Field(
        default=False,
        description=(
            "Echo of the include_workloads request flag. False means the workloads "
            "were never queried — do NOT interpret their absence as 'no workloads'."
        ),
    )
    workloads: Optional[List[WorkloadInfo]] = Field(
        default=None,
        description=(
            "Deployments and StatefulSets of this release with their replica counts. "
            "None = not queried, or the lookup failed (see workload_retrieval_error); "
            "[] = queried, none exist."
        ),
    )
    workload_retrieval_error: Optional[str] = Field(
        None,
        description=(
            "Error message if the workload lookup failed. The caller MUST NOT read a "
            "missing workloads list as 'healthy' while this is set."
        ),
    )


class ReleaseListResponse(BaseModel):
    """List of Helm releases."""

    releases: List[ReleaseInfo]


class ReleaseRevisionsResponse(BaseModel):
    """List of release revisions."""

    revisions: List[ReleaseRevision]
    # INFRA-FIX-018-1／AI-HER-003-1：operator 的 helm --history-max（每個 release 最多保留
    # 幾筆 revision，含 failed）。更舊的 revision 已被 helm 清掉，無法回復。
    history_max: Optional[int] = None


class NamespaceInfo(BaseModel):
    """Information about a namespace."""

    name: str
    status: str
    created: str
    labels: Optional[Dict[str, str]] = None


class TunnelConnectionInfo(BaseModel):
    """Connection details for a Cloudflare Tunnel connector.

    ⚠ ``connector_id`` 是**單一條連線**的 id（CF 的 ``connections[].id``），不是
    「一個 cloudflared 行程」——cloudflared 預設開 4 條連線到不同 edge，每條都有
    自己的 id。要數「有幾台機器接著這條 tunnel」只能看 ``client_id``（CF 的
    ``connections[].client_id``，同一個行程的 4 條連線共用一個）。平台端靠這個欄位
    偵測「同一條 tunnel 被兩台 HA 同時接上」（平台 #1766 / 元件 #31）。
    """

    connector_id: str = Field(..., description="Connection ID (one per edge connection)")
    client_id: str = Field(
        default="",
        description="cloudflared process ID (shared by that process's connections)",
    )
    type: str = Field(default="", description="Connection type")
    origin_ip: str = Field(default="", description="Origin IP address")
    opened_at: str = Field(default="", description="Connection opened timestamp")


class TunnelCreateResponse(BaseModel):
    """Response after creating a dedicated Cloudflare Tunnel."""

    tunnel_id: str = Field(..., description="Cloudflare Tunnel ID")
    tunnel_name: str = Field(..., description="Tunnel name")
    tunnel_token: str = Field(..., description="Tunnel token for cloudflared")
    hostname: Optional[str] = Field(
        None, description="Configured public hostname (None for a bare tunnel)"
    )
    dns_record_id: Optional[str] = Field(
        None, description="DNS record ID if created"
    )
    dns_failed: bool = Field(
        False,
        description=(
            "True when DNS was enabled but the record creation failed, so "
            "dns_record_id is None for an error reason (vs None because DNS "
            "is intentionally disabled). Lets the caller distinguish the two "
            "and alert/re-sync rather than silently treating the tunnel as "
            "fully reachable."
        ),
    )


class TunnelStatusResponse(BaseModel):
    """Status information for a Cloudflare Tunnel."""

    tunnel_id: str = Field(..., description="Cloudflare Tunnel ID")
    name: str = Field(..., description="Tunnel name")
    status: str = Field(..., description="Tunnel status (e.g., 'healthy', 'inactive')")
    connections: List[TunnelConnectionInfo] = Field(
        default_factory=list, description="Active tunnel connections"
    )
    created_at: Optional[str] = Field(
        None, description="Tunnel creation timestamp"
    )
    deleted_at: Optional[str] = Field(
        None,
        description=(
            "Cloudflare soft-delete timestamp (#833). NOT None = the tunnel has "
            "been deleted on Cloudflare; None = not deleted, OR this operator "
            "version does not report it. Callers MUST treat a falsy value as "
            "'not deleted' and fall back to their previous behaviour — an absent "
            "field is a version signal, never a failure signal. Cloudflare soft "
            "deletes tunnels: GET /cfd_tunnel/{id} still returns 200 after DELETE, "
            "so 'the GET succeeded' is not proof the tunnel is alive."
        ),
    )


class TunnelTokenResponse(BaseModel):
    """Response containing a tunnel token."""

    tunnel_id: str = Field(..., description="Cloudflare Tunnel ID")
    token: str = Field(..., description="Tunnel token for cloudflared")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: datetime
    helm_version: Optional[str] = None


class SidecarContainer(BaseModel):
    """Sidecar container specification for deployment patching."""

    name: str = Field(..., description="Container name")
    image: str = Field(..., description="Container image")
    ports: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Container ports"
    )
    env: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Environment variables"
    )
    resources: Optional[Dict[str, Any]] = Field(
        default=None, description="Resource requests/limits"
    )
    liveness_probe: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="livenessProbe",
        description="Liveness probe configuration",
    )
    readiness_probe: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="readinessProbe",
        description="Readiness probe configuration",
    )

    model_config = {"populate_by_name": True}


class SidecarPatchRequest(BaseModel):
    """Request to patch a deployment with a sidecar container."""

    deployment_name: Optional[str] = Field(
        None,
        description="Deployment name to patch (auto-detected from release if not provided)",
    )
    container: SidecarContainer = Field(
        ..., description="Sidecar container specification"
    )


class SidecarPatchResponse(BaseModel):
    """Response from sidecar patch operation."""

    message: str
    deployment: str
    namespace: str
    container_name: str
    mcp_service_name: Optional[str] = Field(
        None, description="Name of the K8s Service created for the sidecar"
    )
    mcp_endpoint_url: Optional[str] = Field(
        None, description="Public URL to reach the MCP sidecar endpoint (via Cloudflare)"
    )
    mcp_internal_url: Optional[str] = Field(
        None, description="Internal K8s URL for the MCP sidecar service"
    )


class N8nInitRequest(BaseModel):
    """Request to initialize an n8n instance after deployment."""

    owner_email: str = Field(
        default="admin@woowtech.io",
        description="Email for the n8n owner user",
    )
    owner_password: str = Field(
        ...,
        description="Password for the n8n owner user",
    )
    # #1534：舊平台不送 ⇒ False（既有 owner 必須是 owner_email 才發 key）；pydantic 預設忽略
    # 未知欄位，新平台送給舊 operator 也不會被拒。
    owner_confirmed: bool = Field(
        default=False,
        description="The platform has completed an n8n init for this service before, so an "
                    "existing owner is the tenant's account even if its email changed",
    )


#: init 失敗分類（#1263／#1257 Q11），依「遠端副作用是否可能已發生」：
#: not_executed＝只跑過唯讀探測；failed＝收到明確的錯誤回應；partial＝n8n 已拿到
#: api_key 但 Secret 沒寫成；unknown＝timeout／exec 失敗／輸出不可解析／非預期例外。
#: ⚠ 與 ``ErrorResponse.error_type``（例外類別名稱）是不同維度，不要混用。
InitErrorKind = Literal["not_executed", "failed", "partial", "unknown"]


class N8nInitResponse(BaseModel):
    """Response from n8n initialization."""

    success: bool
    api_key: Optional[str] = Field(None, description="Generated n8n API key (JWT)")
    owner_email: Optional[str] = None
    secret_patched: bool = Field(default=False, description="Whether the K8s Secret was updated with the real API key")
    pod_restarted: bool = Field(default=False, description="Whether the deployment was restarted to pick up the new API key")
    message: Optional[str] = None
    error: Optional[str] = None
    # #1263：成功時兩者皆為 None；舊平台忽略未知欄位。
    error_step: Optional[str] = None
    error_kind: Optional[InitErrorKind] = None


class OdooInitResponse(BaseModel):
    """Response from Odoo module list initialization."""

    success: bool
    modules_added: int = Field(default=0, description="Number of new modules discovered")
    modules_updated: int = Field(default=0, description="Number of existing modules updated")
    message: Optional[str] = None
    error: Optional[str] = None
    # #1263：成功時兩者皆為 None；舊平台忽略未知欄位。
    error_step: Optional[str] = None
    error_kind: Optional[InitErrorKind] = None
    # #1355：台灣在地化預設的結果（success／changes／skipped／error）。失敗不影響
    # ``success``——在地化不是服務可用的前提；舊平台忽略未知欄位。
    localization: Optional[Dict[str, Any]] = None


# --- App 自管管理員密碼重設（#1370／#1186）---

# 腳本回報的原因碼（``success: false`` 時）。平台據此轉成租戶看得懂的可行動訊息。
AppAdminResetCode = Literal[
    "admin_missing",      # Odoo：找不到 base.user_admin／n8n：沒有 owner 或 owner 從未 setup／
                          # Omnigent：找不到 admin 帳號
    "admin_inactive",     # Odoo：base.user_admin 已封存／Omnigent：admin 已被拿掉管理員角色
    "owner_ambiguous",    # n8n：owner 不只一列，或 UPDATE 影響列數 != 1
    "hash_unavailable",   # n8n：n8n 安裝目錄裡找不到 bcryptjs／DB driver
    "db_unsupported",     # n8n：DB_TYPE／schema 不在支援範圍／Omnigent：沒有 DATABASE_URL 或非 accounts 模式
    "invalid_password",   # 新密碼長度不在 12–128
]


class AppAdminPasswordResetRequest(BaseModel):
    """新密碼只在 request body；operator 以 stdin 交給容器，不進 argv／log。

    刻意**不在 schema 上設長度限制**：FastAPI 的 422 回應會把 ``input`` 原文帶回
    （pydantic v2 對 SecretStr 的長度錯誤同樣先驗內層 str）。長度由 endpoint 自己
    檢查、回不含原文的錯誤。
    """

    new_password: SecretStr = Field(..., description="New admin password (12-128 chars)")


class AppAdminPasswordResetResponse(BaseModel):
    success: bool
    login: Optional[str] = Field(None, description="Odoo login / n8n owner email whose password was reset")
    second_factor_cleared: bool = Field(
        default=False, description="Odoo TOTP / n8n MFA was enabled and has been turned off",
    )
    message: Optional[str] = None
    error: Optional[str] = None
    error_step: Optional[str] = None
    error_code: Optional[AppAdminResetCode] = None
    error_kind: Optional[InitErrorKind] = None


# --- Odoo Module Management ---


class OdooModuleStatesRequest(BaseModel):
    """Request to query Odoo module installation states."""

    module_names: List[str] = Field(..., description="List of module technical names to check")


class OdooModuleStateInfo(BaseModel):
    """Installation state and reverse dependencies for a single module."""

    state: str = Field(..., description="Module state: installed, uninstalled, uninstallable, etc.")
    dependents: List[str] = Field(default_factory=list, description="Installed modules that depend on this one")


class OdooModuleStatesResponse(BaseModel):
    """Response with module states."""

    success: bool
    modules: Dict[str, OdooModuleStateInfo] = Field(default_factory=dict)
    error: Optional[str] = None




class PublicAddressResponse(BaseModel):
    """對外位址（chart 的 ngrok sidecar 所廣告的那一個；EMQX 的 MQTT、Headscale 的控制網址）。

    ``public_url`` 為 None 不代表錯誤：租戶沒開、agent 還沒連上、sidecar 撤回了位址、
    pod 還在起來，都會落在這裡。呼叫端不得把 None 當失敗。
    """

    public_url: Optional[str] = Field(
        default=None,
        description=(
            "ngrok agent 當前持有的 tcp://host:port 或 https://host[:port]；"
            "沒有可報告的位址時為 None"
        ),
    )
    # #1559：沒有位址、而 sidecar log 說得出原因時（例：authtoken 錯 ⇒ ERR_NGROK_105）。
    # 封閉集合的代碼＋operator 端固定中文說明；絕不是原始 log。舊平台不讀這兩欄（純加法）。
    error_code: Optional[str] = Field(
        default=None,
        description="ERR_NGROK_<n> | NO_AUTHTOKEN | SERVER_URL_MISMATCH；有位址時恆為 None",
    )
    error: Optional[str] = Field(
        default=None,
        description="error_code 的中文說明（固定文案，不含任何 log 內容）",
    )


class MqttAddressResponse(BaseModel):
    """舊端點 ``/mqtt-address`` 的回應（平台 ≤ 18.0.1.0.205 仍會呼叫）。

    只會回 ``tcp://`` 位址；新平台改呼叫 ``/public-address``（``PublicAddressResponse``）。
    ``public_url`` 為 None 不代表錯誤。
    """

    public_url: Optional[str] = Field(
        default=None,
        description=(
            "ngrok agent 當前持有的 tcp://host:port；沒有可報告的位址時為 None"
        ),
    )


class ErrorResponse(BaseModel):
    """Error response.

    ``detail`` 放寬成 ``str | dict``（#531）。**每一個** ``HTTPException`` 都經
    ``src.main.http_exception_handler`` 灌進這個 model，而原本的 ``detail: str``
    會讓任何結構化 detail 在**回應序列化階段**炸成 500 —— 症狀是端點看起來壞掉，
    實際上是這裡的型別把它擋下來的。FastAPI 自己的 ``HTTPException.detail`` 型別
    本來就是 ``Any``，這裡只是不再比它窄。
    ``str`` 排在前面：既有的字串 detail 維持原樣序列化，不會被 coerce 成別的形狀。

    呼叫端相容性：平台 client 的 ``_parse_error``（``src/services/paas_operator.py``）
    是 ``error_data.get('detail', ...)`` 後直接丟進 f-string，拿到 dict 不會炸。
    """

    detail: Union[str, Dict[str, Any]]
    error_type: Optional[str] = None


# Agent Runtime (per-project pod) — design §2.11


class SpawnOrAttachRequest(BaseModel):
    """Request to spawn / attach / resume a project's agent-runtime pod."""

    project_hash: str = Field(..., description="K8s namespace hash (per-project)")
    project_id: int = Field(..., description="Authoritative project id (token claim)")
    channel_id: int = Field(..., description="Invoking discuss.channel id")
    invoker_user_id: int = Field(..., description="Invoking res.users id")
    invoker_role: str = Field(..., description="Invoker project_access role")
    project_workspace_ids: List[int] = Field(
        default_factory=list,
        description="Workspace ids the pod's NetworkPolicy egress may reach",
    )
    agent_template_snapshot: Dict[str, Any] = Field(default_factory=dict)
    mcp_snapshot: Dict[str, Any] = Field(default_factory=dict)
    skill_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    # F-PILOT-8 — per-tenant LLM credential (design §8.2). The operator never
    # queries the Odoo DB, so the tenant's ai.config api_key (+ optional model
    # config) rides the spawn request and the operator provisions it as a
    # per-release Secret. NB the model-config field is `llm_model_config`, NOT
    # `model_config` — the latter is a pydantic-reserved BaseModel attribute.
    api_key: Optional[str] = Field(
        default=None,
        description="Per-tenant LLM api_key, provisioned as a per-release Secret",
    )
    llm_model_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional LLM model config (model / temperature / ...)",
    )


class ResumeRequest(BaseModel):
    """Optional body for the standalone resume endpoint (re-sign token)."""

    project_id: Optional[int] = None
    project_workspace_ids: List[int] = Field(default_factory=list)


class InvalidateMcpRequest(BaseModel):
    """Request to invalidate a pod's mcp.json ConfigMap snapshot."""

    mcp_snapshot: Dict[str, Any] = Field(default_factory=dict)


class InvalidateSkillsRequest(BaseModel):
    """Request to invalidate a pod's skills.json ConfigMap snapshot.

    Symmetric with :class:`InvalidateMcpRequest`; the snapshot is the
    list[{name, version, sha256, description}] the Odoo controller builds.
    """

    skill_snapshot: List[Dict[str, Any]] = Field(default_factory=list)


class SyncWorkspacesRequest(BaseModel):
    """Request to re-render a project's tenant-isolation legs (#279 O5).

    leg-1 = pod NetworkPolicy egress allowlist (helm upgrade);
    leg-2 = agent-ns ``reach-ws-*`` labels (O3 reconcile primitive).
    """

    project_workspace_ids: List[int] = Field(default_factory=list)
    # GRANT path sets True: if the pod's helm release is gone, leg-1 egress can't be
    # applied, so the operator MUST fail-loud (409) instead of returning 200 after only
    # leg-2 — otherwise the caller commits a non-functional grant + an orphan reach
    # label. REVOKE leaves it False (a revoke against a gone pod is a no-op success).
    require_release: bool = Field(default=False)


# Security Access — multi-route dedicated tunnel sync (security-access P1)


class TunnelRouteSpec(BaseModel):
    """One desired ingress route on a dedicated tunnel."""

    subdomain: str = Field(..., description="Subdomain under the platform zone")
    service_url: str = Field(..., description="Backend service URL or http_status:NNN")
    http_host_header: bool = Field(
        True, description="Send the public hostname as HTTP Host header to origin"
    )


class TunnelRoutesSyncRequest(BaseModel):
    """Declarative full sync of a dedicated tunnel's routes."""

    routes: List[TunnelRouteSpec] = Field(..., min_length=0)
    comment: Optional[str] = Field(
        None, description="Canonical name stamped on DNS records for traceability"
    )
    #: #531 ownership claim. 刻意**不重用** ``comment``：後者是 DNS record 上的
    #: traceability stamp（在本 repo 是只寫不讀的自由文字），把它變成授權判準等於
    #: 讓「改 stamp 格式」順帶改掉安全姿態。兩個用途分兩個欄位。
    tunnel_name: Optional[str] = Field(
        None,
        description=(
            "Ownership claim: the caller's own record of this tunnel's Cloudflare "
            "name. Verified against the live tunnel — see _assert_tunnel_ownership."
        ),
    )


class TunnelRouteSyncResult(BaseModel):
    subdomain: str
    hostname: str
    dns_record_id: Optional[str] = None


class TunnelRoutesSyncResponse(BaseModel):
    tunnel_id: str
    routes: List[TunnelRouteSyncResult]


# Security Access — Cloudflare Access (Zero Trust) sync (security-access P2)


class AccessDecision(str, Enum):
    """Cloudflare Access policy decision. ``bypass`` skips authentication and
    does not support identity-based selectors (enforced below)."""

    ALLOW = "allow"
    DENY = "deny"
    BYPASS = "bypass"


# Selectors whose rule_type carries no value (e.g. "everyone").
_VALUELESS_RULE_TYPES = {"everyone"}
# Identity-based selectors a bypass policy may not use (Cloudflare rejects them).
_IDENTITY_RULE_TYPES = {"email_domain", "email_list"}


class AccessRuleSpec(BaseModel):
    """One selector rule inside a policy group (include / require / exclude).

    rule_value is per-type: domain (no @) / comma-separated emails / CIDR /
    comma-separated ISO-2 country codes; empty for ``everyone``.
    """

    rule_type: str = Field(
        ...,
        description="email_domain | email_list | ip_range | everyone | country",
    )
    rule_value: Optional[str] = Field(None, description="Selector value per type")

    @field_validator("rule_value", mode="before")
    @classmethod
    def _normalize_empty(cls, v):
        # Odoo reads an unset Char field back as Python ``False``; a valueless
        # selector (e.g. everyone) therefore arrives as JSON ``false``. Coerce
        # any falsy value (False / "" / None) to None so it is accepted instead
        # of 422'd as a non-string — defense in depth with the Odoo-side
        # normalization in _rules_by_group.
        return v or None


class AccessPolicySpec(BaseModel):
    """One desired Access policy (reusable) on an SA's Access App.

    Mirrors Cloudflare's native policy shape: include (OR) / require (AND) /
    exclude (NOT) selector groups + a decision. At least one Include rule is
    required; bypass policies may not carry identity-based selectors.
    """

    name: str = Field(..., description="Policy display name")
    decision: AccessDecision = Field(..., description="allow | deny | bypass")
    include: List[AccessRuleSpec] = Field(
        default_factory=list, description="OR group; at least one required"
    )
    require: List[AccessRuleSpec] = Field(
        default_factory=list, description="AND group"
    )
    exclude: List[AccessRuleSpec] = Field(
        default_factory=list, description="NOT group"
    )
    precedence: int = Field(1, description="Evaluation order; lower first")

    @model_validator(mode="after")
    def _validate_groups(self) -> "AccessPolicySpec":
        if not self.include:
            raise ValueError(
                "Access policy must contain at least one include rule")
        if self.decision == AccessDecision.BYPASS:
            for group in (self.include, self.require, self.exclude):
                if any(r.rule_type in _IDENTITY_RULE_TYPES for r in group):
                    raise ValueError(
                        "Bypass policies cannot use identity-based selectors "
                        "(email_domain / email_list)")
        return self


class AccessSyncRequest(BaseModel):
    """Declarative full-state sync of one SA's Cloudflare Access config.

    enabled=False → operator tears down the app + its reusable policies.
    """

    enabled: bool = Field(..., description="Master Access protection toggle")
    canonical_name: str = Field(
        ..., description="SA canonical name; stamped on app/policy names"
    )
    app_name: str = Field(..., description="Access Application display name")
    primary_hostname: str = Field(
        ..., description="App primary domain (SA primary hostname)"
    )
    protected_hostnames: List[str] = Field(
        default_factory=list,
        description="Hostnames to protect (app destinations); subset of routes",
    )
    session_duration: str = Field(
        "24h", description='Go duration string, e.g. "8h" / "24h"'
    )
    login_idp_types: List[str] = Field(
        default_factory=list,
        description='IdP types to allow, e.g. ["google", "onetimepin"]; '
                    "empty = all available",
    )
    policies: List[AccessPolicySpec] = Field(default_factory=list)
    existing_app_id: Optional[str] = Field(None)
    existing_policy_ids: Optional[List[str]] = Field(None)


class AccessSyncResponse(BaseModel):
    app_id: Optional[str] = Field(None, description="CF Access App id (None if disabled/torn down)")
    policy_ids: List[str] = Field(default_factory=list)
    allowed_idps: List[str] = Field(
        default_factory=list, description="Resolved IdP ids set on the app"
    )
    unresolved_idp_types: List[str] = Field(
        default_factory=list,
        description="Requested login methods with no matching account IdP "
                    "(silently dropped → login restriction widened to all IdPs)",
    )
    orphaned_policy_ids: List[str] = Field(
        default_factory=list,
        description="Old reusable policies that could not be deleted after the "
                    "app was repointed — permanent CF orphans to log/alert on",
    )


class IdentityProviderInfo(BaseModel):
    id: str
    name: str
    type: str


class IdentityProvidersResponse(BaseModel):
    identity_providers: List[IdentityProviderInfo]


# ==================== L4 reconciler（帳號級反向孤兒對帳）====================
# 設計 doc: docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md
# 首版「零自動刪除」：reconcilable / dry_run 的預設都偏向安全（不可回收 / dry-run）。


class OrphanKind(str, Enum):
    """L4 reconciler 反向發現的孤兒資源類別。enum 值是 operator↔Odoo wire 契約的一部分。"""

    TUNNEL = "tunnel"                  # cfd_tunnel（dedicated，CNAME 指自身）
    DNS_RECORD = "dns_record"          # zone DNS（指 shared / dedicated tunnel）
    ACCESS_APP = "access_app"          # Zero Trust Access application
    ACCESS_POLICY = "access_policy"    # reusable Access policy
    NAMESPACE = "namespace"            # k8s 租戶 namespace（Phase E）


class OrphanCandidate(BaseModel):
    """單筆疑似孤兒（dry-run inventory 的一列）。"""

    id: str = Field(..., description="CF / k8s 資源 id")
    name: str = Field(default="", description="顯示名（tunnel name / hostname / app name ...）")
    kind: OrphanKind = Field(..., description="資源類別")
    env_hint: str = Field(
        default="unknown", description="判讀的環境歸屬：prod / stg / unknown"
    )
    reconcilable: bool = Field(
        default=False,
        description="是否『結構上可被自動回收』。首版恆 False（dry-run-only、刪除權留人手）；"
                    "唯帶 owner marker 的資源未來可能升 True（仍受 DP-MARKER 等 gate）。",
    )
    reason: str = Field(
        default="", description="判定理由 / 人工審用說明（如 'no env token, manual only'）"
    )


class ReconcileRequest(BaseModel):
    """Odoo → operator：帶各類 stored-id 左集（白名單），請 operator 對 CF/k8s 右集取差集。"""

    stored_ids: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Odoo 持有的 stored-id 左集，依 kind 分組"
                    "（如 {'tunnel': [...], 'dns_record': [...], 'access_app': [...]}）",
    )
    dry_run: bool = Field(
        default=True,
        description="首版恆 True；傳 False 由 endpoint 422 拒收（發現與刪除刻意分離）。",
    )


class ReconcileResponse(BaseModel):
    """operator → Odoo：dry-run 孤兒 inventory。"""

    dry_run: bool = Field(default=True, description="本次是否 dry-run（首版恆 True）")
    partial: bool = Field(
        default=False,
        description="completeness gate：任一類右集撈取失敗 → True，"
                    "且此時不得有任何 reconcilable=True 的候選。",
    )
    orphan_candidates: List[OrphanCandidate] = Field(
        default_factory=list, description="疑似孤兒清單（dry-run，供人工審）"
    )


# ── 方向 A：operator 唯讀 inventory wire（operator→Odoo 右集 ground truth）──────
# operator 對 CF 帳號級資源只做唯讀發現、回 raw 事實；差集 / 分類（env_hint /
# reconcilable / reason）/ 刪除全在 Odoo `src/services/reconciler.py`。故 operator 端
# wire 是未分類的 `InventoryResponse`，不是 `ReconcileResponse`（後者是 Odoo 端產出）。


class CfResource(BaseModel):
    """帳號級反向 inventory 的單筆 CF 資源（raw、未分類）。

    operator 只回『事實』（id / name / kind），**不**含 env_hint / reconcilable /
    reason — 那些是 Odoo reconciler.py 依 stored-id 左集 + 守衛規則算出的分類結果。
    """

    id: str = Field(..., description="CF 資源 id")
    name: str = Field(default="", description="顯示名（tunnel name / app name / policy name）")
    kind: OrphanKind = Field(..., description="資源類別")


class InventoryResponse(BaseModel):
    """operator → Odoo：帳號級 CF 資源 inventory（右集 ground truth，未分類、唯讀）。"""

    paas_env: str = Field(
        ...,
        description="operator 鎖定的環境（R3 env-pin 回拋，恆為 prod|stg；未鎖定時端點 503 不回此物件）",
    )
    shared_tunnel_id: str = Field(
        ...,
        description="operator 配置的 shared tunnel id（CLOUDFLARE_TUNNEL_ID，與 CloudflareService "
                    "同源）。Odoo reconciler 用它把 shared tunnel 本體標 protected（必出現在 "
                    "list_all_tunnels 右集、不在 SM/SA 左集 → 否則被誤判孤兒）。**取代**對完整 "
                    "32-char UUID 永不命中的 8-char 硬編 denylist。空值時 Odoo 端 fail-loud abort。",
    )
    partial: bool = Field(
        default=False,
        description="completeness gate：任一類 list-all 失敗 → True，該類資源缺漏。"
                    "下游差集**不得**把缺漏類當『無孤兒』，且 partial 時不得授權任何 reconcilable=True。",
    )
    failed_kinds: List[str] = Field(
        default_factory=list,
        description="撈取失敗的資源類別（partial 細節，供人工審看是哪一類沒撈到）",
    )
    resources: List[CfResource] = Field(
        default_factory=list, description="撈到的 CF 資源（未分類，右集 ground truth）"
    )
