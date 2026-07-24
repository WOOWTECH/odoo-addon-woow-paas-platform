# #411 Reset Values Backfill 盤點 ＋ W4 stg 完整驗證報告

> 生成：`scripts/audit/audit_411_reset_values.py`｜context=`woow-k3s`｜env=`stg`｜生成時間 `2026-07-24 23:47:15 +0800`
> helm：`v4.2.3+g43e8b7f` 讀取（release 由 operator helm 3.13.3 部署，`get values`/`list` 唯讀相容；已剝除 helm 4 `USER-SUPPLIED VALUES` sentinel）
> 狀態：**完成** —— 前置盤點（8 release 逐鍵三分類）＋ W4 stg 完整驗證（Phase 1-6：build／DB 升級／deploy／盤點／backfill／e2e 雙向）＋ (c) n8n canonical 查證 ＋ agent-runtime out-of-scope 定性。
> 鐵律：唯讀 `get`/`list`；raw values（含明文 secret）不進 git（落點見文末）；本報告 stored values 已遮罩（對齊 operator `_sanitize_release_values` ＋ 鍵名 heuristic）。

## 摘要

- 目標 ns：5｜release：9 收集成功、0 失敗

| env | namespace | release | chart | status | rev | stored 鍵數 | 分類 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| stg | `paas-ws-stg-5a105e8b` | `svc-16e7ac1a` | `n8n-2.0.1` | deployed | 2 | 6 | a:1 b:0 c:1 info:0 |
| stg | `paas-ws-stg-5a105e8b` | `svc-1b606411` | `mcp-server-0.1.5` | deployed | 1 | 10 | a:2 b:0 c:0 info:0 |
| stg | `paas-ws-stg-5a105e8b` | `svc-2d5a0ca0` | `mcp-server-0.1.5` | deployed | 1 | 10 | a:2 b:0 c:0 info:0 |
| stg | `paas-ws-stg-5a105e8b` | `svc-9d7dfc1d` | `mcp-server-0.1.5` | deployed | 1 | 10 | a:2 b:0 c:0 info:0 |
| stg | `paas-ws-stg-5a105e8b` | `svc-beab2427` | `open-design-0.1.4` | deployed | 2 | 10 | a:2 b:0 c:0 info:0 |
| stg | `paas-ws-stg-86f34bd4` | `svc-06b55af1` | `odoo-28.2.10` | deployed | 2 | 15 | a:1 b:0 c:0 info:0 |
| stg | `paas-ws-stg-86f34bd4` | `svc-5b78f97b` | `n8n-2.0.1` | deployed | 2 | 6 | a:1 b:0 c:1 info:0 |
| stg | `paas-ws-stg-9630836d` | `svc-a66927a6` | `home-assistant-0.2.1` | deployed | 4 | 7 | a:0 b:16 c:0 info:0 |
| stg | `paas-ws-stg-pa-oudse266vg2ztjpb` | `agent-pa-oudse266vg2ztjpb` | `agent-runtime-0.4.3` | deployed | 1 | 11 | pending |

## Per-release stored values（已遮罩）

### stg / `paas-ws-stg-5a105e8b` / `svc-16e7ac1a`（chart `n8n-2.0.1`）

```json
{
  "config": {
    "database": {
      "type": "sqlite"
    }
  },
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "main": {
    "config": {
      "GENERIC_TIMEZONE": "Asia/Taipei",
      "N8N_LOG_LEVEL": "info",
      "N8N_PUBLIC_API_ENABLED": "true"
    },
    "persistence": {
      "enabled": true,
      "size": "2Gi",
      "type": "dynamic"
    },
    "resources": {
      "limits": {
        "cpu": "1000m",
        "memory": "1Gi"
      },
      "requests": {
        "cpu": "100m",
        "memory": "256Mi"
      }
    },
    "secret": {
      "N8N_API_KEY": "***"
    }
  },
  "persistence": {
    "storageClass": "longhorn-delete"
  },
  "updateStrategy": {
    "type": "Recreate"
  }
}
```

### stg / `paas-ws-stg-5a105e8b` / `svc-1b606411`（chart `mcp-server-0.1.5`）

