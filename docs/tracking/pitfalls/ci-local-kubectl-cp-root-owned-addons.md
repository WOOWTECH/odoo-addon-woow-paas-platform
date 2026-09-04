---
name: ci-local-kubectl-cp-root-owned-addons
description: ci-local sandbox 的 /mnt/extra-addons 整棵是 root-owned、容器卻以 uid=100(odoo) 執行，kubectl cp 熱蓋檔案被 tar 報成「Cannot open: File exists」而不是 Permission denied（訊息把人導向「查衝突」的死路）；繞法是 kubectl debug --target 借 root 身分 chown，且 chown 必須用數字 100:101——busybox debug image 用名字會回 unknown user/group 但不中斷整條指令，看起來執行完了其實沒生效
metadata:
  type: pitfall
---

# Pitfall: ci-local sandbox 的 `kubectl cp` 撞 root-owned addons 樹，`tar` 卻報「File exists」

> 來源：2026-08-10 hermes-agent 驗收 session。在 `docker-desktop` 的 ci-local sandbox
> `dev-sandbox-hermes013` 想把改好的 `src/data/cloud_app_templates.xml` **熱蓋進 pod**
> （不重建 image）以便跑測試，`kubectl cp` exit 2 失敗，訊息說「File exists」——
> 真因是 permission denied。
>
> **姊妹篇**：[[ci-local-stale-pod-green-tests-on-old-code]] —— 同一個 ci-local 設計的另一半。
> 本篇是「**你想熱蓋進去卻蓋不進去**」，姊妹篇是「**你根本不知道 pod 裡跑的是哪個版本**
> （而且測試會照樣給你綠燈）」。兩者咬合：發現 pod 退回舊碼 → 想用 `kubectl cp` 補救 →
> 撞上本篇的權限牆。**會來讀本篇的人，多半也需要那一篇。**
>
> **證據來源分兩層**：逐字錯誤訊息、原始路徑／權限實值、可複製的指令序列由當次操作者
> 提供（第一手）；**根因與影響範圍由本檔作者在同一天、同一顆 sandbox 上以唯讀方式
> 重新查證**（見 §Root cause 每條後面的證據）。原始素材把 root-owned 的成因列為
> 「未解的開放問題、懷疑是 `docker cp` 弄髒的」——那個懷疑是**錯的**，見成因 ②。

## Symptom

```bash
NS=dev-sandbox-hermes013
POD=$(kubectl --context=docker-desktop -n $NS get pod \
  -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')
kubectl --context=docker-desktop -n $NS cp \
  <本機檔案> "$NS/$POD:/mnt/extra-addons/woow_paas_platform/data/cloud_app_templates.xml"
```

逐字輸出（exit code 2）：

```
tar: cloud_app_templates.xml: Cannot open: File exists
tar: Exiting with failure status due to previous errors
```

**這句話會把人導向「為什麼會有檔案衝突」——那是死路。** 真因是目標目錄對執行身分沒有
寫入權限。看到 `tar: ...: File exists` 從 `kubectl cp` 冒出來時，**不要去想衝突，直接做
下面的一分鐘診斷。**

## 一分鐘診斷（先驗身分與擁有者，別信訊息字面）

```bash
kubectl --context=docker-desktop -n $NS exec "$POD" -- sh -c "id"
# uid=100(odoo) gid=101(odoo) groups=101(odoo)

kubectl --context=docker-desktop -n $NS exec "$POD" -- \
  sh -c "ls -ld /mnt/extra-addons/woow_paas_platform/data"
# drwxr-xr-x 3 root root 4096 ... /mnt/extra-addons/woow_paas_platform/data

# 決定性的一條：直接試寫
kubectl --context=docker-desktop -n $NS exec "$POD" -- \
  sh -c "touch /mnt/extra-addons/woow_paas_platform/data/__wtest__"
# touch: cannot touch '.../__wtest__': Permission denied   (exit=1)
# ↑ 被擋＝確診。沒被擋＝不是這個坑，記得把 __wtest__ 刪掉（別留在別人的 pod 裡）
```

`id` 的 uid/gid 不等於 `ls -ld` 的 owner/group、而 other 位元又沒有 `w` ⇒ 就是這個坑，
不必再查別的。

## Root cause

### ① `kubectl cp` 的執行身分鎖死在容器的 runtime user

`kubectl cp` 底層是 `tar czf - <src> | kubectl exec -i <pod> -- tar xf - -C <dst>`：
**遠端解壓的那一半在 pod 內、以 `kubectl exec` 的身分執行**，而 `kubectl exec` 沒有
`--user`／`-u` 這種指定身分的旗標（不像 `docker exec -u root`），它固定用 image config
裡的 runtime user。

