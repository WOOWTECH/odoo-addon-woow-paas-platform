# OpenDesign Paseo product promotion — awaiting image digest

The source base is platform develop `c3f8c59d` / module `18.0.1.0.263`.
The matching chart candidate is `0.5.9`; its runtime paths match published
`0.5.8`. The image is expected to include fork `woow-od-v0.24.1-w2.5`
(`c8ae3900f9ff5d58f50d11f4055756f6e7f14a25`) and the runtime wrapper at
`/opt/opendesign-paseo/bin/serve.mjs`. **AWAITING_COORDINATOR_DIGEST**.

The seven-line change to `_inject_allowed_origins` uses the existing unified
values assembly path. Only an explicitly enabled OpenDesign Paseo receives
`paseo.origin` equal to the service's `_public_base_url()`. The shared seed stores
an empty origin, never one tenant's hostname. Legacy/off values remain off.

The product seed remains at `0.5.7` with its existing image until the reviewed
matching image is supplied. Generate a reviewable patch locally with:

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
`TestOpendesignTemplateSeed`; run those in the normal Odoo sandbox before merging,
then test the actual image and seed upgrade after the digest arrives. No sandbox
or production deployment is performed by this source-preparation subtask.