```json
{
  "app": "opendesign",
  "config": {
    "nonSensitive": {
      "mcp_server": {
        "args": [
          "--stdio",
          "python -m opendesign_mcp_server",
          "--outputTransport",
          "streamableHttp",
          "--streamableHttpPath",
          "/mcp",
          "--port",
          "8000",
          "--healthEndpoint",
          "/healthz"
        ],
        "command": "supergateway",
        "env": {},
        "env_prefix": "",
        "port": 8000
      }
    },
    "requireFields": true,
    "requiredFields": [
      "admin_password"
    ],
    "sensitive": {
      "admin_password": "***"
    }
  },
  "envPrefix": "",
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "image": {
    "repository": "jcr-prod.woowtech.io/woow-paas-docker-local/opendesign-mcp-admin",
    "tag": "723182e3ea1dab1162a5703ef6ac05d31229045c@sha256:22e9b758c1e6b46643b7ddb025cb46cb35ae99d17d0272f97f784c78a91c1895"
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "persistence": {
    "enabled": true,
    "size": "5Gi",
    "storageClass": "longhorn-delete"
  },
  "resources": {
    "limits": {
      "cpu": "1",
      "memory": "1Gi"
    },
    "requests": {
      "cpu": "100m",
      "memory": "128Mi"
    }
  },
  "service": {
    "port": 8080
  },
  "updateStrategy": {
    "type": "Recreate"
  }
}
```

### stg / `paas-ws-stg-5a105e8b` / `svc-2d5a0ca0`（chart `mcp-server-0.1.5`）

```json
{
  "app": "k8s",
  "config": {
    "nonSensitive": {
      "mcp_server": {
        "args": [
          "--port",
          "8000"
        ],
        "command": "k8s-mcp-http",
        "env": {
          "K8S_MCP_ALLOW_SECRETS": "***",
          "K8S_MCP_DISABLE_DESTRUCTIVE": "1",
          "K8S_MCP_READ_ONLY": "1",
          "K8S_MCP_TOOLSETS": "core,config,helm"
        },
        "env_prefix": "K8S_MCP_",
        "port": 8000
      }
    },
    "requireFields": true,
    "requiredFields": [
      "admin_password"
    ],
    "sensitive": {
      "admin_password": "***"
    }
  },
  "envPrefix": "",
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "image": {
    "repository": "jcr-prod.woowtech.io/woow-paas-docker-local/k8s-mcp-admin",
    "tag": "723182e3ea1dab1162a5703ef6ac05d31229045c@sha256:1a7b52c45f72456ce6fa1cabd71e9523d84c140b3f9d7ac3e3bd28526ea8fb17"
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "persistence": {
    "enabled": true,
    "size": "5Gi",
    "storageClass": "longhorn-delete"
  },
  "resources": {
    "limits": {
      "cpu": "1",
      "memory": "1Gi"
    },
    "requests": {
      "cpu": "100m",
      "memory": "128Mi"
    }
  },
  "service": {
    "port": 8080
  },
  "updateStrategy": {
    "type": "Recreate"
  }
}
```

### stg / `paas-ws-stg-5a105e8b` / `svc-9d7dfc1d`（chart `mcp-server-0.1.5`）

```json
{
  "app": "vk",
  "config": {
    "nonSensitive": {
      "mcp_server": {
        "args": [
          "--stdio",
          "vibe-kanban-mcp --mode global",
          "--port",
          "8000",
          "--outputTransport",
          "streamableHttp",
          "--healthEndpoint",
          "/healthz"
        ],
        "command": "supergateway",
        "env": {},
        "env_prefix": "",
        "port": 8000
      }
    },
    "requireFields": true,
    "requiredFields": [
      "admin_password"
    ],
    "sensitive": {
      "admin_password": "***"
    }
  },
  "envPrefix": "",
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "image": {
    "repository": "jcr-prod.woowtech.io/woow-paas-docker-local/vk-mcp-admin",
    "tag": "723182e3ea1dab1162a5703ef6ac05d31229045c@sha256:9284a5b239a6b8b6026b8592b2e26def688c1d256b64a52dbfc0cc51b1f60567"
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "persistence": {
    "enabled": true,
    "size": "5Gi",
    "storageClass": "longhorn-delete"
  },
  "resources": {
    "limits": {
      "cpu": "1",
      "memory": "1Gi"
    },
    "requests": {
      "cpu": "100m",
      "memory": "128Mi"
    }
  },
  "service": {
    "port": 8080
  },
  "updateStrategy": {
    "type": "Recreate"
  }
}
```

