# #411 前置盤點：Reset Values Backfill 差異報告（第一階段）

> 生成：`scripts/audit/audit_411_reset_values.py`｜context=`woow-k3s`｜env=`stg`｜生成時間 `2026-07-24 18:59:19 +0800`
> helm：`v4.2.3+g43e8b7f` 讀取（release 由 operator helm 3.13.3 部署，`get values`/`list` 唯讀相容；已剝除 helm 4 `USER-SUPPLIED VALUES` sentinel）
> 狀態：**第一階段＝收集端＋骨架**。`recomputed` 側與 diff 分類待第二階段接 W2 `dev-411-pipeline` helper。
> 鐵律：唯讀 `get`/`list`；raw values（含明文 secret）不進 git（落點見文末）；本報告 stored values 已遮罩（對齊 operator `_sanitize_release_values` ＋ 鍵名 heuristic）。

## 摘要

- 目標 ns：4｜release：6 收集成功、0 失敗

| env | namespace | release | chart | status | rev | stored 鍵數 | 分類 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| stg | `paas-ws-stg-5a105e8b` | `svc-16e7ac1a` | `n8n-2.0.1` | deployed | 2 | 6 | pending-phase2 |
| stg | `paas-ws-stg-5a105e8b` | `svc-beab2427` | `open-design-0.1.4` | deployed | 2 | 10 | pending-phase2 |
| stg | `paas-ws-stg-86f34bd4` | `svc-06b55af1` | `odoo-28.2.10` | deployed | 2 | 15 | pending-phase2 |
| stg | `paas-ws-stg-86f34bd4` | `svc-5b78f97b` | `n8n-2.0.1` | deployed | 2 | 6 | pending-phase2 |
| stg | `paas-ws-stg-9630836d` | `svc-a66927a6` | `home-assistant-0.2.1` | deployed | 4 | 7 | pending-phase2 |
| stg | `paas-ws-stg-pa-oudse266vg2ztjpb` | `agent-pa-oudse266vg2ztjpb` | `agent-runtime-0.4.3` | deployed | 1 | 11 | pending-phase2 |

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

## 差異分類框架（diff 待第二階段填）

- **(a) injector 類會自癒**：install-time injector 寫入的鍵，翻語意後平台重新注入、缺了自癒。實例：`networkPolicy.apiServerCidrs`（#404）、`config.allowedOrigins`（#399）。
- **(b) 平台該管卻漏寫 → backfill**：平台會算出、stored 缺、非 injector 自癒。翻 reset 語意前須顯式 backfill 進 stored values。
- **(c) out-of-band 無法解釋 → 待 Eugene 裁決**：stored 有、平台算不出（人手改過或殘留）。

## 比對引擎（stub）

`compute_platform_values()` 為 stub，待接 W2 `dev-411-pipeline` 全量組裝 helper（經 odoo shell dry-run）。**禁止自寫 template defaults merge**——單一真相源＝該 helper，自寫會產生假 diff。第二階段接上後 `classify_diff()` 才輸出 (a)/(b)/(c)。

## 鐵律與 raw dump 落點

- raw values（**未遮罩、含明文 secret**）：`/private/tmp/claude-501/-Users-eugene-Documents-woow-AREA-odoo-woow-addons-woow-paas-platform/3d906c0d-bc2d-41a5-b737-851a6f91f7e0/scratchpad/audit-411-raw` —— **不進 git**。
- 本報告已遮罩；non-secret 基礎設施值（chart 版本、resources、CF tunnel host、#404 apiServerCidrs 內網 IP 等）保留，供 backfill 分析。
- 遮罩 heuristic 涵蓋 `pass`/`token`/`secret`/`credential`/`key` 命名；命名不含上列字樣的 secret（如 `dsn`/`connectionString`）不保證遮罩，故 raw dump 一律不進 git 為最終防線。