> 查證：`kubectl exec --help` 的 Options 只有 `-c/--container`、`-f/--filename`、
> `--pod-running-timeout`、`-q/--quiet`、`-i/--stdin`、`-t/--tty`——沒有指定身分的旗標。

本例 image 以 `USER odoo`（uid 100）收尾 ⇒ 重試幾次都是同一個受限身分。這顆 pod 只有
一個 container（`odoo`），沒有以 root 執行的 sidecar 可以 `-c` 切過去；`sudo`／`su`
需要密碼，非互動環境下必失敗。

### ② `/mnt/extra-addons` 整棵是 root-owned —— 而且是**建置路徑的結構性缺口**，不是環境髒污

pod 內唯讀實測（2026-08-10，pod `hermes013-odoo-dev-sandbox-odoo-7c4dc98597-94l8b`）：

| 物件 | 擁有者／模式 |
| --- | --- |
| `/mnt/extra-addons`（目錄本身） | `odoo:root`，`drwxr-xr-x` |
| 其下 9 個 addon 目錄（含 `woow_paas_platform`） | `root:root`，`drwxr-xr-x` |
| `.../woow_paas_platform/data/` | `root:root`，`drwxr-xr-x`（0755） |
| `.../data/cloud_app_templates.xml` | `root:root`，`-rw-r--r--`（0644） |
| `find /mnt/extra-addons -not -user odoo \| wc -l` | **1358**（總項目 1359） |

權限算術：odoo（uid 100 / gid 101）既不是 owner（uid 0）也不在 group（gid 0）裡 ⇒ 落在
**other**，而 other 對目錄只有 `r-x`。**這代表 odoo 對這些目錄完全沒有寫入權限，連新建
檔案都不行，不只是「不能覆蓋既有檔」。**

⚠ **反例陷阱**：`/mnt/extra-addons` **本身**是 odoo 擁有的（base image `odoo:18.0` 帶的），
所以 cp 到那一層會成功、往下一層就失敗。別用「我上次 cp 有成功過」推論這次也可以。

**為什麼會是 root-owned**：這顆 image 不是用本 repo 的 `Dockerfile.dev` 建的。

- `Dockerfile.dev`（repo 根目錄）在 `USER odoo` 之前有
  `RUN chown -R odoo:odoo /mnt/extra-addons /mnt/extra-addons-ext`，照它建出來的 image
  不會有這個問題。
- 但 ci-local 的 image 走的是
  [docker-desktop-ci-local-sandbox-image-import](../../guides/testing/docker-desktop-ci-local-sandbox-image-import.md)
  的**手寫 Dockerfile**——該 guide 明講「**別用 plugin 的 `build.sh`**」（`Dockerfile.dev`
  的 `COPY extra/extra-addons/` 指向不存在的目錄，base 也缺該 recipe 列出的那幾個 CI
  相依），於是繞過 `build.sh` 的同時**也一併繞過了那行 `chown`**。
- 查證：`docker history paas-odoo-sandbox:hermes013`（`sha256:781abe2d…`，建於
  2026-08-10T04:18:26Z，正是現行 pod 跑的 imageID）的自建層依序是
  `USER root` → `COPY req.txt /tmp/req.txt` → `pip3 install …` →
  `COPY addons/ /mnt/extra-addons/` → `sed -i '/^fastembed/d' …` → `USER odoo`。
  **沒有任何 chown 層。**

⇒ **照那份 guide 建出來的每一顆 ci-local image 都是這樣**，不是這顆 sandbox 被誰弄髒。
（原始素材推測「可能是更早的 session 用 `docker cp` 寫髒的」——`docker history` 與
podSpec 兩邊都不支持這個推測：image 本身就沒 chown，pod 也沒有任何蓋在
`/mnt/extra-addons` 上的 volume 或 initContainer。）

### ③ 為什麼訊息是「File exists」而不是「Permission denied」

**觀察到的事實**（第一手，可據以辨識）：覆蓋一個**既有檔案**時，權限不足會被 `tar`
報成 `Cannot open: File exists`。

**同一件事換個寫法，訊息就誠實了**——同一顆 pod、同一個目標檔，改用 stdin 直寫：

```
kubectl exec -i … -- sh -c 'cat > .../cloud_app_templates.xml'
# sh: 1: cannot create .../cloud_app_templates.xml: Permission denied   (exit 2)
```

⇒ **說謊的是 `tar`，不是 K8s，也不是你的權限判斷。** 這條也可以當第二個診斷探針：
`kubectl cp` 報 `File exists` 而 `cat >` 報 `Permission denied` 時，答案已經出來了。

