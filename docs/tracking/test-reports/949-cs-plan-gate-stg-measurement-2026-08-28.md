# #949 CS 方案白名單閘 fail-open — stg 唯讀量測與可達性判定

> issue **#949**（A1-security）｜受測修復：本 PR（`fix/949-cs-plan-gate`，base `origin/develop` `fc3661e5`）
> 環境：`woow-k3s`／`dev-sandbox-stg`（DB `woow_stg`）｜**全程 SELECT，零寫入**
> 執行：2026-08-28（leader1）
> prod 側同題量測由 issue-leader 於 Eugene 授權下執行，逐字結果見 **#949 留言 10244**（影響面 0），本檔不重述。
>
> **一句話結論**：缺陷成立（兩條閘的 truthiness 前綴讓零方案 template 整段被跳過），
> 但**開單時宣稱的「今天就可達」不成立** —— stg 上唯一「上架中且零方案」的 template
> 是 HA PaaS，它 `conceal_instances=True`，`api_service_change_plan` 更早的 gate
> （`paas.py:1137`）就先擋掉了。本 PR 修的是「資料一變就會敞開」的那條線。

## 為什麼需要這份檔

決定「收緊會不會打壞合法流程」靠的是資料分布，而分布會過期。把 **SQL 原文與 psql 逐字輸出**
留在 repo 內，往後任何人都能用同一把尺重量一次，而不是相信這份報告的結論。
（原始檔曾放 `.scratch/`，該目錄會被清 ⇒ 引用它等於留死路徑，故落進 repo。）

## 0. 可達性判定（控制流，不是屬性篩選）

`api_service_change_plan` 的關卡順序：

| # | 行 | 關卡 |
| --- | --- | --- |
| 1 | `paas.py:1137` | `if not service.exists() or service.template_id.conceal_instances:` → `Service not found` |
| 2 | — | workspace role（owner/admin） |
| 3 | — | `plan.exists()` |
| 4 | **`paas.py:1157`** | **本單的閘** |
| 5 | — | `old_sub.state != 'in_progress'` → `No active subscription` |

⇒ 掛在 HA PaaS（`conceal_instances=t`）上的服務**停在第 1 關**，到不了第 4 關。
repo 內既有測試 `src/tests/test_conceal_instances_split_471.py::TestConcealTemplateOperationsBlocked::test_service_change_plan_blocked`
逐字守著這個行為，可當第三方佐證。

⚠ 判可達性**不能只看 `is_active`** —— 那是屬性篩選；可達性要走控制流。
（另：Odoo 讀 M2M 會濾掉封存記錄 ⇒「方案全被封存」的 template 在閘看來與零方案相同，
prod 側已一併驗過，見留言 10244。）

## 1. SQL 原文

以下四段依序執行（`kubectl exec -i <pg-pod> -- psql -U odoo -d woow_stg -f -` 走 stdin）。

### `949-q1.sql`

```sql
\echo '=== [A] 陽性對照：template 全表筆數 ==='
select count(*) as template_total from woow_paas_platform_cloud_app_template;

\echo '=== [B] 每 template 掛的 plan 數分布（含零） ==='
select n_plans, count(*) as n_templates from (
  select t.id, count(r.plan_id) as n_plans
  from woow_paas_platform_cloud_app_template t
  left join woow_template_plan_rel r on r.template_id = t.id
  group by t.id
) x group by n_plans order by n_plans;

\echo '=== [C] 零 plan template 逐一展開（完整身分） ==='
select t.id, t.slug, t.name, t.category, t.is_active, t.is_hidden,
       t.conceal_instances, t.max_per_partner, t.workload_type,
       t.create_date, t.write_date
from woow_paas_platform_cloud_app_template t
where not exists (select 1 from woow_template_plan_rel r where r.template_id = t.id)
order by t.id;

\echo '=== [D] 陽性對照：cloud_service 全表筆數＋template_id 可否為 NULL ==='
select count(*) as cs_total, count(template_id) as cs_with_template from woow_paas_platform_cloud_service;
select is_nullable from information_schema.columns
 where table_name='woow_paas_platform_cloud_service' and column_name='template_id';
```

### `949-q2.sql`