### stg / `paas-ws-stg-5a105e8b` / `svc-beab2427`（chart `open-design-0.1.4`）

```json
{
  "authProxy": {
    "basicAuth": {
      "enabled": true,
      "username": "admin"
    },
    "enabled": true
  },
  "config": {
    "allowedOrigins": "https://paas-cs-5a105e8b-453f335b.woowtech.io",
    "sensitive": {
      "admin_password": "***"
    }
  },
  "console": {
    "enabled": true,
    "image": {
      "repository": "jcr-prod.woowtech.io/woow-paas-docker-local/open-design-console",
      "tag": "20260720.0332@sha256:2b91c4ad4a6a94d1735c8aa7a6c18ec05491d9db31fa455f5e5da1cbb52b0f92"
    }
  },
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "image": {
    "repository": "jcr-prod.woowtech.io/woow-paas-docker-local/open-design",
    "tag": "20260720.2255@sha256:ccfa52c4ae87be10c09b0f27813c3cd086658697a285a41c63c7f71d9e7db87a"
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "networkPolicy": {
    "apiServerCidrs": [
      "10.43.0.1/32",
      "192.168.10.21/32",
      "192.168.10.22/32",
      "192.168.10.23/32"
    ]
  },
  "persistence": {
    "data": {
      "mountPath": "/app/.od",
      "size": "8Gi"
    },
    "home": {
      "mountPath": "/home/opendesign",
      "size": "2Gi"
    },
    "storageClass": "longhorn-delete"
  },
  "resources": {
    "limits": {
      "cpu": "4",
      "memory": "8Gi"
    },
    "requests": {
      "cpu": "1",
      "memory": "2Gi"
    }
  },
  "service": {
    "port": 7457
  }
}
```

### stg / `paas-ws-stg-86f34bd4` / `svc-06b55af1`（chart `odoo-28.2.10`）

```json
{
  "customPostInitScripts": {
    "fix-admin-credentials.sh": "***",
    "odoo-conf-system.sh": "#!/bin/bash\nset -eo pipefail\nCONF=/opt/bitnami/odoo/conf/odoo.conf\necho '[odoo-conf-system] Injecting system-level odoo.conf settings'\n[ ! -f \"$CONF\" ] && echo \"WARN: $CONF not found, skipping\" && exit 0\ngrep -q '^proxy_mode' \"$CONF\" || echo 'proxy_mode = True' >> \"$CONF\"\ngrep -q '^list_db' \"$CONF\" || echo 'list_db = False' >> \"$CONF\"\nsed -i '/^workers/d' \"$CONF\"\nsed -i '/limit_memory_hard/d' \"$CONF\"\nsed -i '/limit_memory_soft/d' \"$CONF\"\nsed -i '/limit_time_cpu/d' \"$CONF\"\nsed -i '/limit_time_real/d' \"$CONF\"\necho 'workers = 0' >> \"$CONF\"\necho 'limit_memory_hard = 0' >> \"$CONF\"\necho 'limit_memory_soft = 0' >> \"$CONF\"\necho 'limit_time_cpu = 60' >> \"$CONF\"\necho 'limit_time_real = 120' >> \"$CONF\"\necho '[odoo-conf-system] Settings injected'\n"
  },
  "global": {
    "imagePullSecrets": [
      "***"
    ],
    "security": {
      "allowInsecureImages": true
    }
  },
  "image": {
    "registry": "jcr-prod.woowtech.io",
    "repository": "woow-paas-docker-local/cloud-odoo",
    "tag": "20260407.1522"
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "loadDemoData": false,
  "odooEmail": "user@example.com",
  "odooPassword": "***",
  "odooSkipInstall": false,
  "persistence": {
    "enabled": true,
    "size": "10Gi",
    "storageClass": "longhorn-delete"
  },
  "postgresql": {
    "architecture": "standalone",
    "auth": {
      "database": "bitnami_odoo",
      "username": "bn_odoo"
    },
    "enabled": true,
    "image": {
      "registry": "docker.io",
      "repository": "bitnamilegacy/postgresql",
      "tag": "17.6.0-debian-12-r4"
    },
    "primary": {
      "persistence": {
        "storageClass": "longhorn-delete"
      }
    }
  },
  "resources": {
    "limits": {
      "cpu": "2000m",
      "memory": "2Gi"
    },
    "requests": {
      "cpu": "200m",
      "memory": "512Mi"
    }
  },
  "service": {
    "type": "ClusterIP"
  },
  "smtpPort": "587",
  "smtpProtocol": "starttls",
  "updateStrategy": {
    "type": "Recreate"
  }
}
```

