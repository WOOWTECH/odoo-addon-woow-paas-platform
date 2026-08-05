# reference act runner dind mtu custom network

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_act_runner_dind_mtu_custom_network`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：2026-06-04 起 Gitea CI lychee job 全紅根因 — act-runner dind 自訂 network MTU 1500 > pod eth0 1450，外網大封包（TLS/下載）被丟；daemon.json mtu 1400 只作用 docker0

**現象**：git-prod.woowtech.io 的 lychee job（docs-link-check）自 2026-06-04 起每次都紅，死在「下載 lychee binary」步驟，curl exit 35（SSL connect error），跑 ~2m31s（重試到超時）。與 PR 內容無關；`continue-on-error: true` 所以不擋 merge。

**根因鏈**（2026-06-06 在 woow-k3s 實測驗證）：
1. `gitea/act-runner-runner-0`（StatefulSet，Helm release `act-runner`，dind 跑在 native sidecar initContainer）於 2026-06-03 00:55 UTC 重新部署（rev1 docker:29.5.1-dind → rev2 01:42 降版 28.5.2-dind）；最後一次成功 build 就是重佈前那一刻
2. act_runner 對每個 job task 建**自訂 docker network**；dind `daemon.json` 的 `"mtu": 1400` **只作用在預設 docker0**，自訂 network 一律 MTU 1500
3. runner pod eth0 MTU 1450（flannel vxlan）→ job container 的 1500 大封包被丟 → 對外 TLS handshake / 下載失敗
4. 實測：自訂 network 容器（1500）wget GitHub release timeout；預設 bridge（1400）OK；runner pod 本身 OK

**修法**（尚未套用，2026-06-06 使用者決定只出報告）：patch `gitea/dind-daemon-config` ConfigMap（kubectl apply 手動管理，無 GitOps ownership，patch 不會被沖掉）加：
```json
"default-network-opts": { "bridge": { "com.docker.network.driver.mtu": "1400" } }
```
（docker ≥26 支援；現為 28.5.2）然後 delete pod `act-runner-runner-0` 重啟，再用 `docker network create test-mtu && docker run --network test-mtu alpine wget <github release url>` 驗證。

**辨識特徵**：CI step 在下載外部資源處 exit 35 / timeout、小流量步驟（checkout、git 對內網）正常、dind daemon 自己 pull image 正常（走 dind eth0 非 job network）→ 先懷疑 MTU，不要去查文件內容或 GitHub rate limit。