```sql
\echo '=== [E] 掛在零 plan template 上的 cloud_service ==='
select cs.id, cs.name, cs.template_id, cs.state, cs.workspace_id,
       cs.subscription_id, cs.provision_partner_id, cs.create_date
from woow_paas_platform_cloud_service cs
where cs.template_id in (3, 41) order by cs.id;

\echo '=== [F] 這些服務的訂閱狀態與目前 plan ==='
select cs.id as cs_id, s.id as sub_id, s.state as sub_state,
       s.sh_subscription_plan_id as plan_id, p.name as plan_name,
       p.service_type, p.sh_plan_price
from woow_paas_platform_cloud_service cs
left join sh_subscription_subscription s on s.id = cs.subscription_id
left join sh_subscription_plan p on p.id = s.sh_subscription_plan_id
where cs.template_id in (3, 41) order by cs.id;

\echo '=== [G] 爆炸半徑上限：catalog 全 plan 數（若閘真的可達） ==='
select count(*) as plan_total from sh_subscription_plan;
select service_type, count(*) from sh_subscription_plan group by service_type order by 1;
```

### `949-q3.sql`

```sql
\echo '=== [H] plan 35 是否 orphan（三個 family rel 表都查） ==='
select 'cloud_app' as fam, count(*) from woow_template_plan_rel where plan_id=35
union all select 'smart_home', count(*) from information_schema.tables where table_name like '%smart_home_template%plan%';
\echo '--- 列出所有含 plan 的 rel 表 ---'
select table_name from information_schema.tables where table_name like '%plan%rel%' or table_name like '%rel%plan%' order by 1;
```

### `949-q4.sql`

```sql
\echo '=== [H2] plan 35 在三個 family rel 表的掛載數（orphan 判準） ==='
select 'cloud_app_template' as fam, count(*) as n from woow_template_plan_rel where plan_id=35
union all
select 'smart_home_template', count(*) from woow_smart_home_template_plan_rel where plan_id=35
union all
select 'security_access_template', count(*) from woow_security_access_template_plan_rel where plan_id=35;

\echo '=== [H3] 陽性對照：一個已知有掛的 plan（隨便取一筆 rel 的 plan_id）同樣查法應 >0 ==='
select r.plan_id, 'cloud_app_template' as fam, count(*) as n
from woow_template_plan_rel r
where r.plan_id = (select min(plan_id) from woow_template_plan_rel)
group by r.plan_id;

\echo '=== [I] 全 catalog 的 orphan plan 盤點（三表皆無掛載） ==='
select p.service_type, count(*) as orphan_plans
from sh_subscription_plan p
where not exists (select 1 from woow_template_plan_rel r where r.plan_id=p.id)
  and not exists (select 1 from woow_smart_home_template_plan_rel r where r.plan_id=p.id)
  and not exists (select 1 from woow_security_access_template_plan_rel r where r.plan_id=p.id)
group by p.service_type order by 1;

\echo '=== [I2] orphan plan 逐筆（僅 cloud_service 家族） ==='
select p.id, p.name, p.service_type, p.sh_plan_price
from sh_subscription_plan p
where p.service_type='cloud_service'
  and not exists (select 1 from woow_template_plan_rel r where r.plan_id=p.id)
  and not exists (select 1 from woow_smart_home_template_plan_rel r where r.plan_id=p.id)
  and not exists (select 1 from woow_security_access_template_plan_rel r where r.plan_id=p.id)
order by p.id;
```

> ⚠ `q3` 是找 rel 表名的探路查詢，結論已被 `q4` 取代，保留只為完整重現。

## 2. psql 逐字輸出（q1／q2／q4 重跑落檔）