### stg / `paas-ws-stg-86f34bd4` / `svc-5b78f97b`（chart `n8n-2.0.1`）

```json
{
  "config": {
    "database": {
      "type": "sqlite"
    }
  },
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "main": {
    "config": {
      "GENERIC_TIMEZONE": "Asia/Taipei",
      "N8N_LOG_LEVEL": "info",
      "N8N_PUBLIC_API_ENABLED": "true"
    },
    "persistence": {
      "enabled": true,
      "size": "2Gi",
      "type": "dynamic"
    },
    "resources": {
      "limits": {
        "cpu": "1000m",
        "memory": "1Gi"
      },
      "requests": {
        "cpu": "100m",
        "memory": "256Mi"
      }
    },
    "secret": {
      "N8N_API_KEY": "***"
    }
  },
  "persistence": {
    "storageClass": "longhorn-delete"
  },
  "updateStrategy": {
    "type": "Recreate"
  }
}
```

### stg / `paas-ws-stg-9630836d` / `svc-a66927a6`（chart `home-assistant-0.2.1`）

```json
{
  "global": {
    "imagePullSecrets": [
      "***"
    ]
  },
  "haProxy": {
    "seedConfiguration": true,
    "trustedProxies": [
      "10.42.0.0/16"
    ],
    "useXForwardedFor": true
  },
  "image": {
    "repository": "jcr-prod.woowtech.io/woow-paas-docker-local/woowtech-ha",
    "tag": "20260722.2329@sha256:73a9faf98d3037bf48560e670f811141bdc1125cb2ea63aa05e4efe21e7cb9c4"
  },
  "ingress": {
    "className": "",
    "enabled": false,
    "host": ""
  },
  "persistence": {
    "mountPath": "/config",
    "size": "5Gi",
    "storageClass": "longhorn-delete"
  },
  "resources": {
    "limits": {
      "cpu": "1",
      "memory": "1Gi"
    },
    "requests": {
      "cpu": "250m",
      "memory": "512Mi"
    }
  },
  "service": {
    "port": 8123,
    "targetPort": 8123
  }
}
```

### stg / `paas-ws-stg-pa-oudse266vg2ztjpb` / `agent-pa-oudse266vg2ztjpb`（chart `agent-runtime-0.4.3`）

```json
{
  "agentRuntime": {
    "externalEgress": {
      "enabled": true
    }
  },
  "auth": {
    "currentKey": "***"
  },
  "configMap": {
    "mcpSnapshot": "{\"servers\": {}}",
    "skillsSnapshot": "[]"
  },
  "env": {
    "ODOO_BASE_URL": "http://stg-odoo-dev-sandbox-odoo.dev-sandbox-stg.svc.cluster.local:8069",
    "PROJECT_HASH": "oudse266vg2ztjpb",
    "SKILL_BUNDLE_TOKEN": "***"
  },
  "image": {
    "tag": "1.4.2"
  },
  "imagePullSecrets": [
    {
      "name": "***"
    }
  ],
  "networkPolicy": {
    "egressWorkspaceIds": [
      1
    ],
    "odooNamespace": "dev-sandbox-stg"
  },
  "probe": {
    "style": "httpGet"
  },
  "projectHash": "oudse266vg2ztjpb",
  "projectId": 37,
  "skillRunner": {
    "enabled": true
  }
}
```

## 差異分類

- **(a) injector 類會自癒**：storageClass / imagePullSecrets / config.allowedOrigins（#399）/ networkPolicy.apiServerCidrs（#404）等平台管線每次重算的鍵，reset 後自動補回，無須動作。
- **(b) backfill**：stored 有、平台 computed 無、且非 injector —— reset 後會從 rendered manifest 消失，翻語意前須顯式補進 svc.helm_values。
- **(c) 逐筆裁決**：同鍵不同值、非 injector（例 image tag pin：stored 釘舊 tag、computed 是 template 新 tag，reset 後被覆蓋）。**prod 翻 flag 前 (c) 必須清零。**
- (info) computed 有、stored 無：reset 後自然補上（預期效果，非問題，不列明細）。

