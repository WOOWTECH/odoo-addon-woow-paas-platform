---
status: archived
archived-from: docs/plans/journal/2026-05-06-pkce-oauth-migration.md
archived: 2026-07-19
archived-reason: "PKCE 已上線；現況=src/controllers/oauth2.py 與 guides/development/oauth2-architecture.md"
---

# PKCE-only OAuth2 migration — 開發過程紀錄

> 對應 plan：`docs/plans/2026-05-06-pkce-oauth-migration-design.md`
> 雙端對齊 memory：Maestri note `sm-api-doc`（§1-§9 對應本紀錄各 phase）
> 期間：2026-05-06 ～ 2026-05-08

---

## Phase 1：跨 agent 設計對齊（2026-05-06）

### 起點

User 反映 HA 整合「先建 client 再貼 secret」UX 太差，希望改成 OAuth public client + PKCE，讓使用者點「Add Integration」就直接走授權流程。同時問了一個架構問題：「不同 HA server 共用同一組 OAuth server 可行嗎？」

paas-platform agent 給三個選項分析：A. PAT、B. Public client + PKCE、C. Dynamic Client Registration。**推薦 B**，給出 trade-off：HA framework 已原生支援 PKCE（`LocalOAuth2ImplementationWithPkce`），server 端只需擴充既有 OAuth2 provider。

「多 HA server 共用 OAuth server」的疑慮（單點故障、負載集中、無法分辨來源）逐項回應 — **是 by-design**：Google Home 全球幾百萬戶也是同一個 client_id，client_id 識別的是 app 不是 instance。

### sm-api-doc 跨 agent 紀錄

ha-smart-home agent 寫 §1-§3：API spec、目前 OAuth2 setup、PKCE 提案。paas-platform agent 接續寫 §4：實作可行性分析（讀過 6 個 source file 引用具體 line number）+ 7.5h estimate + 對 HA 端的 3 個確認問題。

ha-smart-home agent 用 §5 回答 3 題，全 ✅，附 HA Core source code 行號。

對齊完成 → 雙方都 commit 動工。

---

## Phase 2：paas-platform 實作（PR #21）

按 §4.8 PR 清單做 6 個檔案改動：

| 改動 | 檔案 |
|---|---|
| 新欄位 + `@api.constrains` | `src/models/oauth_client.py` |
| `_authenticate_client` 分流 + PKCE 三層強制 | `src/controllers/oauth2.py` |
| UI 顯示新欄位、隱藏 secret 按鈕 | `src/views/oauth_client_views.xml` |
| Seed `woow-ha-smart-home` public client | `src/data/oauth_clients.xml`（新檔） |
| 加 data 檔 | `src/__manifest__.py` |
| Constraint + flow 測試 | `src/tests/test_oauth2.py` |

實作中遇到的小決定：

- Tests 採 `TransactionCase` + 直接呼叫 controller（同 `test_ha_api.py` pattern），避免 `HttpCase` 的跨 cursor 可見性問題
- Odoo 18 用 `invisible="client_type == 'public'"`（Python 表達式）取代舊的 `attrs={...}` 寫法

Commit `a9a557c` `feat(oauth2): add public client + PKCE-only flow` push 到 `featrue/smart-home-odoo-login`，用 `tea pulls create --base develop` 開 PR #21。

---

## Phase 3：PR review（agent panel）+ 補測試 / logging

跑 `/pr-review-toolkit:review-pr` 平行 4 個 specialist agents（code-reviewer / pr-test-analyzer / silent-failure-hunter / comment-analyzer）。

### Critical 發現（merge 前修）

| 來源 | 內容 |
|---|---|
| pr-test-analyzer | `authorize_get` / `authorize_post` 的 PKCE enforcement **零測試**，只測了 token endpoint — PR 一半 code path 沒覆蓋 |
| pr-test-analyzer | redirect_uri mismatch、unknown client_id、inactive client 都沒 negative case |
| silent-failure-hunter | `_authenticate_client` 三種失敗（unknown / bad secret / public+secret）全返回 `None`，只 public+secret 有 WARN log，其他靜默 |
| silent-failure-hunter | `authorize_post` 的 PKCE 拒絕完全沒 server log，downgrade 攻擊不會出現在 telemetry |