**機制推測（未查 tar 原始碼，標為待驗）**：GNU tar 展開既有檔時先以 `O_EXCL` 開檔、
撞 `EEXIST` 後改走「先 unlink 再建」；unlink 需要的是**目錄**的寫入權限，這一步失敗後
tar 回報的是最初那個 `EEXIST`，於是 `EACCES` 被吃掉。**同理推測**：目標檔**不存在**時
（純新增），訊息應該會是誠實的 `Cannot open: Permission denied`——這條同樣未實測，但
可以解釋為什麼有人覺得「有時候訊息是對的」。

不論機制如何，操作準則不變：**`kubectl cp` 吐出 `File exists` 一律先驗身分與 owner。**

## Fix：`kubectl debug --target` 借一個 root 身分進去 chown

`kubectl exec` 出不去的地方，`kubectl debug` 可以——因為它掛的是**一顆新的 ephemeral
container**：

- 它用**自己指定的 image**（這裡 `busybox:1.36`），預設身分是該 image 的預設 user
  （busybox 是 **root**），完全不受目標 container 的 `USER odoo` 限制。
- `--target=<container>` 讓它共用目標 container 的 **process namespace**（pod 沒開
  `shareProcessNamespace: true` 也可以，`--target` 就是為此設計）。
- 於是可以透過 `/proc/<pid>/root/`（該行程看到的根目錄）**以 root 身分讀寫目標
  container 的整個檔案系統**。本例 odoo 主行程是 pid 1。

```bash
NS=dev-sandbox-hermes013
POD=<每次重查，pod 名會變>

# 🔴 chown 一定要用數字 UID:GID。用名字（odoo:odoo）會回
#    "chown: unknown user/group odoo:odoo" 但**不中斷後續指令**——
#    busybox image 自己的 /etc/passwd 根本沒有 odoo 這個帳號。
kubectl --context=docker-desktop -n $NS debug "$POD" \
  --image=busybox:1.36 --target=odoo --container=debugger-chown -- sh -c "
chown 100:101 /proc/1/root/mnt/extra-addons/woow_paas_platform/data
chown 100:101 /proc/1/root/mnt/extra-addons/woow_paas_platform/data/cloud_app_templates.xml
ls -ld /proc/1/root/mnt/extra-addons/woow_paas_platform/data
"

# kubectl debug 沒帶 -it 時不會即時串流輸出，要另外撈 log 才看得到結果：
kubectl --context=docker-desktop -n $NS logs "$POD" -c debugger-chown
# 期望：drwxr-xr-x 3 100 101 ... data      ← owner 變 100/101 才算生效

# 改完 owner，原本失敗的 cp 直接成功（exit 0）
kubectl --context=docker-desktop -n $NS cp \
  <本機檔案> "$NS/$POD:/mnt/extra-addons/woow_paas_platform/data/cloud_app_templates.xml"

# 一定要另外驗 pod 內真的是新值，不要只信 cp 的 exit code
kubectl --context=docker-desktop -n $NS exec "$POD" -- \
  grep -c '<你這次改動的特徵字串>' /mnt/extra-addons/woow_paas_platform/data/cloud_app_templates.xml
```

### 這一段最容易再踩的兩處

1. **`chown odoo:odoo` 會靜默失敗。** busybox debug image 沒有 `odoo` 這個帳號，named
   chown 回 `unknown user/group` 但**整條 `sh -c` 繼續往下跑**，而輸出又不會即時串流
   ⇒ 「指令看起來執行完了」和「真的生效了」長得一模一樣。**用數字 `100:101`**，並且
   **拿 `ls -ld` 的 owner 欄當驗收**，不要拿「指令沒報錯」當驗收。
2. **`chmod u+w` 完全沒用。** 原本就是 `0755`/`0644`，owner 早就有 `w` 位元——缺的是
   「owner 是誰」。要動的是 **chown 不是 chmod**。（第一輪就是在這裡繞了彎路。）

## 這個修改活不過下一次 restart（好消息：不必手動還原）

ci-local 模式的 `/mnt/extra-addons` 是**烤進 image 的層，沒有掛任何 volume**——本次唯讀
查證 podSpec：`volumes` 只有 `odoo.conf` 的 configMap 與 `/var/lib/odoo` 的 PVC，
`initContainers: null`，沒有任何東西蓋在 `/mnt/extra-addons` 上。

`kubectl rollout restart` 會用同一顆 image 起全新 container，檔案系統從 image layer
重新展開 ⇒ 前一個 pod 實例上的 chown 與 cp **全部蒸發**。而
沙盒 `test`（現為 woow-dev plugin `test.sh`（`${CLAUDE_PLUGIN_ROOT}/skills/k8s-dev-sandbox/scripts/test.sh`），原 in-repo `woow-test.sh`）在**測試成功與失敗兩條路徑的最後都會**
`kubectl rollout restart` + `rollout status`（因為測試用 `--stop-after-init` 會把 odoo
停掉，要重新拉起來）。

