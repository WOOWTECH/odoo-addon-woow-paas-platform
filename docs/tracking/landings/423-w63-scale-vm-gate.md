# project 423 w63 scale vm gate

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_423_w63_scale_vm_gate`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#486/#488 scale_release VM 擇一缺陷——舊 gate 讓 suspend 只停 proxy、VM 照燒；缺陷被一條「斷言不會查 VM」的測試保護著"

2026-07-30 交付（leader2 派工，teammate haos-op-sa）。PR **#488**、分支
`fix/423-scale-vm-gate`、SHA `2a6c2d58`、base `a0cb49a9`，`Closes #486`。**已收官**：merge 後在 stg **真混合拓撲**驗證通過（suspend 兩半都停、quota 完整
釋放、operator log 可見 VM patch）——亦即本地只能 skip 的混合案，確實由 stg 重測承接
補上了。#486 已關、#423 v1 收官。

**缺陷**：`scale_release` 的 `if not deployments and not statefulsets:` 只在沒有容器
workload 時才查 VirtualMachine。HAOS chart 0.1.2 加上 xff-proxy Deployment 後閘門永遠
不成立 ⇒ suspend 只把 proxy scale 到 0，VM 從未被 patch runStrategy。stg 實測
（`paas-ws-stg-0f5bf95d` / `svc-5851cdbf`）：Odoo state=suspended、proxy replicas=0，
但 VM 仍 Running、quota 只釋放 50m，VM 的 2000m/4.4Gi 照燒。**比它前一版修掉的 404
更危險**：欠費停權帳面成立、成本全額在燒，端點回 200 零錯誤訊號。

值得記住的三點：

1. **缺陷是被測試保護著的**——`test_container_release_never_queries_virtualmachines`
   斷言「有容器 workload 時不得查 VM」。這是本輪最大的教訓，已抽成
   [[feedback_test_asserting_absence_locks_shortcut]]。

2. **同形風險要一起收**：拆掉 gate 後，混合拓撲下容器那半會成功，所以一個被吞掉的
   Forbidden 會讓端點回 200「已 suspend」而 VM 照跑——與原缺陷同結果、換個成因。
   收窄成「只有 CRD-absent 可被容忍」並補測試。leader2 裁可此順修。

3. **skip 不可冒充通過**：VM 相關整合案需 KubeVirt CRD，docker-desktop 沒有。沿用
   repo 既有 `_skip_or_fail`——`REQUIRE_KUBEVIRT=1` 時把 skip 升級成 **fail**（我實測
   驗過真的會 fail）。docker-desktop 上真正跑得動、也真有價值的是「容器-only 在**真的
   沒有 KubeVirt** 的叢集照樣 scale」：`is_crd_absent_error` 是字串比對，只有真 kubectl
   的 stderr 說了算，mock 測得到「有 catch」卻測不到「這個 stderr 落不落進判別式」。

數字：unit 502 passed/44 deselected（baseline 實跑 496/41）、integration 40 passed/
4 skipped（baseline 39/2）、紅燈驗證 6/6。

相關：[[project_423_w6_default_sa_imagepullsecrets]]（同一 v1 阻斷週期的前一件）、
[[reference_tea_cli_write_gotchas]]（本輪新增第 7、8 點皆出自這次）。