### (b) backfill 清單

| namespace | release | path | stored 值 |
| --- | --- | --- | --- |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["haProxy", "seedConfiguration"]` | `true` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["haProxy", "trustedProxies"]` | `["10.42.0.0/16"]` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["haProxy", "useXForwardedFor"]` | `true` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["image", "repository"]` | `"jcr-prod.woowtech.io/woow-paas-docker-local/woowtech-ha"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["image", "tag"]` | `"20260722.2329@sha256:73a9faf98d3037bf48560e670f811141bdc1125cb2ea63aa05e4efe21e7cb9c4"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["ingress", "className"]` | `""` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["ingress", "enabled"]` | `false` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["ingress", "host"]` | `""` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["persistence", "mountPath"]` | `"/config"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["persistence", "size"]` | `"5Gi"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["resources", "limits", "cpu"]` | `"1"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["resources", "limits", "memory"]` | `"1Gi"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["resources", "requests", "cpu"]` | `"250m"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["resources", "requests", "memory"]` | `"512Mi"` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["service", "port"]` | `8123` |
| `paas-ws-stg-9630836d` | `svc-a66927a6` | `["service", "targetPort"]` | `8123` |

### (c) 待裁決清單（prod 翻 flag 前須清零）

| namespace | release | path | stored | computed |
| --- | --- | --- | --- | --- |
| `paas-ws-stg-5a105e8b` | `svc-16e7ac1a` | `["main", "secret", "N8N_API_KEY"]` | `"***"` | `"***"` |
| `paas-ws-stg-86f34bd4` | `svc-5b78f97b` | `["main", "secret", "N8N_API_KEY"]` | `"***"` | `"***"` |

### Per-release 分類明細

#### stg / `paas-ws-stg-5a105e8b` / `svc-16e7ac1a` — a:1 b:0 c:1 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  },
  {
    "category": "c",
    "computed": "***",
    "path": [
      "main",
      "secret",
      "N8N_API_KEY"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-5a105e8b` / `svc-1b606411` — a:2 b:0 c:0 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "config",
      "sensitive",
      "admin_password"
    ],
    "stored": "***"
  },
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-5a105e8b` / `svc-2d5a0ca0` — a:2 b:0 c:0 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "config",
      "sensitive",
      "admin_password"
    ],
    "stored": "***"
  },
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-5a105e8b` / `svc-9d7dfc1d` — a:2 b:0 c:0 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "config",
      "sensitive",
      "admin_password"
    ],
    "stored": "***"
  },
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-5a105e8b` / `svc-beab2427` — a:2 b:0 c:0 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "config",
      "sensitive",
      "admin_password"
    ],
    "stored": "***"
  },
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-86f34bd4` / `svc-06b55af1` — a:1 b:0 c:0 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-86f34bd4` / `svc-5b78f97b` — a:1 b:0 c:1 info:0

```json
[
  {
    "category": "a",
    "computed": null,
    "path": [
      "imagePullSecrets"
    ],
    "stored": "***"
  },
  {
    "category": "c",
    "computed": "***",
    "path": [
      "main",
      "secret",
      "N8N_API_KEY"
    ],
    "stored": "***"
  }
]
```

#### stg / `paas-ws-stg-9630836d` / `svc-a66927a6` — a:0 b:16 c:0 info:0

```json
[
  {
    "category": "b",
    "computed": null,
    "path": [
      "haProxy",
      "seedConfiguration"
    ],
    "stored": true
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "haProxy",
      "trustedProxies"
    ],
    "stored": [
      "10.42.0.0/16"
    ]
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "haProxy",
      "useXForwardedFor"
    ],
    "stored": true
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "image",
      "repository"
    ],
    "stored": "jcr-prod.woowtech.io/woow-paas-docker-local/woowtech-ha"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "image",
      "tag"
    ],
    "stored": "20260722.2329@sha256:73a9faf98d3037bf48560e670f811141bdc1125cb2ea63aa05e4efe21e7cb9c4"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "ingress",
      "className"
    ],
    "stored": ""
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "ingress",
      "enabled"
    ],
    "stored": false
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "ingress",
      "host"
    ],
    "stored": ""
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "persistence",
      "mountPath"
    ],
    "stored": "/config"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "persistence",
      "size"
    ],
    "stored": "5Gi"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "resources",
      "limits",
      "cpu"
    ],
    "stored": "1"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "resources",
      "limits",
      "memory"
    ],
    "stored": "1Gi"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "resources",
      "requests",
      "cpu"
    ],
    "stored": "250m"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "resources",
      "requests",
      "memory"
    ],
    "stored": "512Mi"
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "service",
      "port"
    ],
    "stored": 8123
  },
  {
    "category": "b",
    "computed": null,
    "path": [
      "service",
      "targetPort"
    ],
    "stored": 8123
  }
]
```

