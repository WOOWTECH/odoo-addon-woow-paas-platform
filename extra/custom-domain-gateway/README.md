# PaaS custom-domain gateway

This HTTP-only gateway is a separate origin for Cloudflare for SaaS customer
hostnames. It is not the existing shared tenant-service Tunnel. Each request is
routed by an exact, lowercase HTTP `Host` to one verified PaaS Odoo Service;
unknown hosts receive 404. The original Host and URL are passed through so
Odoo Website can select the intended site. WebSockets and generic PaaS
workloads are intentionally unsupported.

The Operator owns `paas-custom-domain-routes` ConfigMap's `routes.json` key,
using Kubernetes `resourceVersion` compare-and-swap. The gateway mounts it
read-only and drops all routes if the file is absent or invalid. The Helm chart
in `woow-paas-charts/charts/paas-custom-domain-gateway` owns only ConfigMap
metadata; it must not reset route data during an upgrade.

Before enabling onboarding, the platform must provide a distinct Cloudflare
Tunnel with one catch-all ingress rule to this Service, a proxied CNAME to that
Tunnel, an active Cloudflare for SaaS fallback origin in the platform zone, a scoped
Custom Hostnames API token, and a shared route-check secret for the gateway
and Operator. The Operator's capability probe verifies these prerequisites and
otherwise fails closed. Customers do not access the platform's Cloudflare
account; they publish ownership and cutover DNS records with their own DNS
provider.

Existing fallback origins stay in place. Each newly created customer hostname
selects the dedicated gateway through `custom_origin_server`. The optional
`CUSTOM_DOMAINS_API_TOKEN` setting reads an independent Kubernetes Secret with
zone-scoped SSL and Certificates Write permission; the existing Tunnel/DNS
credential remains separate.

Development tests: install `requirements-test.txt`, then run
`python -m pytest -q`. The runtime image uses only `requirements.txt`.