修法（commit `698c9ed` `test(oauth2): cover authorize endpoint PKCE + add failure-path logging`）：

- `oauth2.py` 8 處新增 `_logger.info` / `_logger.warning`
- 加 `TestAuthorizeEndpointPkce`（3 case）：mock `request.render` / `request.redirect` 抓 template / URL params
- `TestPkceOnlyTokenFlow` 加 3 case：redirect mismatch、unknown client_id、inactive client

### Polish（comment-analyzer 提）

commit `de5b65b` `chore(oauth2): apply PR #21 review polish`：

- `_authenticate_client` 11 行 docstring → 3 行
- `set_secret` / `action_regenerate_secret` 的 `Args:` / `Returns:` / `Raises:` blocks 全砍，留一行 WHY
- `TestPkceOnlyTokenFlow` docstring 不再 reference `test_ha_api.py`（違反「不要 reference callers」規則），改寫實際原因（cursor 一致性）
- `client_id` field `help=` 加 HA integration readonly contract 說明（`woow-ha-smart-home` pinned in const.py）

review 中其他 Suggestion 級別的 follow-up（`confidential + require_pkce` 4 種布林組合補測、auth code replay test、`import base64` 搬 top-level）暫列 follow-up，不 block merge。

---

## Phase 4：PR #21 merged → develop → stg deploy

`tea pulls merge 21 --style merge` → develop merge commit `8fa483c`。

### Stg deploy 第一輪卡住

`./.claude/skills/deploy-stg/scripts/deploy-stg.sh` background 跑：

- ✅ Git tag `stg-20260506.1608` 建在 paas-odoo-ci，CI build 90 秒成功（`paas-odoo-stg:20260506.1608`）
- ❌ Sandbox 已存在的「`Upgrade instead? (yes/no):`」prompt 卡住 → exit 1

### Workaround：`yes |` + `--image`

CI 已 build 完成的 image 不重複 build：

```bash
yes | ./.claude/skills/deploy-stg/scripts/deploy-stg.sh \
  --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:20260506.1608
```

helm upgrade 成功（revision 18），`init_odoo_system_params` 設 `web.base.url=https://stg.woowtech.io` + Odoo rollout restart。

### DB verify

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name='woow_paas_platform_oauth_client'
  AND column_name IN ('client_type','require_pkce');
-- 兩個欄位都存在 ✅

SELECT * FROM woow_paas_platform_oauth_client
WHERE client_id='woow-ha-smart-home';
-- 1 row：public + require_pkce=true + 正確 redirect_uri ✅
```

Stg 後端 ready。寫 `sm-api-doc §6` `STG ready` 通知 HA agent 開跑 E2E。

---

## Phase 5：stg.woowtech.io 502 — 一連串 sandbox debug

HA agent 還沒收到通知前，user 自己打 stg.woowtech.io 看到 **Cloudflare 502 Bad Gateway**。

### 反向 narrow down

| 測試 | 結果 | 推論 |
|---|---|---|
| `kubectl get pods -n dev-sandbox-stg` | 全 Running | Pod 自身 OK |
| Odoo log | 只跑 cron，沒 error | Odoo 服務正常 |
| Nginx error log | `connect() failed (111: Connection refused)` to `odoo:8072` | nginx → odoo:8072（websocket） refused |
| `curl http://odoo:8069/...` from nginx pod | HTTP 200 | 8069 通；8072 沒 listen 是次要問題 |
| `curl https://stg.woowtech.io` × 1 | 502 | 外部還是進不來 |
| `kubectl logs cloudflared --tail=50` | 最後 log 是 30+ 小時前 | **Zombie tunnel** |

### Root cause #1：cloudflared zombie tunnel

