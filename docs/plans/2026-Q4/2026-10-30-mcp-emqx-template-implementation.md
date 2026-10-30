# EMQX MCP 正式模板實作計畫

**Goal:** 以獨立來源 PR 新增一個可使用既有 marketplace／訂閱／管理密碼流程的 EMQX MCP 商品，固定已發布 PR97 exact image，待真實broker驗收與獨立review後才合併／曝光。

**Architecture:** 使用既有 `cloud_app_template`、`mcp-server` chart與月／年兩個 `sh.subscription.plan`，不加model、controller、schema或新平台啟動器。MCP child與39工具政策由已發布app維護；平台只seed非敏感固定啟動契約及正常admin password入口。首個候選 `is_hidden=True`、`is_active=True`，Mencius真broker證據未通過前不翻曝光旗標、不合併。

**Tech Stack:** Odoo18 XML seeds／TransactionCase、既有MCP chart0.1.9、Python3.12 local audits、需新lease的隔離K8s Odoo install／upgrade。

## 來源與邊界

- 新worktree `work/platform-emqx-template-20261030`、branch `feat/mcp-emqx-template-20261030`，base `85a57df898098344dc0c2b2004954d49714da91f`。PR2091 worktree／runtime／八pins不動。
- EMQX source `b5ed89ca01acfc33a7ef57987f0f64a11f603ea3`，image tag同source、digest `sha256:f60858334f11147675a0071b5caf699692ccf31fb493347da5d856bf9a8828dc`；config `sha256:b8a3f5adfc1cac6752ae28c86f5d312ea98db464b6020cd2d4ac5493ad76c2a8`。publication97的manifest/config bytes先獨立核hash，來源以exact commit讀取。
- source launcher為 `emqx-mcp-server --port 8000`，child固定127.0.0.1／Streamable HTTP；`EMQX_MCP_`前綴，connection白名單 `base_url/api_key/api_secret`。預設39工具全開，SQL為write/execute且可單獨撤銷；不在平台重做工具開關。
- 外部broker網址、API key／secret由租戶在服務Connection設定，平台不從既有EMQX服務複製帳密、不自動注入私網、不得覆寫部署SSRF或egress政策。
- release availability準備仍唯讀，不建立release tag／artifact dispatch／人類核可。既有八pin release不等待EMQX。

## 1. Seed與catalog

修改 `src/data/cloud_app_templates.xml`，在既有noupdate=0區塊新增唯一 `cloud_app_mcp_emqx`：

- name `MCP：EMQX`，slug `mcp-emqx`，category `mcp`，tags涵蓋EMQX／MQTT／broker管理。
- 正常中文catalog說明：七組39工具預設全開，含寫入／破壞性／SQL；Tools可逐工具或operation撤銷，SQL execute可單獨撤銷；backend API credentials不是Dashboard登入密碼；使用現有Tokens管理與 `/private_<token>/mcp`。
- chart `oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server`，0.1.9；1vCPU／1GiRAM／1GiPVC，Recreate，8080外部port；resources requests100m／192Mi，limits1／1Gi，與展示min欄位一致。
- `has_admin_gui=True`、`admin_password_carrier=config_json`、`mcp_enabled=False`（body內建）、`post_deploy_init_type=none`、`is_hidden=True`、`conceal_instances`保留預設False。
- config requireFields=True、requiredFields只有admin_password；sensitive空，nonSensitive只有下列mcp_server；不寫connection、token、tools／disabled、READONLY、SQL seed或extraEnv。

```json
{"command":"emqx-mcp-server","args":["--port","8000"],"port":8000,"env_prefix":"EMQX_MCP_"}
```

相同語意在helm頂層 `envPrefix=EMQX_MCP_`；`helm_value_specs={"required":[],"optional":[]}`。保留其餘27筆模板的完整XML／JSON語意與所有pins。

## 2. 正常方案與商品

修改 `src/data/subscription_products.xml` 新增EMQX專屬月／年plan＋product各一對；比照mcp-nextcloud：3000／24000、7天試用、無到期、service_type=cloud_service、無AC加購、無cs_*覆寫，product明寫taxes空以走既有平台稅收斂。不可共用broker或其他MCP的plan ID。

修改 `src/data/subscription_template_links.xml`，以完整 `(6,0,[monthly,yearly])`只連EMQX模板。plans/products沿用檔案noupdate=1，template／links為0；新XMLID可在-u建立，無需migration／version bump。現有plans/products/links不變。

## 3. 意義明確的ORM守線

新增 `src/tests/test_mcp_emqx_seed.py`：真seed record測試精確image/launch、雙env prefix、全開政策不被seed覆寫、無backend秘密／網址、admin-only requiredFields、平台generated admin password實際敏感值通路、無gateway token／跨服務credential供給、hidden catalog與broker模板分離、noupdate、兩plan價格／週期／trial／plan/product linkage／tax／無跨類別綁定、catalog說明SQL並非唯讀。mock只隔離operator網路，不取代ORM。

修改 `src/tests/test_mcp_server_template_seed.py` 只新增EMQX家族子類，繼承既有14個credential／resource／launch／plan不變式。新增test import並更新C2與seed-topology baseline；TestSeedProductCorpusIntact產品下界339→341。group-keys以links集合推導，無需改白名單；+access變體未新增，generator與歷史變體manifest不變。另以generator本地測試確認；不放寬家族或collection guards。PR2091已於05:05:43正常合併，本分支已快轉到其develop merge；僅向EXPECTED_IMAGE_TAGS map補EMQX一筆；八筆PR95及legacy pins逐字不動。

先做local AST/XML負對照（尚無EMQX record時應失敗），再加入seed後重跑；不能把local檢查稱為ORM成功。local命令：`python3.12 scripts/audit/repo-lint.py`、`python3.12 scripts/audit/test_collection_audit.py --check-baseline`、py_compile、docs-index --check、git diff --check。

## 4. 新lease後才做真Odoo驗證

先在獨立evidence root提交RESOURCE-REQUEST；舊8Gi fixture已清，不重用namespace/PVC/DB/intent。申請3CPU／6Gi RAM／PG5Gi+source3Gi（longhorn-delete2replicas/Delete）、local8Gi，75min+15mincleanup。所有images／source／render／runner先freeze，fresh nodeUID／capacity／replica實際磁碟檢查完成才create。

serial順序：基底新DB安裝→確認EMQX不存在且27筆canonical snapshot→候選-u＋EMQX/family/credential/plan/tax selected tests→讀28筆且舊27筆不變、精確新2plan+2product／links→另一新DB候選-i後獨立test process→fresh／upgrade資料比較→UID清理並確認namespace/PV/LH volume/replica消失。保存每個實際method集合／counts／failure與logsha；不以exit0或mock代替。

## 5. 交接／曝光條件

本地commit與來源PR可準備；先綁Mencius exact f608真broker驗收，含允許／拒絕零效果、SQL撤銷、restart/PVC設定保留及cleanup，不能引用HA或fakebackend充當。未通過前來源保持hidden且不merge；若通過後要正式曝光，另記證據hash並只翻EMQX is_hidden，重跑相關catalog guard。Main決定是否趕上.281，不能延誤既有八pin release。
