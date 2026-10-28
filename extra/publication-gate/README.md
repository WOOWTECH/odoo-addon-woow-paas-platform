# Owned bounded publication gate

This package is the deployable extraction of the Stage E durable latch and
fresh-authority design for the REV01 HTTP cohort.  It supports exactly
`GET|HEAD /manifest.json`; every other data request denies.  The private control
listener requires mTLS and exposes `/close`, `/activate`, and the read-only
`/denial-receipts` endpoint.  A denial receipt is written only after a JWT has
passed signature, issuer, audience, expiry and app-token validation but its
email is absent from the fresh committed policy.  It stores only a one-way
session-cohort digest and the exact close generation/effect/digest; no cookie,
JWT, subject or email is persisted.  The receipt is audit evidence, never
authority to release publication or clear token-revocation pending state.

It is not a generic Home Assistant gateway and does not claim WebSocket or
helper-effect capability.  Its capability is the existing bounded-HTTP digest
`16aaf107...f76f`.  Restart begins closed even when the SQLite high-water file
survives.  Normal start refuses a missing latch; initialize it explicitly once
with `python -m publication_gate.main initialize` on the owned state volume.

Runtime files are mounted read-only under `/etc/woow-publication`; only the
SQLite directory is writable.  `gate.json` contains public topology and file
pointers, not private key material. `authority.json` pins the private Odoo
authority mTLS endpoint. Certificates and keys come from an owned Secret.

For live REV01, cloudflared targets gate port `18303`; the gate is injected as a
sidecar and its fixed upstream is the same pod's HA port `8123`. Candidate Odoo calls the normal operator;
the gate never calls Cloudflare and never receives an operator API key.

`deploy/render.py` emits a Secret-name-only, digest-pinned owned Deployment
patch plus Service, ConfigMap, and 64Mi state PVC. It does not apply resources.
The one-shot initialize Job creates the latch only during the explicit first deployment;
it must remain completed and must never be recreated on rollout. Normal gate startup will
not recreate missing state. Before cleanup, verify the
namespace owner label/UID; after deleting the PVC, separately verify that its
Longhorn backing volume is absent.

`deploy/render_candidate_stack.py` is the stricter live handoff renderer. It
emits the HA gate sidecar patch and a separate private native-Odoo authority
Deployment/Service/NetworkPolicy. It accepts Secret and PVC names only, never
their content, and refuses incomplete input. `deploy/test-values.json` is
explicitly non-live and exists only for structural rendering; live mode rejects
test/placeholder values. Rendering is not readiness and never applies or rolls
out the HA Deployment.

The gate image is not a generic base image: it must already contain the
`publication_gate` package and its locked dependencies. The renderer requires
source and requirements SHA-256 pins and records them as Pod annotations. Gate
identity files are projected `0440` and read through candidate-Pod fsGroup
65532; the same fsGroup is the kubelet ownership contract for the owned state
PVC. The native Odoo authority runs UID/GID/fsGroup 100 and receives its
projected identity files as `0440`. These settings are an isolated candidate
contract; the renderer does not patch a shared or production workload.