## 鐵律與 raw dump 落點

- raw values（**未遮罩、含明文 secret**）：`/var/folders/7p/5slqshkx7gx8cp3s87j_kq9r0000gn/T/audit-411-raw-ou3wnb3k` —— **不進 git**。
- 本報告已遮罩；non-secret 基礎設施值（chart 版本、resources、CF tunnel host、#404 apiServerCidrs 內網 IP 等）保留，供 backfill 分析。
- 遮罩 heuristic 涵蓋 `pass`/`token`/`secret`/`credential`/`key` 命名；命名不含上列字樣的 secret（如 `dsn`/`connectionString`）不保證遮罩，故 raw dump 一律不進 git 為最終防線。

---

## agent-runtime release（`agent-pa-*`）out-of-scope 定性結論

**問題**：agent-runtime 的 release 生命週期（spawn / attach / reap cold-start）是否會經過本次 #411 改動的三個呼叫點（`action_redeploy` / `_reset_admin_password` / paas.py reconfigure），從而受 `reset_values` 語意翻轉影響？

**結論：不經過 → out-of-scope。** audit 中標 `pending`（computed dump 不涵蓋）為正確表現，非漏盤。

**Code 實證（origin/develop `39b2b3a4`）**：

1. **三個改動呼叫點只操作 `woow_paas_platform.cloud_service` 自身的 release**，皆帶 `reset_values=True`：
   - `action_redeploy` — `src/models/cloud_service.py:277`（`reset_values=True` @:324）
   - `_reset_admin_password` — `src/models/cloud_service.py:377`（`reset_values=True` @:471）
   - reconfigure endpoint — `src/controllers/paas.py:2544` 區段（`reset_values=True`，operate `env['woow_paas_platform.cloud_service']`）
   - 三者 reconfigure 區段（`paas.py:2480-2600`）對 `agent_session`/`agent-runtime` **零命中**。

2. **agent-runtime = 獨立 `woow_paas_platform.agent_session` model**（`src/models/agent_session.py`），自有 `helm_release_name`（:126）與 spawn/reap 生命週期（:116-120）。其 release 走**獨立 client** `src/services/agent_runtime_client.py`（`_PREFIX='/api/agent-runtimes'` :17）的 `spawn_or_attach`/`reap`/`resume`/`sync_workspaces` 等 method — **全數不傳 `reuse_values`/`reset_values`**（grep 零命中）；operator 端由 `extra/paas-operator/src/api/agent_runtimes.py` 的獨立 helm 邏輯（`_build_install_values` :174，additive router）處理。

3. **agent-runtime code path 對三呼叫點/`reset_values` 雙向零命中**：
   - `agent_session.py` / `agent_template.py` / `agent_runtime_skill.py` / `agent_runtime_webhook.py` / `agent_runtime_client.py` 對 `action_redeploy|_reset_admin_password|reconfigure|reuse_values|reset_values|upgrade_release` 全零命中。
   - 唯一交集為**讀取關係**（非 release 操作、不涉 reset_values）：`agent_session.py:673-761` 讀 `cloud_service.helm_values` 當 agent context；`cloud_service.py:1665-1955` 的 `_reconcile_agent_runtime_reach` 呼叫 `agent_runtime_client.sync_workspaces` 同步 reach。兩者皆非本次改動的三呼叫點。

**附註（prod gate 完整性）**：agent-runtime 的 **operator 端** helm 行為（`/api/agent-runtimes` 的 spawn install / sync upgrade 是否自帶 `reuse_values` 問題）屬**獨立議題**，不在本案（#411 Odoo 端改動，拍板「operator 不動」）scope。若要治理需另案評估 operator `agent_runtimes.py` 的 helm upgrade 語意。就 leader1 提問「是否經過三個 Odoo 呼叫點」，答案明確為**否**。

