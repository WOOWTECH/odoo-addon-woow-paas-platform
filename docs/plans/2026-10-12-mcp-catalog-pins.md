# MCP catalog published image pins Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 新客戶開通八種monorepo MCP時使用真正main #50的固定SHA／digest。

**Architecture:** 只更新cloud_app_templates.xml八個root image.tag；digest來自main18319真registry push log。保留develop228其他變更、既有schema／price／quota／command／Secret seed，legacy獨立odoo-manage不屬該build來源。

**Tech Stack:** Odoo18／PostgreSQL17、k3s隔離dev sandbox、mcp-server chart0.1.7／Helm3.13.3。

---

### Task 1: 固定公開來源與scope

Files: `src/data/cloud_app_templates.xml`；`docs/verification/2026-10-12-mcp-published-pins.json`。
1. 讀每種main推送的immutable tag digest、health200及head1d16391d。
2. 修改8個tag，逐record與JSON深比對其他值相同；manifest228保持原base，不作feature bump。
3. 對chart0.1.7以實際default values render八種app，確認container image、launcher／envPrefix／requiredFields、PVC／resources守線。

### Task 2: 真Odoo seed測試

Files: 使用既有 `src/tests/test_mcp_server_template_seed.py` 的九種seed類及family coverage；不新增鏡射資料值的unit tests。
1. 本人dev sandbox、空白DB，DNS＋同namespace egress，無prod資料clone、provider或共享operator key。
2. source overlay當前candidate，以official image內相依addons執行安裝。
3. 跑既有family seed tests，驗實際ORM解析出的8pins等於已發布artifact；schema／resources／Secret carrier仍成立。
4. 證據保存後刪除本人namespace，驗absent；不清理其他人的dev sandbox。

### Task 3: 可審查PR

1. lint／collection baseline／manifest version gate／diff checks及真Odoo tests全部完成。
2. 以develop為base提交PR，保存精確head CI；由人類merge。
3. 正常release／image／備份／CD流程後才算live catalog更新；本次不直接修改prod template DB。
4. live catalog更新後真UI新開通驗actual digest、正常設定與SDK；未覆蓋項保持未測，商用0/19。
