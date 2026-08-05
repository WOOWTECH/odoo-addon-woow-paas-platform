# project mcp 3type onboarding 375

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_mcp_3type_onboarding_375`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："三型 MCP（k8s/vk/opendesign）上架 plan=issue #375；launch contract 實查包已交割；排期待定未動工"

2026-07-19：woow-mcp-server 已 merge 三個新 MCP admin app（k8s=PR#15 包 kubernetes-mcp-server Go binary 23 tools／vk=PR#16 supergateway 橋 vibe-kanban-mcp 34 tools／opendesign=PR#17 supergateway 橋 vendored od_mcp_server 15 tools；image 已推 Artifactory）。平台側三型全從零（無 template/plan/links、214 manifest 未列——leader2 三重驗證：cloud_app_templates.xml 9 template 無三型＋src grep 零命中＋manifest 零命中）。

**Plan＝tea issue #375**（Eugene 07-19 開立）：單 PR 批量三型；拍板＝沿用 MCP 齊一價 3000/月、24000/年（×8）、+1 AC 席變體 3600/28800；零改動區＝前端型錄（category='mcp' 自動帶出）/operator（generic Helm）/chart（共用 mcp-server 0.1.5）/#357 admin_password 機制（has_admin_gui=True 繼承）；無 migration（純新增 record 走 -u 自建）。**✅ 全案收官 2026-07-19＝PR #377 merged**（Eugene 授權後 tea merge、develop=57cda8d7、版號 .73、三型 template/plan/links 上架生效）；rollout（重釘 sha）另議。

**Landing 新知（PR #377）**：三 image digest 自解＝三型統一釘 woow-mcp-server main HEAD `723182e`（該 push 動共用 packages/mcp-admin-core→觸發全 app rebuild，三型皆具此 tag，比各自引入 commit 更正典、好對齊未來 rollout）；取 digest 用本機 jcr-prod 憑證（docker credential helper，config.json 無明文 auth）+ `docker manifest inspect -v` 取 Descriptor.digest（Artifactory anonymous v2 被擋 UNAUTHORIZED）。真 Odoo 驗證＝woow-k3s dev-sandbox（現成 stg image .68 `paas-odoo-stg:20260716.1208` + kubectl cp overlay 9 檔 + 最小 tests/__init__.py 只 import 三檔 + `-u --test-tags`）→ **16 tests 0 failed**（test_total_263_plans 實證 DB 真建 263 plan/214 變體、三型 launch contract 分別斷言全綠）；docker-desktop k8s 連不上＋docker daemon 未啟＋local 模式廢（hostPath 掛不到 /Users）→ cp overlay 是唯一免 CI build 途徑。Workflow 4 維對抗審查零問題（僅 1 minor generator docstring stale 已修）。計數鏈實作＝**MCP base plan 手寫不入 manifest、只 +1access 變體列進 generator**（manifest 214→220、變體 208→214、mcp family 6→12、總 plan 251→263＝base 43→49）。

**launch contract 實查輸入已交**（leader2 07-19 tea comment 至 #375）：三 app k8s-deploy.yaml 檔頭 seed 契約全文＋k8s port 缺口實查關閉——`k8s-mcp-http`（apps/k8s/k8s_mcp_admin/launcher.py）argparse --port default=8000、docstring 含權威 seed 塊，**三型 port 齊一 8000**。關鍵語意：k8s 治理三 toggle 走 mcp_server.env **全名不加前綴**（env_prefix="K8S_MCP_" 只作用 connection 鍵）；**k8s tools.disabled=真傳導（launcher 轉 TOML）vs opendesign=advisory-only——語意相反勿互抄**；k8s launcher 硬編 --bind-address 127.0.0.1＋--disable-multi-cluster（seed 不可覆寫）；vk/od=supergateway@3.4.3 wrap stdio；port≠args --port → proxy 一律 502。金樣板＝cloud_app_mcp_odoo_manage（cloud_app_templates.xml:316）。契約源＝main=723182e。

**Why:** 三型上架動工時所有前置輸入（契約/拍板/pattern）已集中在 issue #375＋本條，不需重查。
**How to apply:** 動工前先讀 issue #375 本文＋留言；template seed 照金樣板鏡射、config.mcp_server 照 #375 留言契約；k8s/opendesign 治理語意差異寫進 description 勿互抄。

**治理鎖端到端已驗證（2026-07-25，#375 comment 3100）**：docker-desktop 靶叢集＋**真 k8s-mcp-admin 映像**＋真 kubeconfig，三 arm 對照全 PASS。**兩種擋法機制不同、勿混談**：① `READ_ONLY=1`／`DISABLE_DESTRUCTIVE=1` → destructive 工具在 **tools/list 階段就未註冊**（呼叫回 `unknown tool`，只曝 15 支唯讀工具）＝最強形式，無法用構造請求繞過；② `ALLOW_SECRETS=0` → **執行期**擋（`denied_resources`）。另：只開 `DISABLE_DESTRUCTIVE` 即足以擋 delete；鎖關掉再鎖回去會重新生效。
**可複用驗證方法（測任何「阻擋機制」都該照做）**：(a) **事前先驗 SA 真有 delete 權**——否則「刪不掉」可能只是 RBAC 沒權限、治理鎖沒作用也會假 PASS；(b) **對照組把鎖關掉必須真的刪成功**（kubectl NotFound），證明測試本身有效；(c) 事前記下靶物件 **uid** 當錨點——收尾才能分辨「沒刪成功」vs「刪掉後重建」（uid 必變）。leader 以此 uid 獨立複核交付證據為真。

相關：[[reference_mcp_template_launch_contract]]、[[project_meeting_20260710_issue_landing]]、[[feedback_never_fabricate_tool_output]]