cloudflared pod alive、tunnel 之前 register 過，但 30 小時無新事件 — Cloudflare 邊收到 request 試 forward 到 tunnel，tunnel 端沒收到。Known cloudflared QUIC keepalive bug。

修法：`kubectl rollout restart deployment -n paas-stg-cloudflare-system cloudflare-tunnel-stg-cloudflare-tunnel-remote`。

cloudflared 重啟，4 個 connection 全 Registered。但 stg.woowtech.io 仍 502。

### Root cause #2：NetworkPolicy block cross-NS

從不同 namespace 跑 curl-test pod 試 nginx：

| Source ns | Result |
|---|---|
| `default` → `paas-system/paas-operator` | HTTP 200（control） |
| `default` → `dev-sandbox-stg/nginx` | refused 4ms |
| `paas-stg-cloudflare-system` → `dev-sandbox-stg/nginx` | refused 4-90ms |
| `dev-sandbox-stg` 內部互連 | HTTP 200 |

`stg-odoo-dev-sandbox-allow-internal` NetworkPolicy 寫了 ingress 允許 `paas-stg-cloudflare-system` namespace（namespaceSelector match `kubernetes.io/metadata.name=paas-stg-cloudflare-system`）— label 確實存在，但實際被擋。K3s flannel 號稱不 enforce NP，但這個 cluster 顯然有 enforce。

暫時 workaround：`kubectl delete networkpolicy stg-odoo-dev-sandbox-{default-deny,allow-internal}` + 再 restart cloudflared 一次（清 stale state）→ stg.woowtech.io 5 次連續 200。

### 把 paas-charts agent 拉進來

NP 是 `odoo-dev-sandbox` helm chart 自帶的。透過 maestri 跟 paas-charts agent 確認 chart 責任，它先後給兩個 hypothesis：

1. cloudflared 是 `hostNetwork=true` → source IP 是 node IP，NP 看不到 pod label。**驗證後不成立**：`hostNetwork: (空)`、pod IP `10.42.1.54` 在 cluster CIDR
2. **Egress 的 `ipBlock except 10.0.0.0/8` 把 cluster pod CIDR 也擋了**

第 2 個是真 root cause（見 Phase 8 verification）。

---

## Phase 6：HA E2E 第一段 + 缺 home seed

通知 HA agent 開跑 E2E（含 NP 暫刪的 caveat）。它跑 OAuth + 4 支 API 全綠：

- ✅ `/oauth2/authorize` 帶 PKCE S256 challenge
- ✅ `/oauth2/token` 不帶 client_secret + 帶 code_verifier，`Successfully authenticated`
- ✅ Token response 5 個 key 齊（`access_token`、`refresh_token`、`scope`、`expires_in=3600`、`token_type`）
- ✅ `GET /api/smarthome/workspaces` HTTP 200，2 workspaces
- ✅ `GET /api/smarthome/workspaces/{27,1}/homes` HTTP 200，但**0 homes**

卡在 stg DB 兩個 workspace 都沒 home → 不能測 tunnel-token / coordinator polling。HA agent 請 paas-platform 端 seed 一個 home。

### Home seed via SQL

直接讀 `src/models/smart_home.py` 確認最少必填欄位（NOT NULL columns）：`name`, `reference_id`, `workspace_id`, `state`。

```sql
INSERT INTO woow_paas_platform_smart_home (
  name, reference_id, workspace_id, state, tunnel_status, subdomain, ha_port,
  tunnel_id, tunnel_token, tunnel_name, tunnel_route,
  create_uid, create_date, write_uid, write_date
) VALUES (
  'E2E Test Home', gen_random_uuid()::text, 1, 'active', 'connected',
  'paas-sm-e2e-test-feedeed1', 8123,
  'tun-e2e-test-12345',
  'mock-tunnel-token-for-e2e-test-do-not-use-in-prod',
  'paas-sm-e2e-test-tunnel',
  'paas-sm-e2e-test-feedeed1.woowtech.io',
  1, NOW(), 1, NOW()
) RETURNING id, name, workspace_id, state, subdomain, tunnel_status;
```

