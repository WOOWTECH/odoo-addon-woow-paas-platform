"""#411 第二階段：dump 平台「可重算 values」（單一真相源 = CloudService._assemble_deploy_values）。

**在 stg odoo pod 內經 `odoo shell` 執行**（不是本機 python）。iterate 每個有 helm_release 的
cloud service，逐一呼叫 ``svc._assemble_deploy_values()['values']``（唯讀、不寫 DB、不呼叫
operator），輸出 JSON 供 audit_411_reset_values.py 的 --computed-json 比對。

⚠️ 前提：stg pod 的 odoo image 必須已含 #419（_assemble_deploy_values 才存在）。本檔是為
**W4 部署後**準備 —— 第二階段不真跑 stg。

用法（W4，deploy 含 #419 的 stg 後）：

    # 取 odoo pod（dev-sandbox 或 stg 系統 ns）
    POD=$(kubectl --context woow-k3s -n <odoo-ns> get pod -l <selector> -o name | head -1)

    # 經 odoo shell 餵本檔到 stdin，擷取 marker 之間的 JSON
    kubectl --context woow-k3s -n <odoo-ns> exec -i "$POD" -- \
        odoo shell -c /etc/odoo/odoo.conf --no-http < scripts/audit/dump_computed_values.py \
        | sed -n '/===AUDIT411_COMPUTED_DUMP_BEGIN===/,/===AUDIT411_COMPUTED_DUMP_END===/p' \
        | sed '1d;$d' > computed.json

    # 本地跑比對
    python3 scripts/audit/audit_411_reset_values.py --env stg --context woow-k3s \
        --computed-json computed.json --report docs/tracking/reports/411-reset-values-audit.md

輸出 JSON schema（list）：
    [{"service_id": int, "namespace": str, "release": str, "computed_values": {...}}, ...]
    組裝失敗的 service 改帶 "error": str（不含 computed_values），不中斷其他 service。

唯讀保證：_assemble_deploy_values() 純函數（見 #419 decision doc）；本檔不呼叫 write / commit /
operator。odoo shell 預設不 commit，即便誤觸也不落庫。
"""
import json

# `env` 由 odoo shell 注入為全域；本檔設計為餵進 odoo shell 的 stdin。
CloudService = env["woow_paas_platform.cloud_service"]  # noqa: F821

# 「有 helm_release」= helm_release_name 有值（!= False 對 Char 涵蓋空字串）。
services = CloudService.search([("helm_release_name", "!=", False)], order="id")

dump = []
for svc in services:
    entry = {
        "service_id": svc.id,
        "namespace": svc.helm_namespace,
        "release": svc.helm_release_name,
    }
    try:
        # 唯讀：取平台會送 operator 的全量非敏感 values（含 4 injector + odoo addon）。
        entry["computed_values"] = svc._assemble_deploy_values()["values"]
    except Exception as exc:  # 單一 service 組裝失敗不中斷全體盤點
        entry["error"] = f"{type(exc).__name__}: {exc}"
    dump.append(entry)

# marker 包裹，供 W4 的 sed 擷取（odoo shell 會混入啟動 log，marker 隔離乾淨 JSON）。
print("===AUDIT411_COMPUTED_DUMP_BEGIN===")
print(json.dumps(dump, ensure_ascii=False))
print("===AUDIT411_COMPUTED_DUMP_END===")
