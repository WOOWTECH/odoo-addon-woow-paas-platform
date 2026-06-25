# n8n-mcp Integration Research Notes

## Date: 2026-02-27

## 1. n8n Helm Chart Sidecar Support

- **Result**: Not Supported (no native `extraContainers` / `sidecars` key)
- **Chart inspected**: `oci://8gears.container-registry.com/library/n8n` version 2.0.1
- **Details**:
  - The chart's `values.yaml` does NOT provide `extraContainers`, `sidecars`, or `additionalContainers` keys.
  - The `deployment.yaml` template renders a single container (`{{ .Chart.Name }}`) and does not include a loop or injection point for sidecar containers.
  - Available extension points in the chart:
    - `main.extraEnv` - inject additional environment variables
    - `main.extraVolumes` / `main.extraVolumeMounts` - mount shared volumes
    - `main.initContainers` - run init containers before the main pod
    - `extraManifests` / `extraTemplateManifests` - render arbitrary K8s manifests alongside the release
  - The chart DOES support `extraManifests` which can render raw K8s resources, but this cannot inject a container into the existing Deployment spec.
- **Alternative approaches (recommended order)**:
  1. **PaaS Operator post-deploy patch** (Recommended): After Helm install, use the PaaS Operator to patch the Deployment via Kubernetes API to add the n8n-mcp sidecar container. This is the cleanest approach as it doesn't require forking the chart.
     ```python
     # Pseudocode for PaaS Operator
     kubectl patch deployment <n8n-release> -n <namespace> --type='json' \
       -p='[{"op":"add","path":"/spec/template/spec/containers/-","value":{...sidecar spec...}}]'
     ```
  2. **Custom Helm wrapper chart**: Create a thin wrapper chart that depends on the n8n chart and uses a post-render hook or Kustomize to inject the sidecar.
  3. **Fork the Helm chart**: Add `extraContainers` support to the forked chart. This is the most maintenance-heavy approach.

## 2. n8n API Key Environment Variable

- **Result**: Not Supported (no `N8N_API_KEY` env var for pre-provisioning)
- **Details**:
  - n8n does NOT support setting an API key via environment variable at deployment time.
  - API keys can only be created through the n8n UI: Settings > n8n API > Create an API key.
  - The n8n REST API itself requires an API key for authentication (header `X-N8N-API-KEY`), creating a chicken-and-egg problem for automation.
  - There is no public REST API endpoint to create an API key programmatically.
  - Community thread "Setting X-N8N-API-KEY without the frontend" confirms this is a known limitation with no official solution.
  - n8n stores API keys in its internal database (SQLite by default or PostgreSQL).
- **Alternative approaches (recommended order)**:
  1. **n8n-mcp multi-tenant headers** (Recommended): The n8n-mcp HTTP server supports `x-n8n-url` and `x-n8n-key` headers per-request. This means the API key can be passed dynamically from the Odoo side rather than being baked into the sidecar. The flow would be:
     - User creates API key in n8n UI (one-time setup)
     - User saves the API key in Odoo CloudService settings
     - Odoo passes the key to n8n-mcp via headers when making MCP calls
  2. **Init container + DB manipulation**: Use an init container or startup script to insert an API key directly into n8n's database before it starts. This is fragile and version-dependent.
  3. **Post-deploy automation**: Use a headless browser or HTTP automation to create the API key after n8n first boot, then store it in a Kubernetes Secret for the sidecar to read.

## 3. n8n-mcp Health Endpoint

- **Result**: Available
- **Endpoint**: `GET /health` (no authentication required)
- **Default port**: 3000 (configurable via `PORT` env var)
- **Details**:
  - The health endpoint returns a JSON response with comprehensive status information:
    ```json
    {
      "status": "ok",
      "mode": "sdk-pattern-transports",
      "version": "<version>",
      "environment": "production",
      "uptime": 12345,
      "sessions": {
        "active": 0,
        "total": 0,
        "expired": 0,
        "max": 100,
        "usage": "0/100"
      },
      "memory": {
        "used": 50,
        "total": 100,
        "unit": "MB"
      },
      "timestamp": "2026-02-27T15:00:00.000Z"
    }
    ```
  - The Docker image includes a built-in `HEALTHCHECK` that runs every 30 seconds:
    ```dockerfile
    HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
      CMD sh -c 'curl -f http://127.0.0.1:${PORT:-3000}/health || exit 1'
    ```
  - This health endpoint can be used directly for Kubernetes liveness/readiness probes on the sidecar container.