```
### /tmp/949-q1.sql ###
=== [A] 陽性對照：template 全表筆數 ===
 template_total 
----------------
             16
(1 row)

=== [B] 每 template 掛的 plan 數分布（含零） ===
 n_plans | n_templates 
---------+-------------
       0 |           2
       2 |           2
       4 |           8
       8 |           1
      44 |           1
      48 |           2
(6 rows)

=== [C] 零 plan template 逐一展開（完整身分） ===
 id |  slug   |  name   | category  | is_active | is_hidden | conceal_instances | max_per_partner | workload_type |        create_date         |        write_date         
----+---------+---------+-----------+-----------+-----------+-------------------+-----------------+---------------+----------------------------+---------------------------
  3 | redis   | Redis   | database  | f         |           |                   |               0 | container     | 2026-06-01 15:06:08.854375 | 2026-08-20 08:12:43.35686
 41 | ha-paas | HA PaaS | container | t         | t         | t                 |               1 | container     | 2026-07-13 02:27:02.821601 | 2026-08-20 08:12:43.35686
(2 rows)

=== [D] 陽性對照：cloud_service 全表筆數＋template_id 可否為 NULL ===
 cs_total | cs_with_template 
----------+------------------
       11 |               11
(1 row)

 is_nullable 
-------------
 NO
(1 row)


### /tmp/949-q2.sql ###
=== [E] 掛在零 plan template 上的 cloud_service ===
 id |  name   | template_id |   state   | workspace_id | subscription_id | provision_partner_id |        create_date         
----+---------+-------------+-----------+--------------+-----------------+----------------------+----------------------------
 43 | HA PaaS |          41 | suspended |           33 |              32 |                   68 | 2026-07-13 15:37:27.930304
 85 | HA PaaS |          41 | running   |           41 |              73 |                    3 | 2026-08-17 14:35:31.819028
 86 | HA PaaS |          41 | running   |           42 |              74 |                   71 | 2026-08-19 06:55:43.115863
(3 rows)

=== [F] 這些服務的訂閱狀態與目前 plan ===
 cs_id | sub_id |  sub_state  | plan_id |  plan_name   | service_type  | sh_plan_price 
-------+--------+-------------+---------+--------------+---------------+---------------
    43 |     32 | expire      |      35 | HA PaaS Free | cloud_service |             0
    85 |     73 | in_progress |      35 | HA PaaS Free | cloud_service |             0
    86 |     74 | in_progress |      35 | HA PaaS Free | cloud_service |             0
(3 rows)

=== [G] 爆炸半徑上限：catalog 全 plan 數（若閘真的可達） ===
 plan_total 
------------
        320
(1 row)

  service_type   | count 
-----------------+-------
 cloud_service   |   188
 security_access |    88
 smart_home      |    44
(3 rows)


### /tmp/949-q4.sql ###
=== [H2] plan 35 在三個 family rel 表的掛載數（orphan 判準） ===
           fam            | n 
--------------------------+---
 cloud_app_template       | 0
 smart_home_template      | 0
 security_access_template | 0
(3 rows)

=== [H3] 陽性對照：一個已知有掛的 plan（隨便取一筆 rel 的 plan_id）同樣查法應 >0 ===
 plan_id |        fam         | n 
---------+--------------------+---
       1 | cloud_app_template | 1
(1 row)

=== [I] 全 catalog 的 orphan plan 盤點（三表皆無掛載） ===
 service_type  | orphan_plans 
---------------+--------------
 cloud_service |            4
(1 row)

=== [I2] orphan plan 逐筆（僅 cloud_service 家族） ===
 id  |             name             | service_type  | sh_plan_price 
-----+------------------------------+---------------+---------------
   7 | AnythingLLM Monthly          | cloud_service |           299
   8 | AnythingLLM Yearly           | cloud_service |          2990
  35 | HA PaaS Free                 | cloud_service |             0
 268 | Hermes Agent stg 測試（0元） | cloud_service |             0
(4 rows)


```

## 3. 從輸出讀出來的四件事

1. **零方案 template ＝ 2 個**（`redis` `is_active=f`／`ha-paas` `is_active=t` 但
   `is_hidden=t`、`conceal_instances=t`），合計 16 個 template（陽性對照相符）。
2. **掛在其上的 CloudService ＝ 3 筆**，全在 `ha-paas`；其中 `#43` 的訂閱已 `expire`
   ⇒ 就算閘可達也會被第 5 關擋掉 ⇒ 真正的影響面是 **2 筆**，不是 3 筆。
3. **這 3 筆正在合法使用一個 orphan plan**（`#35 HA PaaS Free`，三個 family rel 表皆 0）。
   這是 seed 刻意的設計，見 `src/data/subscription_template_links.xml` 的
   「HA PaaS plan 刻意 orphan」註解；HA 開通走 `controllers/ha_paas.py` 直接
   `create()`＋直接 `env.ref(plan)`，**完全不經** `_create_service` 的白名單閘
   ⇒ 收緊兩條閘都動不到 HA PaaS 開通。
4. **CS 沒有 template-less 這一格**：`cloud_service.template_id` `required=True`，
   `information_schema` 回 `is_nullable=NO`，資料側 11/11 皆非 NULL。
   ⇒ 不需要 SA 端的 `else`（家族判準）分支；修法就是把 truthiness 前綴刪掉。

## 4. 未涵蓋

- 本檔只量 `dev-sandbox-stg`；prod 見 #949 留言 10244。
- 未量「admin 新建 template 後、掛方案前」那個時間窗 —— 那是設計面推論，非量測。