---

## W4 Phase 1–3 執行證據回顧（留檔）

| Phase | 項目 | 值 / 證據 |
| --- | --- | --- |
| 1 build | 新 image tag | `paas-odoo-stg:20260724.1526` |
| 1 build | CI run | Gitea run 1113 `completed/success`（觸發 15:26:35Z）；runner log `Job succeeded` |
| 1 build | registry digest | 新 `sha256:ea709d12…` ≠ 舊 `sha256:68eb3bd1…`（非快取假綠） |
| 1 build | image addon 驗證 | 臨時 pod：`_assemble_deploy_values`×4、`reset_values`×5、`全新開通`×3、manifest `18.0.1.0.78` |
| 2 DB 升級 | -u 是否執行 | **是** — job `stg-upgrade-78b`（新 image `odoo -u woow_paas_platform`）start `15:32:29Z` / complete `15:32:58Z` / succeeded=1 |
| 2 DB 升級 | -u 生效實證 | open-design template `full_description` 重載含新文案「全新開通」（noupdate=0 生效） |
| 3 deploy | 手法 | `kubectl set image`（Recreate roll，先刪後建） |
| 3 deploy | 完成時間 | 新 pod `stg-odoo-dev-sandbox-odoo-7d9c5d95b7-xrk6q` created `15:41:41Z`；deployment Available `15:42:52Z` |
| 3 deploy | pod / image | ready=true、image=`20260724.1526`、phase=Running |
| 全程 | DB 版號 | `ir_module_module.latest_version = 18.0.1.0.78`（Phase 2 後、Phase 3 後皆驗證一致） |
| 全程 | `/woow` | raw=303 → follow=200（未登入 303→login 標準行為，服務健康） |

---

## (c) n8n canonical key 查證（audit trail — prod gate 條件）

**裁決規則（Eugene）**：n8n 實際運行使用的 API key == computed → (c) 清零、reset 修正性；== stored → backfill；皆不符 → 停下回報。

**查證方法（唯讀）**：
- 進 n8n pod 的 `n8n` container：`svc-16e7ac1a-n8n-*`（ns `paas-ws-stg-5a105e8b`）、`svc-5b78f97b-n8n-*`（ns `paas-ws-stg-86f34bd4`）。
- 三方取值比對：
  - **A (helm stored)** — `helm get values <rel> -n <ns> -o json` 的 `main.secret.N8N_API_KEY`（reset 後 release 會被覆蓋掉的值）。
  - **ENV (n8n 進程實讀)** — pod 內 `printenv N8N_API_KEY`（n8n 實際持有；來源＝post-deploy 直接 patch 的 k8s secret `<rel>-n8n-app-secret`，非 helm）。
  - **C (computed)** — `_assemble_deploy_values()['values']` 的 `main.secret.N8N_API_KEY`（== `svc.helm_values`；reset 後 release 會採用的值）。
- **決定性實測** — pod 內 `node -e "fetch('http://localhost:5678/api/v1/workflows',{headers:{'X-N8N-API-KEY':process.env.N8N_API_KEY}})"` → 驗 running env key 是否為 n8n public API 認可的實際 key。

**判定依據（遮罩特徵：sha256 前 12 碼 ＋ len ＋ 格式；不洩漏原值）**：

| release | A (helm stored) | ENV (n8n 實讀) | C (computed) | n8n API | 判定 |
|---|---|---|---|---|---|
| svc-16e7ac1a | sha=`737cb39b4310` len=36（非 JWT，stale） | sha=`bc131c5c5b1c` len=229（JWT `ey…`） | sha=`bc131c5c5b1c` len=229 | HTTP **200** | **ENV==C**（sha 全等）→ 實際＝computed |
| svc-5b78f97b | sha=`6bcd2595a03d` len=36（非 JWT，stale） | sha=`7e7c1ef94f90` len=229（JWT `ey…`） | sha=`7e7c1ef94f90` len=229 | HTTP **200** | **ENV==C**（sha 全等）→ 實際＝computed |