## 4. n8n-mcp Transport Modes

- **Supported transports**: Both **Streamable HTTP** (recommended) and **SSE** (legacy, supported for backward compatibility)
- **Default port**: `3000` (configurable via `PORT` env var)
- **Default MCP endpoint path**: `/mcp`
- **Details**:
  - **Streamable HTTP** (POST `/mcp`):
    - The primary and recommended transport mode
    - Uses standard HTTP POST with JSON-RPC 2.0 payloads
    - Each request can create a new session (stateless-friendly)
    - Authentication via `Authorization: Bearer <AUTH_TOKEN>` header
    - More efficient than SSE, simpler implementation
  - **SSE** (GET `/mcp` with `Accept: text/event-stream`):
    - Legacy transport, still supported for backward compatibility
    - Established via GET request with `Accept: text/event-stream` header
    - Long-lived connection with server-sent events
    - The `SSEServerTransport` from MCP SDK is used
    - Deprecated in favor of Streamable HTTP but still functional
  - **Multi-tenant support via headers**:
    - `x-n8n-url`: Override n8n instance URL per request
    - `x-n8n-key`: Override n8n API key per request
    - `x-instance-id`: Instance identifier for session management
    - `x-session-id`: Session identifier for state continuity
  - **Authentication**: `AUTH_TOKEN` or `AUTH_TOKEN_FILE` env var is **required** for HTTP mode. Used as Bearer token.
  - **Key environment variables for sidecar deployment**:
    | Variable | Value | Required |
    |----------|-------|----------|
    | `MCP_MODE` | `http` | Yes |
    | `AUTH_TOKEN` | `<generated-token>` | Yes |
    | `PORT` | `3000` (default) | No |
    | `N8N_API_URL` | `http://localhost:5678` | Yes (if same pod) |
    | `N8N_API_KEY` | `<user-provided>` | Yes (if not using multi-tenant headers) |

## Summary

- **All assumptions valid**: No - 1 of 4 assumptions needs adjustment
- **Blockers found**:
  1. **n8n Helm chart lacks sidecar support** - No `extraContainers` key. Requires PaaS Operator to patch the Deployment post-install.
  2. **n8n API key cannot be pre-provisioned** - No environment variable to set API key at deploy time. Requires user to create key in UI and save to Odoo, OR use multi-tenant headers to pass key per-request.
- **No hard blockers** - Both issues have viable workarounds.
- **Recommended approach**:
  1. Deploy n8n via Helm as usual
  2. PaaS Operator patches the Deployment to inject n8n-mcp sidecar container with `MCP_MODE=http` and shared `AUTH_TOKEN`
  3. User creates n8n API key through n8n UI after first login
  4. User saves the API key in Odoo CloudService configuration
  5. Odoo sends MCP requests to n8n-mcp sidecar using multi-tenant headers (`x-n8n-url`, `x-n8n-key`) to pass the n8n API key dynamically
  6. Health checks use `GET /health` on port 3000 for the sidecar readiness probe
  7. MCP communication uses Streamable HTTP (POST `/mcp`) with Bearer token auth

### Architecture Decision

```
┌──────────────────────────────────────────────────────┐
│  Kubernetes Pod (per-user namespace)                  │
│                                                       │
│  ┌─────────────┐     ┌──────────────────┐            │
│  │   n8n        │◄───│   n8n-mcp        │            │
│  │  (port 5678) │     │  (port 3000)     │            │
│  │              │     │  MCP_MODE=http   │            │
│  └─────────────┘     └──────────────────┘            │
│        ▲                      ▲                       │
│        │ localhost:5678       │ :3000/mcp             │
│        │ (n8n API)            │ (Streamable HTTP)     │
└────────┼──────────────────────┼───────────────────────┘
         │                      │
         │              ┌───────┴───────┐
         │              │  Odoo / LLM   │
         │              │  (MCP client) │
         └──────────────┴───────────────┘
              x-n8n-url + x-n8n-key
              passed per-request
```
