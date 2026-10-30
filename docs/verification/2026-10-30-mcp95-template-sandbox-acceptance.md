# MCP95 八模板的真實 Odoo install／upgrade 驗收

2026-10-30，來源 `0ab778b7ec517b1f7b68fdc0b5e47a8563a5e269` 在新建、隔離的 K8s Odoo 沙盒完成兩路實測：upgrade 182 方法、fresh-install 後獨立 test process 182 方法，共364次執行；各路16 classes、0失敗、0錯誤、0 skip標記。實際方法集合與來源繼承方法及 C2 選中類別數量雙向一致。

[完整收據及原始證據 SHA256](2026-10-30-mcp95-template-sandbox-acceptance.json) 綁定實際 source map、imageIDs、namespace／node／PVC UIDs、測試 log、ORM讀回與清理。後續本分支只加證據文件，`src/` tree 保持與實測 commit相同。本結果是選定模板／credential carrier／launch／plan測試，不是完整平台 suite，也不是八個 MCP child runtime驗收。

## 實際結果

| 路徑 | 實際驗證 | 結果 |
| --- | --- | --- |
| 基底安裝 | `c42850a` 新合成 DB，ORM module installed與27筆模板讀回 | 通過；首次host結果收集失敗另留紀錄 |
| 候選升級 | `-u woow_paas_platform --test-enable`，182個實際方法 | 0失敗／0錯誤 |
| 新安裝 | 另一個新 DB先 `-i`，無test／HTTP／cron | process exit0，正常shutdown |
| 新安裝後測試 | 獨立 `-u --test-enable`，同182方法 | 0失敗／0錯誤 |
| ORM前後比較 | 全27筆 seed欄位、plan links、XMLID metadata | 只變八個 `image.tag` |
| fresh與upgraded比較 | 全27筆模板值及plan資料 | 一致 |
| 清理 | UID-precondition namespace刪除與控制器回收 | namespace、兩PVC/PV/LH volume及replica物件全消失；watchdog退出 |

八筆精確 pin仍是 PR95 source `3c93d0e727fdc948f1ea17cb5fa7d1ad1887a883` 的已發布 digest，未追隨 PR97重建。其餘19筆、legacy `odoo-manage`、所有非 image欄位、chart與治理設定完全保留；八筆 `noupdate=false`。沒有新增 EMQX MCP模板，也沒有schema／version變更。發布來源與 rollback前值見 [publication證據](2026-10-30-mcp95-template-pins.json)。

## 沙盒與來源優先序

Main於04:21授權的具體 lease綁定 `ubuntuserver-z0v3w` UID `61b090d8-2b7c-4e68-87f9-c6d3ccf8b6a7`。實際namespace為 `dev-sandbox-mcp95-20261030t043057z-af5c`，UID `b8515409-03d4-41d7-a935-8791e08d65d2`，04:31建立；75分鐘stop與15分鐘cleanup界線未延長。PG5Gi、source/filestore3Gi，使用實際2 replicas／Delete的 `longhorn-delete`；寫入前已核每個實際replica的磁碟身份與至少100Gi空間。

實際 runner採用已發現 sandbox CLI的 Odoo分離 install/test命令契約，render為兩個owned pods，未直接呼叫其預設 create／post-create hook或chart。這避免 hook探索operator／付款／AI設定及不必要cluster RBAC。default-deny只放同namespace PostgreSQL和DNS；無ingress、hostPath、service-account token、常駐cron、正式／staging資料還原或任何平台API寫入。

Odoo依賴 runtime使用已發布 official280 immutable digest `941b92091cbf851fdca22cd5d2e67f06235ccfdaad5b2af044682823fa5011a3`；PG用 `pgvector/pgvector@sha256:9a169499888c9d75ad14d0401be64839d2c325f32e399098261589faff93d65c`。兩者manifest/config與實際pod imageID均核對，沒有其他helper image。每路完整1292檔 hash吻合，實際 `get_module_path`分別解析到 `/work/base/woow_paas_platform` 或 `/work/candidate/woow_paas_platform`，沒有誤載image內舊addon。official280只是依賴runtime，不能稱為本候選的發布映像。

## 保留的失敗與後續命令

最初 `runner.py phase install base owned_upgrade` 的host結果收集失敗，原始固定reason為 `command_failed_inspect_before_retry`。原helper沒有保留失敗的確切transport步驟，因此不推斷是哪一個API timeout，也不宣稱缺失的Odoo process exitcode。保存當次完整安裝及正常shutdown log，另以新唯讀ORM確認已安裝的基底與27筆模板；沒有重跑基底安裝。`BASE-INSTALL-RECONCILIATION.json`保留這個證據界線。

原helper/freeze未改。另凍結 `guest_v2.py`／`durable.py`，使後續process先寫durable結果再回stdout，host只做有界唯讀輪詢；每phase保留exclusive intent，沒有自動重播。實際serial命令如下，均在owned沙盒內執行Odoo；這是紀錄，不是要求重跑：

```text
python3.12 durable.py readback base owned_upgrade
python3.12 durable.py test candidate owned_upgrade
python3.12 durable.py readback candidate owned_upgrade
python3.12 durable.py install candidate owned_fresh
python3.12 durable.py test candidate owned_fresh
python3.12 durable.py readback candidate owned_fresh
python3.12 collect_acceptance.py
python3.12 runner.py cleanup
```

先前 [sandbox plan](2026-10-30-mcp95-template-sandbox-plan.md) 與publication JSON的「尚未實測／尚未建資源」描述是04:12準備時點，本文件與收據記錄後續實際結果，未覆寫早期失敗或把準備證據重標成成功。

正式模板目前未變，須待 Main當前批次結束並經正常來源／release流程採用。完整平台CI、正式artifact、人類核可、CD與租戶rollout另行綁定；此收據不宣稱其完成。清理只證明控制器物件消失，不宣稱物理byte擦除。
