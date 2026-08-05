# reference docker desktop ci local sandbox image import

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_docker_desktop_ci_local_sandbox_image_import`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：docker-desktop 上 k8s-dev-sandbox local 模式已死，改走 ci-local＋手動把本機 image 匯進 kind node containerd 的完整 recipe

2026-07-29（#464 headed E2E）實證：**`docker-desktop` 的 k8s-dev-sandbox `local` 模式（hostPath）已不可用**，替代路線是 `ci-local` ＋ 手動匯 image。

**為什麼 local 死了**：Docker Desktop 的 k8s node 現在是 kind 風格容器 `desktop-control-plane`（本機該 node 建立於 ~2026-07-07），mounts 只有 `/etc/kind/hosts.toml`、`/lib/modules`、一顆 docker volume，**沒有 macOS `/Users`** → hostPath 指不到 worktree。（同 [[reference_docker_build_local_odoo_test]] 的根因記載。）

**ci-local 完整 recipe**：

1. **自建 image**（`FROM odoo:18.0`，**有 arm64**＝Apple Silicon 原生、不走 Rosetta）：
   build context 放 `addons/woow_paas_platform`（rsync worktree `src/`）＋ 8 個 CI 相依
   （`$PAAS_CI_ADDONS_PATH`：ai_base_gt / ecpay_invoice_tw / ecpay_invoice_website /
   odoo_ai_assistant_chatgpt_connector / payment_ecpay / payment_ecpay_ecpg /
   sh_subscription / sh_subscription_wallet），`COPY addons/ /mnt/extra-addons/`
   （chart 在 ci-local 下 `addons_path = /mnt/extra-addons`）。
   - `pip3 install --break-system-packages --no-cache-dir **--ignore-installed** -r req.txt`
     ——**少了 `--ignore-installed` 必炸**：`Cannot uninstall typing_extensions 4.10.0,
     RECORD file not found. Hint: The package was installed by debian`。副作用是會升
     cryptography → req.txt 要補 `pyopenssl`。
   - req.txt 拿掉 `fastembed`（拉 onnxruntime ~200MB），**同時**要
     `sed -i '/^fastembed/d' /mnt/extra-addons/woow_paas_platform/requirements.txt`
     ——`pre_init_hook._check_python_dependencies()` 會逐行驗 importable、缺的就現場 pip install。
   - 建置約 6 分鐘、image 約 2.2GB。

2. **匯進 node 的 containerd**（kind node 的 image store 與 Docker daemon **是兩套**，
   不匯就 `ErrImagePull` → `pull access denied, repository does not exist`）：
   ```bash
   docker save <tag> | docker exec -i desktop-control-plane ctr -n k8s.io images import -
   docker exec desktop-control-plane ctr -n k8s.io images ls | grep <tag>   # 驗
   ```
   chart 的 `imagePullPolicy` 是 `IfNotPresent`，匯完 kubelet 下一輪 backoff 就撿到
   （事件轉為 `already present on machine and can be accessed by the pod`）。約 1 分鐘。

3. `create.sh --context docker-desktop --mode ci-local --image <tag> --branch <slug>`
   ——其餘（install、credentials seed、TTL）走通用流程，不需改動。

**別用 plugin 的 `build.sh`**：它走本 repo 的 `Dockerfile.dev`，而該檔 `COPY extra/extra-addons/`
指向**不存在的目錄**（`extra/` 下只有 `cloudflare/`、`paas-operator/`），base 也缺上述 6 個相依。

**排錯教訓**：`docker build ... | tail -40` 會讓 Bash 的 exit code 變成 `tail` 的 0
——背景任務通知報 "exit code 0" 但實際 build 失敗。**看輸出本體，別信被管線洗過的 exit code。**

相關：[[reference_woow_playwright_e2e_gotchas]]、[[reference_k8s_sandbox_test_gotchas]]
