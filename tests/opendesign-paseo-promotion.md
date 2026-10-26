# OpenDesign Paseo product promotion — candidate pinned, awaiting live acceptance

The source base is platform develop `198150fe` / module `18.0.1.0.265`.
Chart `0.5.9` is published with OCI manifest
`sha256:74f45b9960efbe84f3b89fabff2736e720f853d8edbcd930215ccc08edec024e`;
its runtime paths match published `0.5.8`. The candidate includes fork `woow-od-v0.24.1-w2.5`
(`c8ae3900f9ff5d58f50d11f4055756f6e7f14a25`) and the runtime wrapper at
`/opt/opendesign-paseo/bin/serve.mjs` and shared runtime `0.2.1`.
Coordinator-supplied build 28347 passed default startup, native Paseo and full MCP:
`20261026.0712@sha256:9de8850209a18d18579d81442f9d079076a76ef9128970296435abdb8c144ab2`.
The earlier `20261026.0654` wrapper is rejected because its final ENTRYPOINT
cleared inherited CMD. **Native HA acceptance and production promotion remain pending**.

The seven-line change to `_inject_allowed_origins` uses the existing unified
values assembly path. Only an explicitly enabled OpenDesign Paseo receives
`paseo.origin` equal to the service's `_public_base_url()`. The shared seed stores
an empty origin, never one tenant's hostname. Legacy/off values remain off.

This review branch promotes only the source seed to chart `0.5.9`, the accepted
candidate image, and native Paseo. It does not change live shared product 13 or
existing services. Further artifact updates can generate a reviewable patch with:

```sh
python3 scripts/release/prepare_opendesign_paseo.py \
  --image-tag 'YYYYMMDD.HHMM@sha256:<reviewed-64-character-digest>' > /tmp/opendesign-default.patch
git apply --check /tmp/opendesign-default.patch
```

This command emits a diff and never edits files or contacts Odoo/Kubernetes.
Missing, malformed, feature-tagged, or unchanged old image pins fail before any
patch is emitted. Digest syntax does not establish image compatibility; the
coordinator must verify the actual artifact before applying the patch.

The diff changes only `cloud_app_opendesign`'s chart, daemon image, Paseo enable
flag, and image-coupled rollback switch, plus the native seed assertions.
Console/MCP/Omnigent settings, authentication, resource limits, PVCs, subscription
plans and all other product records are retained. The seed uses `noupdate=0`;
the existing `_heal_sticky_helm_value_specs` healer clears historical sticky flags
at the end of XML loading. On a legacy database whose flag is still true, the
first upgrade clears it and a subsequent reload applies seed changes. Verify
the actual XML-ID flag and final fields during the coordinator's upgrade test.

New installs merge these defaults through `PaasController._deep_merge` and store
their own JSON values. Existing services retain their feature choices; only an
explicit redeploy refreshes image references. The native `deployment_policy`
resolver continues to select administrator pins for services 10/244; promotion
does not write service records, redeploy them, or unpin them.

Offline validation:

```sh
python3 -m pytest -q tests/test_deployment_target.py tests/test_opendesign_paseo_promotion.py
python3 scripts/audit/test_collection_audit.py --check-baseline
```

The fixture uses synthetic digests and checks patch idempotence/applicability,
new installs, default-following redeploys, exact origin binding and both pinned
service shapes. Synthetic manifests are test artifacts, never rollout inputs.
Native Odoo integration cases are added to `TestServiceDeploymentPin` and
`TestOpendesignTemplateSeed`. All 39 cases (including the four new cases) passed
on 2026-10-26 in `dev-sandbox-od-paseo-release-20261026`, database
`od_paseo_native`. The release sandbox uses a fresh native database, a full
checkout overlay with file hashes verified, install without tests followed by
selected post-install transaction cases, and disabled cron. The first run found
one test expectation that omitted native storage-class injection; the corrected
test verifies the two preserved volume definitions independently.
The 61 offline cases, repository lint and collection audit also pass.
A canary default-service test and the
coordinator's live acceptance must precede production shared-default promotion.
Manifest bumps remain in the normal separate `release/*` lane.