**結論**：兩 release 的 n8n 進程實讀 key（ENV）與 computed 的 sha256 **完全相同**、且以該 key 通過 n8n public API 認證（**200**）；helm stored 是 len=36 的 stale 初始值（≠ 實際 key，吻合 `cloud_service.py:1289` 回寫 `svc.helm_values` 但從不 helm upgrade 的漂移機制）。→ **實際運行 key == computed → (c) 清零、reset 屬修正性、無需 backfill**。（插曲：首量 ENV len=100 為 tailscale 中途 stopped 的截斷壞資料，重啟後穩定量得 len=229 且 sha==C、API 200 三重印證。）

---

## Phase 5 backfill 執行記錄（HA svc-a66927a6，service_id=43）

odoo shell（`dev-sandbox-stg` odoo pod）`_deep_merge` 16 筆 stored 原值進 `svc.helm_values` → `env.cr.commit` → fresh browse 重讀逐筆驗證 **16/16 OK**。`before helm_values={}`（空）→ `after top keys [haProxy, image, ingress, persistence, resources, service]`。16 筆值 byte-match `helm get values`（見上方 (b) backfill 清單）。

---

## Phase 6 e2e 雙向驗證（拋棄式 mcp-k8s CS）

**載體**：拋棄式 mcp-k8s CS（id=57, release `svc-e35e3ee4`, ws 7 / ns `paas-ws-stg-86f34bd4`, plan 252 trial）。用 **mcp-server chart `0.1.1→0.1.5`** 版差（open-design 因 daemon 8Gi+authproxy 128Mi=8.125Gi > 8Gi 新 ns quota 排除）。暫改 mcp-k8s template version 製造版差（該 template 0 租戶、零風險，測後復原 0.1.5）。

### 正向 — chart 新增預設鍵經 reset 落地
| 項 | baseline (0.1.1) | after redeploy (0.1.5) | 判定 |
|---|---|---|---|
| `seedResources` | 缺席 | `{limits:{cpu:100m,mem:64Mi},requests:{cpu:10m,mem:16Mi}}` | ✅ 落地 |

release `svc-e35e3ee4` helm 升級到 `mcp-server-0.1.5 rev=2 deployed`；`seedResources`（chart 0.1.2 新增預設）經 `action_redeploy`(reset_values=True) coalesce 落地 → **#411 核心驗證成立**（既存 release 升級路徑吃到 chart 新增預設鍵）。

### 負向 B1 — user config / image pin / ingress / pull secret 原值保留
| 項 | baseline | after | 判定 |
|---|---|---|---|
| image.tag(pin) | `723182e3…@sha256:1a7b52c4…` | 同 | ✅ |
| image.repository | `k8s-mcp-admin` | 同 | ✅ |
| `global.imagePullSecrets` | `["paas-registry"]` | `["paas-registry"]` | ✅（真注入路徑，injector 自癒）|
| ingress | `{}` | `{}` | ✅ |
| config.jwtSecret/seedImage | 有/`""` | 同 | ✅ |
| envPrefix/app | `""`/`"k8s"` | 同 | ✅ |

（top-level `imagePullSecrets` `[{name}]→[]` 為 chart 0.1.1→0.1.5 預設演進，非平台值丟失；pod `ready=true restarts=0` 佐證 pull 正常。）

### 負向 B2 — 真實服務 backfill 後 reset 保留原值
重跑 audit：**HA `svc-a66927a6` (b) 從 16 → 0**（`a:0 b:0 c:0 info:0`）——backfill 生效，computed 現含 16 筆、reset 後與 stored 一致、零丟失。n8n (c) 維持 `c:1`（已裁決 computed=canonical、reset 修正性，無需動作）。

### 清理
拋棄式 CS 57 delete（helm uninstall `svc-e35e3ee4` ✅、subscription 46 `cancel` ✅、**不 delete ns**——共存真實 odoo/n8n）；mcp-k8s template 復原 `0.1.5` ✅；ns `paas-ws-stg-86f34bd4` Active 保留。

### 觀察（非 W4 操作導致）
Phase 4→B2 間隔數小時內，3 個 MCP 測試殘留服務（`svc-1b606411`/`svc-2d5a0ca0`/`svc-9d7dfc1d`，template 關聯異常的孤立 release）被外部清理（CS record + helm release 皆消失，audit 對象 8→5）。W4 操作範圍明確不含它們（僅碰 svc 43 backfill / mcp-k8s template version / 拋棄式 CS 57）；其 Phase 4 分類本為 `a:2/b:0/c:0`，消失不影響 (b)/(c) 結論。
