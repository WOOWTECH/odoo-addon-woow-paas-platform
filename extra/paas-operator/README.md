# PaaS Operator Service

FastAPI-based Kubernetes Helm operations API for the PaaS platform.

## Overview

The PaaS Operator Service is a secure, high-performance API that wraps Helm CLI operations for managing Kubernetes workloads. It provides RESTful endpoints for installing, upgrading, and managing Helm releases in a multi-tenant PaaS environment.

### Key Features

- **Helm Operations**: Install, upgrade, rollback, and uninstall Helm releases
- **Namespace Management**: Create namespaces with resource quotas
- **Pod Monitoring**: Real-time pod status and health checks
- **Cloudflare Integration**: Automatic DNS record and Tunnel route management for public service access
- **Security**: API key authentication and application-layer namespace prefix validation (see [Namespace Enforcement](#namespace-enforcement) — the prefix boundary is enforced in the app layer, _not_ by RBAC)
- **High Performance**: Async-first FastAPI with connection pooling
- **Production-Ready**: Health checks, structured logging, and error handling

## Architecture

```
┌──────────────┐
│     Odoo     │
│  (woow_paas) │
└──────┬───────┘
       │ HTTP + API Key
       ▼
┌──────────────────────┐       ┌─────────────────┐
│  PaaS Operator API   │──────▶│  Cloudflare API │
│     (FastAPI)        │ HTTPS │  (DNS + Tunnel) │
└──────┬───────────────┘       └─────────────────┘
       │ subprocess
       ▼
┌──────────────────────┐       ┌─────────────────┐
│    Helm CLI          │──────▶│  Kubernetes API │
└──────────────────────┘       └─────────────────┘
```

## API Endpoints

### Resource observations

- `POST /api/namespaces/{namespace}/resource-observations` accepts `{"release_names":["svc-example"]}` (1–100 releases). It reads namespace-scoped Kubernetes inventory and Prometheus; it does not run Helm, execute commands inside tenants, change resource allocations, or update billing.
- `RESOURCE_PROMETHEUS_URL` selects the Prometheus base URL, defaulting to `http://prometheus-operated.cattle-monitoring-system.svc:9090`. An empty value disables usage queries while preserving readable Kubernetes configuration. No `nodes/proxy` or `metrics.k8s.io` RBAC grant is required.
- Results include effective CPU/memory requests and limits, CPU usage averaged over five minutes (`window_seconds: 300`), memory working set, persistent-volume filesystem usage/capacity, sample timestamps and coverage. Active replicas and sidecars are aggregated; each referenced PVC is counted once, including unlabelled `existingClaim` volumes. Longhorn replica sizes are not tenant filesystem capacity.
- CPU queries use `rate(...[300s])`, with the range and response metadata derived from `CPU_WINDOW`. The five-minute window accommodates approximately 60-second cAdvisor sampling plus ingestion delay. The newest container/cgroup series is still selected before aggregation; a new series without enough samples remains unavailable rather than borrowing a predecessor's rate.
- Observations are cached for 30 seconds with bounded concurrent collection. Missing data is `null`, genuine zero remains `0`, and samples older than 120 seconds are stale. Partial coverage must not be rendered as a complete utilization percentage. Limits missing on any container are reported as unbounded.
- VM guest capacity is read from its domain configuration. Guest utilization and ephemeral-storage usage are currently unavailable from the configured monitoring sources; the API reports this explicitly rather than substituting launcher usage or zero.
- Production/staging namespace boundaries and the existing API-key authentication apply. The Odoo viewer endpoint resolves authorized service IDs to release/namespace pairs server-side, so browsers never supply Kubernetes scopes or PromQL.

Deploy the operator before the Odoo addon that consumes this endpoint. An older/unreachable operator produces an unavailable reading in the UI without changing the service state. Roll back the UI/addon first if both components must be reverted; this endpoint has no database migration or workload mutation to undo.

### Health

- `GET /health` - Health check with Helm version

### Releases

- `POST /api/releases` - Install a Helm chart. First it refuses a same-name reinstall over orphaned data (#1157): if the target namespace still has PVCs / DataVolumes labelled `paas.woowtech.io/release-name=<name>` **and** `paas.woowtech.io/orphaned-at` (left by a previous `DELETE`), it answers **409** `orphaned_release_resources_exist` with `detail.pvcs` / `detail.datavolumes` — helm would silently re-attach the old data while the orphan labels stay, and the pvc-cleanup CronJob would later delete the live data. Delete those resources first (if the data is not needed) or use a new release name; there is deliberately no re-attach flag. If that lookup itself fails it answers **503** `orphaned_release_check_unavailable` (fail-closed). Then, still before any cluster write, it runs two read-only pre-checks and answers **409** with a structured `detail.error_code` when the install could not succeed: `workspace_storage_quota_exhausted` (namespace ResourceQuota already full, #1348) and `cluster_storage_capacity_exhausted` (the PVCs / StatefulSet `volumeClaimTemplates` / CDI DataVolumes this install would create — rendered with `helm install --dry-run` from the same chart, version and values — cannot be scheduled by Longhorn under its own rules, #1415; `detail` lists the volumes, per-node headroom and orphaned PVCs). Both checks let the install through when they cannot read their data. Kill switch: env `STORAGE_CAPACITY_PRECHECK_ENABLED=false` (chart ≥ 0.1.22: `config.storageCapacityPrecheck: false`).
- `GET /api/releases/{namespace}/{name}` - Get release info
- `PATCH /api/releases/{namespace}/{name}` - Upgrade release. #1552: preflight and upgrade share one helm slot (`HELM_MAX_CONCURRENT_OPERATIONS`); when no slot frees up within `HELM_QUEUE_TIMEOUT_SECONDS` the answer is **503** `detail.error_code=operator_busy` and nothing was applied (the same applies to install, rollback, uninstall and reset-config). Preflight before helm applies anything: #1407 immutable-field conflicts → 409 (string detail); #1513 every PVC that grows is checked first against its Longhorn volume (`Scheduled≠True`, i.e. replicas not all placed → Longhorn refuses to expand) and then with `kubectl patch --dry-run=server` (Longhorn's `validator.longhorn.io` runs on server dry-run and decides whether the replicas' disks fit the extra size) → 409 with `detail.error_code=storage_expansion_rejected`, `reason=capacity|rejected`, `pvcs[]`. Timeouts / unreadable Longhorn are treated as unknown (allowed). Switch: `STORAGE_CAPACITY_PRECHECK_ENABLED`. Reading `volumes.longhorn.io` needs the chart's ClusterRole ≥ 0.1.23. #1647 (only when the body carries `check_compute_quota: true`; absent = unchanged behaviour): from the same dry-run manifest, the live Deployments／StatefulSets and one `kubectl get resourcequotas,limitranges,pods`, work out whether the release's pods fit the namespace ResourceQuota (`limits.*`／`requests.*` for cpu, memory, ephemeral-storage) once upgraded; when they certainly do not → 409 with `detail.error_code=workspace_compute_quota_exceeded`, `namespace`, `release`, `quota`, `quota_source=live|pending`, `shortfalls[]` (`resource`, `dimension`, `kind`, `phase=steady|rolling`, `workload`, `hard`, `used`, `release_current`, `other`, `required`, `available`, `short_by` — all strings: cpu in cores, the rest Gi／Mi; a `rolling` shortfall additionally carries `reason=rolling_on_downgrade` when the limit that does not fit is a `pending_quota` smaller than the live one, i.e. a plan downgrade — the key is absent otherwise), `workloads[]`, `consumers[]` and a Traditional-Chinese `message`. Anything unknown is allowed (unreadable quota／LimitRange／pods — look for `Compute quota pre-check skipped` in the log, e.g. a ServiceAccount that cannot list `limitranges`; any other exception while reading them is logged the same way — selectors with `matchExpressions`, pods that cannot be matched to their workload, pod-level resources, `OnDelete` StatefulSets, containers without limits and no LimitRange, or an error inside the check itself). Details of the judgement: while a quota's `spec.hard` and `status.hard` disagree (the quota controller has not caught up with a quota that was just pushed) the larger value is used; when the same upgrade lowers `replicas` of a `RollingUpdate` Deployment, the old pods that are scaled away first are subtracted before asking for room for one new pod; pods owned by a Job (CronJob runs included) are temporary usage — an upgrade that only fails to fit because of them is allowed and reported as a `transient_job_usage` advisory (a `Recreate` pod then waits for the Job to finish), it is refused only when it does not fit without them either (a Job pod whose usage cannot be worked out is not subtracted, so it never turns a refusal into a pass; `job_pods` in the advisory is the amount that was subtracted). Not counted as demand: DaemonSets, Jobs／CronJobs, bare Pods, KubeVirt VMs, helm hooks. Switches (independent; either one turns the check off): the platform system parameter `woow_paas_platform.compute_quota_precheck_enabled` (set it to `False` and the platform stops sending the flag), and the operator env `COMPUTE_QUOTA_PRECHECK_ENABLED=false` (skips the check even when asked). From paas-operator chart 0.1.28 the env is set by the value `config.computeQuotaPrecheck` (woow-paas-charts#180; default `true`, only an explicit `false` turns it off), so it can be switched per environment from Fleet values. Earlier charts cannot set it: their Deployment template lists every env explicitly (as it does `STORAGE_CAPACITY_PRECHECK_ENABLED` ← `config.storageCapacityPrecheck`) and has no generic env pass-through, so setting it there means hand-editing the Deployment, which Fleet reverts. The rollback endpoint accepts the same flag for both of its modes (see below).
- `POST /api/releases/{namespace}/{name}/upgrade-preflight` - (#1513) Run the same preflight with the same body as `PATCH`, apply nothing; returns `{"ok": true, "expansions": [...]}` or the same 409 / 500. The platform calls it before charging a plan change. #1647: with `check_compute_quota: true` the answer also carries `compute_quota` (`status=ok|skipped|unknown`, `quota_source`, `resources`, `advisories` — each with a `type`: `rolling_update_headroom`, `transient_job_usage` or `cronjob_headroom` — or `reason` when not `ok`; `null` when the live objects could not be read) — only the 409 means "does not fit", a missing key or a status other than `ok` is not a failure. The body may also carry `pending_quota` (same fields as `PUT /api/namespaces/{name}/quota`): the quota the platform will push right after the check (plan change), used instead of the live limits of `{namespace}-quota`; `PATCH` ignores it.
- `DELETE /api/releases/{namespace}/{name}` - Uninstall release. The release's leftover PVCs / CDI DataVolumes (kept by #317) are labelled `paas.woowtech.io/orphaned-at` + `grace-period-days` for the chart's pvc-cleanup CronJob. Query: `subdomain` (Cloudflare route to delete); `grace_period_days` (#1431, optional) shortens the retention of those orphans to N days — the platform sends `1` for a service that never reached `running`. Only `1 ≤ N ≤ PVC_GRACE_PERIOD_DAYS` is honoured; anything else (missing, non-integer, out of range) falls back to `PVC_GRACE_PERIOD_DAYS` and never fails the delete. Operators older than #1431 ignore the parameter (FastAPI drops undeclared query parameters).
- `POST /api/releases/{namespace}/{name}/rollback` - Rollback release. With `preserve_value_paths` (#1433) the rollback is one `helm upgrade` and runs the same preflight as `PATCH`; without it it is a plain `helm rollback`. #1526: both modes refuse a target revision whose chart is older than the deployed one (409 `rollback_chart_downgrade`). `check_compute_quota: true` (#1647) is honoured by both modes: the preserving rollback judges its dry-run manifest, a plain `helm rollback` (#1685; earlier operators ignored the flag there) judges the target revision's stored manifest (`helm get manifest --revision <n>`, hooks excluded) together with the live Deployments／StatefulSets — same rules, same 409 `workspace_compute_quota_exceeded`, nothing rolled back. For the plain rollback an unreadable or unparseable stored manifest, unreadable live objects or any error in the check is allowed and logged as `Compute quota pre-check skipped for rollback of …` (the manifest itself is never logged — it may hold Secrets); `COMPUTE_QUOTA_PRECHECK_ENABLED=false` skips it without reading anything. The check and the rollback hold one helm slot (#1552), so a request that passed the check is not queued again; the response shape is unchanged (no `compute_quota` key, only the 409 means "does not fit").
- `GET /api/releases/{namespace}/{name}/revisions` - Get revision history
- `POST /api/releases/{namespace}/{name}/restart` - (#1432) `kubectl rollout restart` every Deployment **and** StatefulSet labelled `app.kubernetes.io/instance={name}`; issued, not awaited (`rollout_awaited: false`). VirtualMachines are not touched. 404 when the release has no Deployment/StatefulSet.
- `POST /api/releases/{namespace}/{name}/route/park` - (woow_paas_platform #1563) Body `{"subdomain": "<one DNS label>"}`. Points **every** ingress rule of `<subdomain>.<CLOUDFLARE_DOMAIN>` (path rules included) at the shared suspended page `http://<SUSPENDED_PAGE_SERVICE>.<cloudflared namespace>.svc.cluster.local:<SUSPENDED_PAGE_PORT>` (defaults `paas-suspended-page`／`8080`; the namespace is `CLOUDFLARED_NAMESPACE` or derived from `PAAS_ENV`; deployed by paas-app-bundle-cd). The target comes from settings only — the caller cannot choose it. Same whole-config read-modify-write as route creation; `path`/`originRequest`, rule order, other hostnames and the catch-all are untouched. Rules of the hostname that are `http_status:*` (e.g. the 404 next to an MCP secret-path rule) are kept as they are — parking them would let unpark turn them into the service URL. 200 with `status` = `parked` / `already_parked` (no PUT) / `not_found` (no rule for this hostname) / `no_service_rule` (only `http_status:*` rules) / `disabled` (Cloudflare integration off). Refusals change nothing: 409 `suspended_page_not_configured`, 409 `suspended_page_unavailable` (the page Service does not exist), 503 `suspended_page_check_failed`, 409 `route_not_owned` (a rule points outside `{namespace}`), 409 `route_multiple_targets` (paths split across services — unpark could not restore them), 502 `cloudflare_error`. Never 404: the platform reads 404 as "older operator without this endpoint".
- `POST /api/releases/{namespace}/{name}/route/unpark` - (#1563) Body `{"expose": <the install-time expose config>}`. Rewrites only the rules that point at the suspended page back to the release's service, re-derived with the same logic as install (`_resolve_route_target`, no 3 s wait). 200 with `status` = `unparked` / `not_parked` (nothing pointed at the page; no lookup, no PUT) / `not_found` / `disabled`. 409 `route_target_unresolved` when the release has no usable Service (the hostname stays on the page), 502 `cloudflare_error`.
- `POST /api/releases/{namespace}/{name}/retrieval` - (woow_paas_platform #1540) Opens a **temporary, read-only** data retrieval entrance for a suspended (all workloads at 0 replicas) or deleted (data PVC still carries `paas.woowtech.io/orphaned-at`) release. Body `{"profile": "nextcloud", "ttl_hours": 72}`; the profile is an operator whitelist (`src/services/data_retrieval.py`: which PVC, which uid, which paths) — the caller cannot pick a PVC or path. Creates `paas-retrieval-<release>` Secret (htpasswd, `{SSHA}` hash only) → ConfigMap (nginx) → NetworkPolicy (ingress only from the cloudflared namespace, no egress) → Service → Pod (`DATA_RETRIEVAL_IMAGE`, PVC mounted `readOnly` at volume and mount level, nginx `autoindex` GET/HEAD only, basic auth, uid 33, restricted securityContext, no fsGroup, `activeDeadlineSeconds` = lifetime), then a tunnel route on a fresh random hostname (`paas-dl-<hex>`, stg `paas-stg-dl-<hex>`). 201 returns `hostname`/`url`/`username`/**`password` (only here, never stored)**/`expires_at`/`data_expires_at`. Lifetime = min(`ttl_hours`, `DATA_RETRIEVAL_MAX_TTL_HOURS`, deleted volume's retention). Refusals change nothing: 422 `retrieval_profile_unknown`, 409 `retrieval_already_open` / `retrieval_release_running` / `retrieval_pvc_in_use` (a live pod still mounts the volume) / `retrieval_pvc_not_found` / `data_retention_expired` / `retrieval_quota_exceeded` / `retrieval_closing`, 502 `cloudflare_error` (k8s objects rolled back), 503 `kubectl_error`. Never 404.
- `GET /api/releases/{namespace}/{name}/retrieval` - (#1540) `status` = `active` (pod ready) / `starting` / `stopped` (pod gone or past its deadline) / `not_found`, plus `hostname`, `expires_at`. Never returns the password.
- `DELETE /api/releases/{namespace}/{name}/retrieval?subdomain=` - (#1540) Idempotent close: deletes the k8s objects first (the volume is released at once), then the route. The route is removed only when every rule of the hostname points at this release's retrieval Service (`route` = `deleted` / `not_found` / `not_owned` / `none` / `disabled`). `subdomain` (must look like `paas-[env-]dl-<hex>`) is used only when the objects are already gone. 502 `route_cleanup_failed` (pod already stopped; retry).
- `POST /api/retrievals/sweep` - (#1540) Closes every entrance of this operator (namespace prefix + `paas.woowtech.io/env`) whose `paas.woowtech.io/retrieval-expires-at` has passed. The platform cron calls it every run; the pod's `activeDeadlineSeconds` stops serving at expiry even without it.
- `GET /api/releases/{namespace}/{name}/status` - Get release and pod status. (woow_paas_platform #1647) `?include_workloads=true` also returns `workloads`: the release's Deployments／StatefulSets with `desired_replicas`／`replicas`／`ready_replicas`, the `replica_failure` condition and the latest controller warning (`last_warning`) — used to notice a workload that should have pods but could not create a single one (`FailedCreate: exceeded quota`). `workloads_included` echoes the flag; a failed lookup still returns 200 with `workloads: null` and `workload_retrieval_error`.
- `GET /api/releases/{namespace}/{name}/public-address` - Get the public address the release's ngrok sidecar advertises on its container log: `tcp://host:port` (charts/emqx, native MQTT) or `https://host[:port]` (charts/headscale, control URL). Contract: the sidecar container is named `ngrok` and prints `WOOW_PUBLIC_URL=<url>` (legacy `WOOW_MQTT_PUBLIC_URL=` is still honoured) at the **start of a line**; the last valid line wins and an empty value retracts the address. `public_url: null` is a normal state (not enabled / agent not connected yet / retracted), not an error. #1559: when there is no address and the sidecar log says why, the response also carries `error_code` (closed set: `ERR_NGROK_<n>` — only a whole line `ERROR:  ERR_NGROK_<n>` counts — `NO_AUTHTOKEN`, `SERVER_URL_MISMATCH`) and `error` (fixed Chinese text). Raw log lines are never returned (ngrok echoes the authtoken on auth failures).
- `GET /api/releases/{namespace}/{name}/mqtt-address` - **Legacy** (called by platform ≤ 18.0.1.0.205): same contract, `tcp://` addresses only. Keep until no deployed platform calls it.

#### Restart scope and rollback feature switches (AI-OD-004-2／AI-OD-006-1)

- `POST /api/releases/{namespace}/{name}/restart?component=<label value>` narrows the
  restart to workloads labelled `app.kubernetes.io/component=<value>` (DNS-1123 label,
  422 otherwise; 404 when none matches). The platform uses it after an API-key change
  (#1435) to roll only the workload that consumes the key env (open-design: `daemon`), and
  retries without it on 404. The response echoes `component`. Without the parameter the
  endpoint restarts every Deployment and StatefulSet as before (tenant restart, #1432).
- `POST /api/releases/{namespace}/{name}/rollback` accepts `preserve_when_equal`
  (`{switch path: [guard paths]}`, e.g. `{"mcp.enabled": ["image"]}`, max 16 switches ×
  8 guards; requires `chart`, selects the preserving rollback like `preserve_value_paths`).
  A switch keeps its current state (absence included) only when every guard path is
  identical in the target and the current values; otherwise it follows the target revision
  together with its guards. The response lists the paths (never values) in
  `feature_paths_preserved`／`feature_paths_reverted`.
- Older operators ignore both (FastAPI drops undeclared query parameters, pydantic ignores
  unknown body fields): the restart covers everything and switches follow the target.

### Namespaces

- `POST /api/namespaces` - Create namespace with resource quota
- `POST /api/namespaces/{name}/baseline-netpol` - Idempotent backfill of the tenant baseline on an existing workspace namespace (#279 P5): the four ingress NetworkPolicies, the managed labels and — when their switches are on — the #1441 egress baseline and Pod Security labels (below).
- `PUT /api/namespaces/{name}/quota` - (#278) Overwrite the namespace ResourceQuota as a whole (`cpu_limit` / `memory_limit` / `storage_limit`, optional `ephemeral_storage_*`); the platform recomputes the values from the workspace's live services and pushes them.
- `GET /api/namespaces/{name}/quota` - (#1647) Read-only: the ResourceQuota `hard` / `used` of the namespace, i.e. how much of the quota is reserved (declared requests / limits and PVC sizes), not live usage. Always `200`: `available: true` with `resources` keyed by the k8s resource name (`limits.cpu`, `limits.memory`, `requests.storage`, `persistentvolumeclaims`, …; each `{hard, used, hard_value, used_value, unit, ratio, quota}`, the tightest quota wins when several declare the same resource), or `available: false` with `reason` `quota_not_found` (no unscoped ResourceQuota — a missing namespace looks the same) / `quota_unreadable` (kubectl failed or timed out). `400` only for a namespace outside the allowed prefix. An operator older than this endpoint answers the GET with `405` (the path exists for `PUT` only).

#### Managed quota observation (#1678)

`GET /api/namespaces/{name}/quota?include_managed=true` adds `managed_quota` to
**the otherwise unchanged #1647 response**. Both projections use the same bounded
ResourceQuota list (5 seconds). The default/false query still returns exactly the
legacy payload; the frontend's tightest-quota summary is not a safety observation.

The managed object must have the existing platform name `{namespace}-quota` and
matching namespace. Its `namespace`, `name`, `uid`, `resource_version`, raw
`status.hard` / `status.used` maps (`hard` / `used`), and `spec_hard` belong to that
one object. `available=true` requires complete identity and valid, nonnegative
quantities for `requests.cpu`, `limits.cpu`, `requests.memory`, `limits.memory`,
`requests.storage`; the five status hard values must agree with spec hard values.
Explicit quantity `0` is known; a missing/invalid value is **never** filled with 0.
No status hard fallback to spec hard is permitted. Scoped objects are unavailable.

`observed_at` is the UTC time of this read attempt, **not** evidence that the quota
controller has reconciled at that instant, capacity reserved, or admission and
observation made atomic. `managed_quota_status_pending` distinguishes spec/status
lag. Other machine-readable reasons are `managed_quota_not_found`,
`managed_quota_scoped`, `managed_quota_identity_incomplete`,
`managed_quota_namespace_mismatch`, `managed_quota_incomplete`,
`managed_quota_invalid_quantity`, and `quota_unreadable`.

A successful empty quota list does not prove whether the namespace exists.
`managed_quota_not_found` means no unique managed object was observed, **not**
"not yet deployed" or permission to skip a safety check. For that reason only,
the operator runs one extra `kubectl get namespace` (#1679, 3s cap) and reports
`namespace_exists`: `true` when the namespace is there, `false` **only** for a
definitive NotFound, and `null` for Forbidden, timeout or any other failure.
When the managed quota is found, `namespace_exists` is `true`; when the quota
read itself fails it stays `null`. The platform treats `null` as unknown and
never as "not created yet". A forbidden/failed/timed-out read is `quota_unreadable`, not an empty list.
An old operator omitting `managed_quota` is unsupported/unknown in the backend;
Odoo never falls back to the frontend summary. Deploy compatible operator first.

The Odoo backend is a system-administrator-only read projection: one observation
per Workspace per compute batch, at most five HTTP reads per batch, list page size
five, existing `(3, 8)` connect/read timeout. Larger RPC batches leave additional
rows unknown (`observation_batch_limit`); opening a form/refresh reads again.
There is no stored snapshot, cron, quota write, or override introduced here.

The opt-in real-SA read gate is `tests/test_managed_namespace_quota_sa_1678.py`
(marked `integration`, deliberately outside the broad integration fixture suite).
Supply an owned namespace beginning `dev-sandbox-quota1678-`, an actual running
operator using its projected ServiceAccount token, and externally verified quota
identity. It creates no fixture resources and requires only quota get/list:

```bash
# Run inside the isolated operator pod with its own in-cluster kubeconfig.
# Fixtures: managed hard = CPU requests 2 / limits 7; memory 3Gi / 5Gi;
# storage 11Gi; all used = 0; diagnostic-other quota CPU limits = 1.
export QUOTA1678_OPERATOR_URL=http://127.0.0.1:8000
export QUOTA1678_NAMESPACE=dev-sandbox-quota1678-1678cafe
export QUOTA1678_API_KEY=<isolated-test-key>
export QUOTA1678_QUOTA_UID=<verified-live-uid>
export QUOTA1678_QUOTA_VERSION=<verified-live-resource-version>
python -m pytest -m integration tests/test_managed_namespace_quota_sa_1678.py -v
```

Odoo `TestWorkspaceQuotaInspectionLive1678` uses those same identity variables and
an operator URL reachable from its dedicated sandbox pod. Set
`REQUIRE_QUOTA1678_LIVE=1`; `QUOTA1678_CPU_LIMIT=9` checks a fixture-only update;
`QUOTA1678_EXPECT_AVAILABLE=false` checks absence of the managed object. Without
an explicit live fixture the class is skipped, not evidence that this gate passed.
Both gates supplement, **not replace**, the required headed backend smoke.
Minimal namespace-local test RBAC does not verify the deployed production chart's
RBAC or operator image packaging.

#### Workspace = its own LAN (#1441 / #1436 / #514)

Both switches default **off** (behaviour unchanged); they are flipped per environment through the paas-operator chart (`config.tenantBaselineEgress`, `config.tenantPodSecurity`) after the existing namespaces have been backfilled. Manifests come from `src/services/tenant_baseline.py` (pure functions, shared with the backfill script).

- `TENANT_BASELINE_EGRESS_ENABLED` — adds NetworkPolicy `paas-baseline-egress` (`podSelector: {}`, Egress only): same namespace on every port, kube-dns pods on 53, and the internet `0.0.0.0/0` except `10.0.0.0/8` (pod/Service CIDR, subnet broadcasts), `172.16.0.0/12`, `192.168.0.0/16` (node IPs, kube-apiserver :6443, NodePorts), `169.254.0.0/16` (metadata), `100.64.0.0/10`, `224.0.0.0/4` (mDNS/SSDP), `240.0.0.0/4` (255.255.255.255). NetworkPolicies are a union: a chart rule that is wider (`- {}`, an un-excepted `0.0.0.0/0`) cancels this baseline for the pods it selects — woow-paas-charts enforces that with `scripts/np-audit.py` in CI. Charts that need the kube-apiserver (open-design console, omnigent server) keep their own narrow rule.
- `TENANT_POD_SECURITY_ENABLED` — labels the namespace `pod-security.kubernetes.io/enforce=baseline` (+ `warn`/`audit=restricted`, versions pinned by `TENANT_PSA_VERSION`, default `v1.34`). Namespaces in `PSA_EXEMPT_NAMESPACES` (default: only the administrators' Omnigent, `paas-ws-ab6564da`) get no PSA labels and an annotation `paas.woowtech.io/psa-exempt-reason`.
- Existing namespaces: `python3 scripts/ws_lan_backfill.py --ns <ns,...>` previews (server-side dry-run, lists pods that would violate PSA), `--apply` writes, `--rollback [--apply]` removes the egress policy and the PSA labels.

### Cluster (read-only)

- `GET /api/cluster/storage-capacity?storage_class=<sc>` - Longhorn scheduling capacity (#1415): per node limit / scheduled / headroom / ratio, the largest new volume that still fits with all replicas (and degraded), and orphaned PVCs (`paas.woowtech.io/orphaned-at`, all namespaces). Always 200; `available: false` + `reason` when Longhorn cannot be read. Polled by the platform monitor cron.

### Routes (Cloudflare Tunnel)

- `GET /api/routes` - List all tunnel routes
- `POST /api/routes` - Create a tunnel route
- `DELETE /api/routes/{subdomain}` - Delete a tunnel route

## Quick Start

### Prerequisites

- Python 3.11+
- Helm 3.13+
- kubectl 1.28+
- Kubernetes cluster access

### Local Development

1. **Install dependencies:**

```bash
pip install -r requirements.txt
```

2. **Configure environment:**

```bash
cat > .env <<EOF
API_KEY=your-secret-key
LOG_LEVEL=info
NAMESPACE_PREFIX=paas-ws-
HELM_BINARY=/usr/local/bin/helm
HELM_TIMEOUT=300
EOF
```

3. **Run the service:**

```bash
cd src
python -m uvicorn main:app --reload --port 8000
```

4. **Access the API:**

- API Documentation: http://localhost:8000/docs
- Health Check: http://localhost:8000/health

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_helm.py -v
```

## Deployment

### Build Docker Image

```bash
docker build -t paas-operator:latest .
```

### Deploy to Kubernetes with Helm

1. **Generate API Key:**

```bash
openssl rand -base64 32
```

2. **Install with Helm:**

```bash
# Install with custom API key
helm install paas-operator ./helm \
  --namespace paas-system \
  --create-namespace \
  --set auth.apiKey="YOUR_GENERATED_KEY"

# Or use an existing secret
helm install paas-operator ./helm \
  --namespace paas-system \
  --create-namespace \
  --set auth.existingSecret="my-secret" \
  --set auth.existingSecretKey="api-key"
```

3. **Customize configuration:**

```bash
# Override values
helm install paas-operator ./helm \
  --namespace paas-system \
  --create-namespace \
  --set auth.apiKey="YOUR_KEY" \
  --set replicaCount=3 \
  --set resources.limits.memory=1Gi
```

Or create a `values-prod.yaml`:

```yaml
replicaCount: 3
auth:
  apiKey: "your-secure-key"
resources:
  limits:
    cpu: 1000m
    memory: 1Gi
```

```bash
helm install paas-operator ./helm -f values-prod.yaml --namespace paas-system
```

4. **Verify deployment:**

```bash
# Check pods
kubectl get pods -n paas-system -l app.kubernetes.io/name=paas-operator

# Check service
kubectl get svc -n paas-system paas-operator

# Test health endpoint
kubectl run test-curl --rm -it --restart=Never --image=curlimages/curl -- \
  curl http://paas-operator.paas-system/health
```

5. **Upgrade:**

```bash
helm upgrade paas-operator ./helm --namespace paas-system --set image.tag=v1.1.0
```

6. **Uninstall:**

```bash
helm uninstall paas-operator --namespace paas-system
```

### Access the Service

From within the cluster:

```bash
curl -H "X-API-Key: YOUR_API_KEY" \
  http://paas-operator/api/releases/paas-ws-test/myapp
```

## Configuration

Environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `API_KEY` | API authentication key | "" |
| `HOST` | Service host | 0.0.0.0 |
| `PORT` | Service port | 8000 |
| `LOG_LEVEL` | Logging level | info |
| `NAMESPACE_PREFIX` | Allowed namespace prefix | paas-ws- |
| `HELM_BINARY` | Path to Helm binary | /usr/local/bin/helm |
| `HELM_TIMEOUT` | Helm command timeout (seconds) | 300 |
| `HELM_MAX_CONCURRENT_OPERATIONS` | #1552: max concurrent helm install／upgrade／rollback／uninstall (incl. dry-run); extra requests queue. Read-only helm calls are not limited | 3 |
| `HELM_QUEUE_TIMEOUT_SECONDS` | #1552: max seconds a request waits for a helm slot; then **503** `detail.error_code=operator_busy` (nothing applied, safe to retry). reset-config uses its own 8s (`RESET_QUEUE_TIMEOUT_SECONDS`, part of the #461 ladder) | 45 |
| `HELM_DISABLE_OPENAPI_VALIDATION` | #1552: pass `--disable-openapi-validation` to install／upgrade so helm does not download and parse `/openapi/v2` (~150MiB per process on this cluster). On k8s ≥ 1.27 helm 3.13 skips client-side schema validation anyway; the API server still validates. `false` = old behaviour | true |
| `CORS_ORIGINS` | Comma-separated allowed CORS origins | "" (none) |

### Cloudflare Integration (Optional)

| Variable | Description | Default |
|----------|-------------|---------|
| `CLOUDFLARE_ENABLED` | Enable Cloudflare integration | false |
| `CLOUDFLARE_API_TOKEN` | API token with Tunnel:Edit and DNS:Edit permissions | "" |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID | "" |
| `CLOUDFLARE_TUNNEL_ID` | Tunnel ID from Zero Trust dashboard | "" |
| `CLOUDFLARE_ZONE_ID` | Zone ID for DNS management (optional) | "" |
| `CLOUDFLARE_DOMAIN` | Base domain (e.g., woowtech.io) | "" |
| `SUSPENDED_PAGE_SERVICE` | #1563: Service name of the shared suspended page in the cloudflared namespace (`CLOUDFLARED_NAMESPACE`, else `paas-cloudflare-system`／`paas-stg-cloudflare-system` from `PAAS_ENV`). Empty disables `route/park` | paas-suspended-page |
| `SUSPENDED_PAGE_PORT` | #1563: port of that Service | 8080 |
| `DATA_RETRIEVAL_IMAGE` | #1540: image of the read-only retrieval pod (same digest as the suspended page / platform nginx sidecar) | `docker.io/nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3…c4c0` |
| `DATA_RETRIEVAL_MAX_TTL_HOURS` | #1540: longest lifetime of one entrance | 72 |
| `DATA_RETRIEVAL_MIN_REMAINING_MINUTES` | #1540: refuse to open a deleted service's volume that is reclaimed sooner than this | 60 |
| `DATA_RETRIEVAL_EXCLUDED_NODES` | #1540: comma-separated node names the retrieval pod must not be scheduled on | "" |

> **Note**: If `CLOUDFLARE_ZONE_ID` is not set, DNS records must be managed manually. Routes will still be created in the tunnel configuration.

## Security

### Namespace Enforcement

All operations are restricted to namespaces starting with the `paas-ws-` prefix. **This prefix boundary is enforced at the application layer only** — `validate_namespace()` in [`src/services/helm.py:79-84`](src/services/helm.py) rejects any namespace that does not start with `settings.namespace_prefix` (`paas-ws-`) before a Helm/kubectl call is issued, and it is invoked at the entry of every mutating `HelmService` / `KubernetesService` method (the single choke-point through which all shell-outs pass).

> **⚠️ RBAC does _not_ enforce the prefix.** The operator's `ClusterRole` (`paas-operator`, and the `paas-operator-stg` variant) grants verbs on resource *types* (`namespaces`, `pods`, `secrets`, `persistentvolumeclaims`, `deployments`, `statefulsets`, `ingresses`, `networkpolicies`, `resourcequotas`, `roles`/`rolebindings`, …) with **no `resourceNames` restriction**, bound cluster-wide via a `ClusterRoleBinding`. At the RBAC layer the ServiceAccount is therefore authorized across **all** namespaces, not just `paas-ws-*`. Kubernetes RBAC structurally *cannot* express a `paas-ws-*` prefix boundary: `resourceNames` matches exact object **names** (a different axis from namespace), supports no wildcard/prefix, and is ignored for `list`/`watch`/`create`; namespace scoping is only achievable via a namespaced `Role` bound per-namespace — impractical here because the operator must **create** namespaces on demand with dynamic `paas-ws-{hash}` names that cannot be pre-enumerated. Treat this `ClusterRole` as **operational authorization** (which verbs on which resource *types*), **not** a tenant-isolation boundary. Rationale, ground-truth manifest, and the defense-in-depth decision (admission-layer webhook as a post-launch hardening candidate) are recorded in [`docs/decisions/namespace-prefix-enforcement-app-layer-not-rbac.md`](../../docs/decisions/namespace-prefix-enforcement-app-layer-not-rbac.md).

### API Key Authentication

All endpoints (except `/health` and `/docs`) require the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-secret-key" \
  http://paas-operator/api/releases/paas-ws-test/myapp
```

### RBAC Permissions

The service uses a dedicated ServiceAccount (`paas-system/paas-operator`) bound cluster-wide via `ClusterRoleBinding` to the `paas-operator` `ClusterRole`. The role grants full RWCU verbs on the following resource **types** (with **no `resourceNames` restriction** — i.e. cluster-wide across every namespace; see the ⚠️ note under [Namespace Enforcement](#namespace-enforcement)):

- `namespaces`
- Core resources: `pods` (+ `pods/log`, `pods/exec`), `services`, `serviceaccounts`, `configmaps`, `secrets`, `persistentvolumeclaims`, `events`
- Apps: `deployments`, `statefulsets`, `replicasets`, `daemonsets` (+ `*/scale`)
- Networking: `ingresses`, `networkpolicies`
- `resourcequotas`, `poddisruptionbudgets`, `horizontalpodautoscalers`, `leases`
- `jobs`, `cronjobs`
- Cluster-scoped: `persistentvolumes` (get/list/patch/delete), `storageclasses` (get/list)
- Longhorn (read-only, chart ≥ 0.1.22, #1415): `nodes.longhorn.io`, `settings.longhorn.io` (get/list) — without it the capacity pre-check fails open
- RBAC: `roles`, `rolebindings` (RWCU — needed for per-tenant Role provisioning)

> The `ClusterRole` is defined and published from the cross-repo **`woow-paas-charts`** `paas-operator` chart and applied via Fleet (`paas-app-bundle-cd`); it is _not_ managed from this repo. Edit it there, not with a local `helm upgrade`.

### YAML Injection Prevention

All Kubernetes manifests are created using `yaml.safe_dump()` to prevent YAML injection attacks. User inputs are never directly interpolated into YAML strings.

### CORS Configuration

For cross-origin requests, configure allowed origins:

```bash
# Allow specific origins
CORS_ORIGINS=https://odoo.example.com,https://admin.example.com

# Leave empty to block all cross-origin requests (recommended for internal services)
CORS_ORIGINS=
```

## Error Handling

The service provides structured error responses for all failure cases:

### Error Response Format

```json
{
  "detail": "Human-readable error message"
}
```

### HTTP Status Codes

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad Request - Invalid input or namespace |
| 401 | Unauthorized - Missing or invalid API key |
| 404 | Not Found - Release or namespace doesn't exist |
| 409 | Conflict - Resource already exists |
| 500 | Internal Server Error - Helm command failed |

### Sanitized Error Messages

All error messages returned to clients are sanitized to prevent information disclosure:

- Internal paths and filenames are removed
- Helm stderr output is logged but not exposed to clients
- Stack traces are never included in responses

### Fail-Fast Startup

The service validates Helm availability on startup. If Helm is not accessible, the service will fail to start with a clear error message:

```
CRITICAL: Helm not available: {error} - Service cannot start!
```

## Usage Examples

### Install a Helm Chart

```bash
curl -X POST http://paas-operator/api/releases \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "paas-ws-demo",
    "name": "my-nginx",
    "chart": "nginx",
    "version": "15.0.0",
    "create_namespace": true,
    "values": {
      "replicaCount": 2,
      "service": {
        "type": "ClusterIP"
      }
    }
  }'
```

### Get Release Status

```bash
curl http://paas-operator/api/releases/paas-ws-demo/my-nginx/status \
  -H "X-API-Key: your-key"
```

### Upgrade Release

```bash
curl -X PATCH http://paas-operator/api/releases/paas-ws-demo/my-nginx \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "15.1.0",
    "values": {
      "replicaCount": 3
    }
  }'
```

### Rollback Release

```bash
curl -X POST http://paas-operator/api/releases/paas-ws-demo/my-nginx/rollback \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "revision": 1
  }'
```

目標 revision 的 chart 版本不得低於目前部署中的版本（#1526）：違反時回 409，`detail.error_code` 為
`rollback_chart_downgrade`（另帶 `target_chart_version`／`current_chart_version`）。成功的回應帶
`mode`（`rollback`／`upgrade_preserving`）與回復後的 `chart_version`。帶 `chart`＋`preserve_value_paths`
時改走保留這些路徑目前值的 helm upgrade（#1433）。

### Create Namespace with Quota

```bash
curl -X POST http://paas-operator/api/namespaces \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "paas-ws-new-tenant",
    "cpu_limit": "4",
    "memory_limit": "8Gi",
    "storage_limit": "50Gi"
  }'
```

### Create Cloudflare Tunnel Route

```bash
curl -X POST http://paas-operator/api/routes \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "subdomain": "myapp-demo",
    "service_url": "http://my-nginx.paas-ws-demo.svc.cluster.local:80"
  }'
```

### List All Tunnel Routes

```bash
curl http://paas-operator/api/routes \
  -H "X-API-Key: your-key"
```

### Delete a Tunnel Route

```bash
curl -X DELETE http://paas-operator/api/routes/myapp-demo \
  -H "X-API-Key: your-key"
```

## Monitoring

### Health Checks

The service includes Kubernetes liveness and readiness probes:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 10
  periodSeconds: 30

readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 5
  periodSeconds: 10
```

### Logging

Structured logging with configurable levels:

```python
logger.info("Installed release my-nginx in paas-ws-demo")
logger.error(f"Helm install failed: {error_message}")
```

## Troubleshooting

### Helm Command Fails

Check logs:

```bash
kubectl logs -n paas-system -l app.kubernetes.io/name=paas-operator --tail=100
```

Common issues:
- Helm binary not found → Check `HELM_BINARY` path
- Permission denied → Verify RBAC configuration
- Timeout → Increase `HELM_TIMEOUT` setting

### Authentication Errors

- Verify API key in Secret matches client requests
- Check `X-API-Key` header is present
- Health endpoint doesn't require authentication

### Namespace Access Denied

- Ensure namespace starts with `paas-ws-` prefix
- Verify RBAC permissions include the namespace
- Check ClusterRoleBinding is correctly applied

### Cloudflare Integration Issues

**Route creation fails:**
- Verify `CLOUDFLARE_API_TOKEN` has correct permissions (Tunnel:Edit, DNS:Edit)
- Check `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_TUNNEL_ID` are correct
- Ensure the tunnel is running and connected

**DNS records not created:**
- Confirm `CLOUDFLARE_ZONE_ID` is set (required for DNS management)
- Verify the domain matches the zone

**Check Cloudflare status:**

```bash
# View operator logs for Cloudflare errors
kubectl logs -n paas-system -l app.kubernetes.io/name=paas-operator | grep -i cloudflare
```

## Development

### Code Structure

```
src/
├── main.py              # FastAPI app + middleware
├── config.py            # Settings management
├── api/
│   ├── releases.py      # Release endpoints
│   ├── namespaces.py    # Namespace endpoints
│   └── routes.py        # Cloudflare route endpoints
├── services/
│   ├── helm.py          # Helm/kubectl wrappers
│   └── cloudflare.py    # Cloudflare Tunnel API client
└── models/
    └── schemas.py       # Pydantic models
```

### Adding New Endpoints

1. Define Pydantic schema in `models/schemas.py`
2. Implement service logic in `services/helm.py`
3. Create API endpoint in `api/`
4. Add tests in `tests/`
5. Update this README

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio pytest-cov

# Run tests
pytest -v

# Generate coverage report
pytest --cov=src --cov-report=term-missing
```

## License

Proprietary - Woow PaaS Platform

## Support

For issues or questions, contact the PaaS Platform team.
