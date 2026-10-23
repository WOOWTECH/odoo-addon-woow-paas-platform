# Proxy conversion attempt correlation implementation plan

Base: #1904 f5c6dabc. Slice B only; no Odoo/client/schema/deployment changes.

1. Test signed ASGI: canonical UUID reaches begin/finish; only AP/CRON add accepts
   metadata; upstream receives exactly four legacy fields.
2. Test duplicate/new nonce/concurrent replicas, lost begin/outcome/response,
   disabled store and pre-begin failures: no second quota POST or false not_sent.
3. Add body metadata policy after HMAC/identity validation. Retain audit v1 and
   legacy requests; preserve SQL duplicate refusal and use fixed attempt_unknown.
4. Run targeted/full proxy suite, client contracts, lint, docs/diff checks.
   Real PG requires dedicated DB or exact-head CI; no shared scratch or cluster.
5. Record limits, commit/push and WIP stacked PR on #1904; no opt-in or deployment.

## 本地驗證與限制（2026-10-21）

- 先驗紅燈：原政策拒絕新body（403）；接受後duplicate/begin loss等14項失敗；
  再補shared replay失效的獨立紅燈，修正後新36項通過。
- 完整proxy suite：770 passed、1 skipped；skip是缺獨立PG DSN的integration模組。
  新增2個真PG HTTP contract cases，由現有postgres-integration CI job執行，
  本地沒有PG binaries／Docker，未碰shared scratch DB或任何叢集。
- unchanged client service/signing/route suite：90 passed。另用既有classify_response
  驗固定attempt_unknown回應確實為Unknown，不是NotExecuted，沒有改client。
- Python3.12 repo-lint --all、C2 --all（1266 classes）通過。
- 新test Ruff全通過；既有app/policy保留base已存在4項UP035/RUF012，
  integration模組保留base已存在I001/SIM117；排除這些既有規則後修改檔通過。
  不宣稱整個repo Ruff零警告。
- SQL/HMAC格式/audit v1未改；未開opt-in、未建image、未部署、未跑Odoo。
- 須等精確head CI與真PG cases過關才可解除WIP。UUID不是入帳／owner／epoch證明；
  ledger、client、來源binding與完整I3仍待獨立切片。