⇒ **跑一次 `woow-test.sh`，熱蓋進去的東西就沒了。** 事後在新 pod 上覆查：owner 回到
`root:root`、檔案內容也回到 image 烤進去的舊值。

好處是不需要（也無法）手動 revert；壞處是「改一次 → 跑測試 → 再改一次」的每一輪都得
把 chown + cp 重做一遍。要迭代多輪時，先接受這個節奏，或直接重建 image。

⚠ **真正的危險不在「白做工」，在於你可能不知道它已經沒了。** pod 重建後一切看起來正常
（`1/1 Running`、無警告），測試照跑照綠，只是綠的是 image 烤進去的舊碼。這一半寫在姊妹篇
[[ci-local-stale-pod-green-tests-on-old-code]]——**熱蓋完 code 之後、跑測試之前，先讀一次
pod 內的版號**，那份 doc 有可直接複製的前置查核。

## 根治：把 chown 補回建置 recipe

在該 guide 的手寫 Dockerfile 的 `USER odoo` **之前**補一行——就是 `Dockerfile.dev` 從
2026-02-28 起一直都有的那行：

```dockerfile
RUN chown -R odoo:odoo /mnt/extra-addons
```

⚠ **待補（未實測）**：本次已把這行補進上述 guide 的 recipe，但**沒有重建 image 驗過**
（當天 host 磁碟 100%、明令禁止 `docker build`／`docker save`）。下一個重建 ci-local
image 的人請順手驗一條：

```bash
docker run --rm <tag> find /mnt/extra-addons -not -user odoo | head
# 期望：空。非空就是 chown 沒生效（或又被新的 COPY 層蓋過去）
```

## 什麼條件下會再出現

- **會撞**：ci-local 模式（或任何「code 烤進 image」的部署）＋ 需要把檔案熱蓋進 pod。
- **不會撞**：hostPath／local 模式——但**不是因為權限比較鬆**，而是那個模式下根本不需要
  `kubectl cp`（worktree 直接 bind-mount，改 host 上的檔案 pod 立刻看得到）。注意
  docker-desktop 上 local 模式已不可用，見上述 guide。
- **同一個身分限制、換個包裝一樣中**：任何「pipe 進 `kubectl exec`」的手法都跑在同一個
  runtime user 上。[odoo-sandbox-ci-local-two-track-test](../../guides/testing/odoo-sandbox-ci-local-two-track-test.md)
  §覆蓋 addon 那條 `tar czf - | kubectl exec -i … tar xzf -` 一行機制相同，理應同樣撞
  （**未實測**，此處只做機制推導）；而且它還多一步 `rm -rf <dst>`，那需要的是**父目錄**
  的寫入權限，一樣沒有。
- **判斷通則**：任何「以非 root `USER` 收尾、但 COPY 進去的東西沒 chown」的 image，都是
  這個形態。跟 image 的 `USER` 指令**有關**，跟 K8s 的 `securityContext` 無關（本例
  podSpec 的 `securityContext` 是空的，身分完全由 image 決定）。

## 機械化狀態：`—`（已評估，卡在沒有可掛載點）

想得到的 gate 是「build 收尾跑 `docker run --rm <tag> find /mnt/extra-addons -not -user
odoo`，非空即 fail」——判準乾淨、零閾值。**沒做的原因不是它不好，是沒有地方掛**：那條
build 路徑是一份**散文 recipe**（人工 `docker build`），repo 內沒有對應腳本可以插檢查
（plugin 的 `build.sh` 走的是另一份 `Dockerfile.dev`，而它正好是**沒有**這個問題的那條
路徑——對它加 gate 等於守一個不會紅的地方）。

⇒ 要機械化，前置條件是**先把那份 recipe 收成腳本**。在那之前這條靠 doc 守；下一個把
recipe 腳本化的人，請把上面那條 `find` 檢查一起帶進去。

## 相關

- 索引：[docs/tracking/pitfalls/README.md](README.md)
- **姊妹篇**：[[ci-local-stale-pod-green-tests-on-old-code]] — 同一個 ci-local 設計的另一半：
  pod 重建後靜默退回舊碼，而測試會給你綠燈
- 產生這顆 image 的 recipe（本坑的源頭，已補 chown）：
  [docker-desktop-ci-local-sandbox-image-import](../../guides/testing/docker-desktop-ci-local-sandbox-image-import.md)
- ci-local 跑測試的其他坑：
  [odoo-sandbox-ci-local-two-track-test](../../guides/testing/odoo-sandbox-ci-local-two-track-test.md)
- 同 family（**別信工具給的成功／失敗訊號，去看被操作對象的實際狀態**）：
  - [[sandbox-module-test-false-green]] — `woow-test.sh` 只看 exit code ⇒ 0 tests 也報綠
  - [[readonly-audit-default-report-overwrites-repo]] — exit 0 且無警告，但檔案已被洗掉
