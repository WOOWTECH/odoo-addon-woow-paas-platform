---
title: kubectl exec／cp 從 pod 拉大串流會尾端截斷且 exit 0
category: tracking
status: active
created: 2026-09-03
updated: 2026-09-03
related: [docs/plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md, docs/guides/deployment/prod-release-sop.md]
---

# kubectl exec／cp 從 pod 拉大串流會尾端截斷且 exit 0

## 症狀

對 woow-k3s（apiserver 經 tailscale）跑 `kubectl exec <pod> -- pg_dump -Fc > file` 或 `kubectl cp <pod>:/x ./x`，
檔案比源頭小、**kubectl exit 0、stderr 空**。`pg_restore -l` 照樣列出完整 TOC（17,942 行），restore 卻在中途
`could not read from input file: end of file`——而且是「部分成功」：DB 91 MB、`res_users` 0 筆，不是整個拒收。

2026-09-03 Phase 2 排練（epic #1032）量測：

| 串流 | 源頭 bytes | 收到 bytes | 備註 |
| --- | --- | --- | --- |
| aiot `pg_dump -Fc`（第 1 次） | 22,631,780 | 19,050,496 | exit 0，4096 倍數 |
| 同上（第 2 次，`KUBECTL_REMOTE_COMMAND_WEBSOCKETS=false`＋尾端 `sleep 5`） | 22,631,780 | 19,664,896 | exit 0 |
| 沙盒 ns 30 MB `/dev/urandom`，websocket | 30,000,000 | 25,935,872 | 前綴逐 byte 相同 |
| 同上，SPDY（log `101 Switching Protocols`） | 30,000,000 | 26,345,472 | 兩協定都丟 |
| 0.5／1／2／4／8 MB 單次 exec | — | 全完整 | 門檻約 20 MB 起 |
| aiot filestore `tar czf -` 1.9 GB | 1,963,793,515（tar 前） | 1,644,105,723 | 19 分後 `connection reset by peer`，exit 1 |

kubectl client v1.36.1；上傳方向（`kubectl cp` 本機→pod 19 MB、`tar | kubectl exec -i`）未見截斷。

## 根因

exec 的 stdout 串流在 apiserver↔client 這段（本例還多一層 tailscale）會丟尾端，kubectl 不把它當錯誤。
websocket／SPDY 都會；不是程序提早結束（尾端加 `sleep` 無效）。確切在哪一層丟未追到底，
但**量測面已足以定案：這條路徑不能拿來搬 >8 MB 的檔**。

## 修法（實測過的）

1. **叢集內 pod-to-pod push（正解）**：源頭 ns 的 NetworkPolicy 多半只擋 ingress、不擋 egress（aiot 的 `default-deny-external` 即如此）；
   在自己的 ns 加一條 ingress NP 放行來源 ns → 目標 pod 跑一支 HTTP PUT 接收器（python，支援 chunked，回 bytes＋sha256）；
   源頭 pod `tar cf - | gzip -1 | curl -T - -X PUT http://<pod-ip>:8123/x.tgz`、`pg_dump -Fc | curl -T - …`。exec 只跑 producer、stdout 幾十 bytes。
   實測 filestore 1.66 GB **58 s**、dump 22.6 MB **4.4 s**，兩端 sha256／檔數／bytes 全等。
   密碫不落地：`kubectl exec <pg-pod> -- printenv POSTGRES_PASSWORD | kubectl exec -i <odoo-pod> -- sh -c 'read -r PW; PGPASSWORD=$PW pg_dump …'`。
2. 真的要到本機（離叢集副本）：HTTP over `port-forward` 完整但只有約 1–1.4 MB/s，且 port-forward 會死；python `http.server` **不支援 Range**，`curl -C -` 無效
   ⇒ pod 內 `split -b 100m` ＋ `sha256sum` 清單，本機逐塊下載、逐塊校驗、失敗重開 port-forward 重試。
3. port-forward ＋ 本機 `pg_dump`：完整但 round-trip bound（6.3 MB 花 240 s），對 aiot 在 `COPY ir_attachment` 中途 tunnel 卡死（backend `ClientRead` 閒置 9 分）。不建議。

## 驗完整性的正確判準

- dump：`pg_restore -f /dev/null <dump>`（全讀）；`-l` 只讀檔頭，**驗不到截斷**。
- tgz：`gzip -t`（trailer CRC＋ISIZE）＋ `tar -tzf | grep -v /$ | wc -l` 與源頭 `find -type f | wc -l` 比、bytes 與源頭 `du -sb` 比。
- 「sha256 相符」只證明本機檔＝本機檔；要拿源頭側的 `pg_dump | wc -c`／`sha256sum` 當對照。

## 波及範圍

- `.claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh` 第 2 步 `kubectl cp` 從 stg pod 拉 dump 到本機＝同一條路徑；stg dump 超過 ~20 MB 即會截斷（restore 會報 end of file，不靜默，但要知道原因不在 dump 本身）。待改：改成叢集內 pod-to-pod 或加 `pg_restore -f /dev/null` 驗證。
- 移植 plan Phase 2.1／Phase 3／Phase 4 的搬運步驤（已在 plan 內改寫）。
- `docs/guides/testing/odoo-sandbox-ci-local-two-track-test.md` 的 `tar | kubectl exec -i` 是上傳方向，實測未受影響。

## 守線

判斷類，無法機械化（需要源頭側對照量測）。本檔＋ plan 內的搬運步驟改寫；`seed-from-stg.sh` 列 follow-up。