`tunnel_token` 是 mock value — HA 拿到後嘗試實際連 Cloudflare tunnel 會失敗，但 schema 完整 + endpoint 200 對 OAuth + API 驗證足夠。真 tunnel 連線是另一段 work。

寫進 `sm-api-doc §8` 通知 HA agent。

---

## Phase 7：HA E2E 全綠 + PR #3（2026-05-07）

HA agent 重試 add integration，10 個階段全跑完：

| # | 階段 | 結果 |
|---|---|---|
| 1-3 | OAuth flow（authorize / token / refresh） | ✅ |
| 4-7 | 4 支 API（workspaces / homes / detail / tunnel-token） | ✅ HTTP 200 |
| 8 | `async_setup_entry` refresh tunnel token | ✅ |
| 9 | Coordinator 30s polling × 3 連續 | ✅ |
| 10 | cloudflared with mock token | ⚠️ 預期 fail（`Provided Tunnel token is not valid`） |

PKCE 行為跟 `sm-api-doc §3.4` spec 100% 對齊。

HA 端 [PR #3](https://git-prod.woowtech.io/ha-components/woow_paas_smart_home/pulls/3) 5 commits 全過 ruff + hassfest，等 paas-platform prod 部署後 merge。

> Note：HA 端 repo 在 2026-05-09 從 `gitlab.com/woow3/woow-paas-smart-home` 遷移到內部 `git-prod.woowtech.io/ha-components/woow_paas_smart_home`。舊 GitLab MR !1 closed、Gitea PR #2 也 closed（重建為繁中 description），同樣 5 commits 一直在 `feat/pkce-oauth-migration` branch，最終版是 PR #3。本文一律使用 PR #3 引用。

寫進 `sm-api-doc §9`。

---

## Phase 8：NetworkPolicy chart fix（PR #22，2026-05-08）

paas-charts 的 hypothesis #2「`ipBlock except 10.0.0.0/8` 把 cluster CIDR 也擋了」需要驗證。

### Verify with minimal NP

paas-charts 提了一個 minimal NP（明確列 `paas-stg-cloudflare-system` 在 ingress + egress 兩邊）。我加上 same-NS egress allow 避免 nginx ↔ odoo 中斷，apply：

```yaml
ingress:
- from:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: paas-stg-cloudflare-system
- from:
  - podSelector: {}
egress:
- to:
  - podSelector: {}
- to:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: paas-stg-cloudflare-system
- to:
  - namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: kube-system
  ports:
  - { protocol: UDP, port: 53 }
  - { protocol: TCP, port: 53 }
```

結果：

| 測試 | Result |
|---|---|
| External `curl https://stg.woowtech.io/web/login` × 3 | ✅ HTTP 200 |
| Test pod from cloudflared ns（**等 5 秒**讓 iptables 同步後 curl） | ✅ HTTP 200 |
| 直接 curl 沒 sleep | refused — 是 timing 問題（new pod NP iptables 還沒同步），不是真不通 |

**Hypothesis #2 確認**。Reply traffic 的 root cause：K8s NP 理論上 stateful（reply 不需要 egress rule），但 K3s 內建 NP enforcer 對某些 reply 路徑處理為 fresh egress，碰到 `ipBlock except 10.0.0.0/8` 把 cluster pod CIDR（`10.42.0.0/16` ⊂ `10.0.0.0/8`）擋掉。

### Chart fix：mirror ingress allowlist 到 egress

`charts/odoo-dev-sandbox/templates/network-policy.yaml` 加 egress mirror block：

```yaml
egress:
  ...existing rules...
  # Mirror ingress allowlist for reply traffic. K8s NetworkPolicy is
  # conceptually stateful, but K3s' embedded enforcer treats some reply
  # paths as fresh egress and drops them once the ipBlock-except rule above
  # excludes the cluster pod CIDR (10.42.0.0/16 ⊂ 10.0.0.0/8). Re-listing
  # the inbound namespaces on the egress side closes that gap.
  {{- range .Values.networkPolicy.allowIngressFromNamespaces }}
  - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: {{ . }}
  {{- end }}
```

### Two-phase verification

`helm upgrade stg charts/odoo-dev-sandbox --reuse-values --namespace dev-sandbox-stg`（revision 19）：

| 階段 | NPs in dev-sandbox-stg | External curl |
|---|---|---|
| 1. `minimal-test` + 兩個 chart NP 並存 | 3 個 | ✅ 200 × 3 |
| 2. 刪 `minimal-test`，只留 chart 兩個 | 2 個 | ✅ 200 × 3 |

Chart fix 獨立有效。

開 PR #22 `fix(chart): mirror ingress allowlist on egress for cross-NS reply traffic`（commit `0871a97`）+ docs commit（`bf594c9`）回應 paas-charts 提的 follow-up：在 `values.yaml` 對 `allowIngressFromNamespaces` 加 inline comment 說明會自動 mirror egress，避免未來有人想分開控制 in/out 卻踩坑。

`tea pulls merge 22` → develop merge commit `71357dd`。

---

## Phase 9：交付狀態

| 項目 | 狀態 |
|---|---|
| paas-platform PR #21（PKCE + tests + polish + logging） | ✅ Merged → develop |
| Stg deploy `paas-odoo-stg:20260506.1608` + DB schema migrate | ✅ |
| `woow-ha-smart-home` public client seed | ✅ DB verified |
| `E2E Test Home`（id=1）seed | ✅ DB verified |
| HA [PR #3](https://git-prod.woowtech.io/ha-components/woow_paas_smart_home/pulls/3)（5 commits，ruff + hassfest 全綠） | ✅ Ready，等 prod merge |
| HA E2E 10/10 階段（含 1 個 mock-token 預期 fail） | ✅ |
| paas-platform PR #22（chart NP fix + docs） | ✅ Merged → develop |
| paas-charts close-loop confirm | ✅ |

剩下 user-driven steps：

1. paas-platform develop → main → prod release（v* tag）
2. HA 端 PR #3 merge（同步 prod release，避免 prod 抓不到 public client）
3. paas-operator 排期 provision 真 Cloudflare tunnel 給某個 stg home，做最後一段 tunnel-connect 驗證

---

## 教訓 / 觀察

- **K3s 內建 NetworkPolicy enforcer（kube-router 變體）對 reply path 不完全 stateful**，碰到 `ipBlock except 10.0.0.0/8` 排除 cluster pod CIDR 時，cross-NS 的 reply egress 會被當成 fresh egress 擋掉。Fix 是 mirror inbound allowlist 到 outbound — 不要假設 NP enforcer 完美處理 stateful。
- **Cloudflared remote-managed tunnel 偶發 zombie**：Pod alive、connection 之前 register 過，但長時間（10+ 小時）無事件 log 是危險訊號 — `kubectl rollout restart` 是已知 workaround。
- **K8s NP iptables 同步有 timing**：剛建好的 pod 立刻 curl 可能 refused，需要 sleep 幾秒。Debug 時要排除這個假陽性。
- **跨 agent collaboration via shared note**：Maestri note `sm-api-doc` 是這次最大的勝點 — 兩端 agent 把 spec 對齊、Q&A、進度更新都寫進同一份文件，形成可追溯的對齊紀錄。HA 端 §3 + paas-platform 端 §4 + 互相 §5 確認，merge to prod 之前就把所有 ambiguity 化解。
- **Section number 衝突**：兩端 agent 並行寫 note 時撞 section number（兩個 §6）— 應對方式是 paas-platform 端 renumber 自己的成 §7 + 修 HA 端 §6.2 表格的 todo 標 ✅ DONE。下次跨 agent shared note，可以一開始就 reserve section ranges。
- **Test pattern 一致**：採 `TransactionCase` + 直接呼叫 controller + mock `request` 的 pattern（同 `test_ha_api.py`），避開 `HttpCase` 跨 cursor 可見性問題，所有 OAuth flow tests 用同一套 helper（`_call_token` / `_call_authorize_get` / `_call_authorize_post`）。
