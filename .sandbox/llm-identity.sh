#!/usr/bin/env bash
#
# woow_paas_platform — AI 模型分頁的沙盒身分（LLM-01b；DESIGN N14、§2.11）。
#
# 用法：bash .sandbox/llm-identity.sh <slug> [salt]   → 印出「<前綴> <block>」，例如「wsqk 17」。
#
# 多個 stg 沙盒共用一套 new-api-stg：每個沙盒要有自己的 new-api 使用者名稱前綴
# （"ws"＋2 個小寫字母，代理的 PROXY_USERNAME_REGEX 是 ^ws[a-z]{2}\d{10}$）與合成 IP block
# （1–31，代理的 PROXY_TENANT_IP_RANGE 只收 block 1–31），才不會撞名、不共用 new-api 的
# CT／GA 限流桶。兩者都由 slug（＋salt）的 sha256 決定：同一組輸入永遠得到同一組輸出。
#
# salt：0（預設）＝只看 slug；1、2、…＝看「<slug>#<salt>」。只有 676 個前綴，而 new-api-stg 的
# 使用者刪不掉——不同 slug 也可能撞到同一個前綴（約 30 個 LLM 沙盒後撞到的機率約一半），同一個
# slug 銷毀後重建更一定撞。所以 post-create 不直接用 salt 0，而是從 0 往上試，挑第一個
#   (1) 能在 paas-stg-system 原子登記（ConfigMap woow-llm-sandbox-prefix-<前綴>，已存在＝別人用過）、
#   (2) new-api-stg 上還沒有 <前綴>0000000001（登記表之前的舊沙盒）的前綴（.sandbox/post-create.sh）。
#
# ⚠ 31 個 block：兩個沙盒剛好同 block 時，限流隔離類驗收要改期（DESIGN §2.11）。
set -euo pipefail

slug="${1:?用法：llm-identity.sh <slug> [salt]}"
salt="${2:-0}"
if ! [[ "$salt" =~ ^(0|[1-9][0-9]{0,2})$ ]]; then
  echo "llm-identity.sh：salt 必須是 0–999 的整數（收到 '${salt}'）" >&2
  exit 2
fi
seed="$slug"
[ "$salt" = 0 ] || seed="${slug}#${salt}"
if command -v sha256sum >/dev/null 2>&1; then
  hex="$(printf '%s' "$seed" | sha256sum | cut -c1-8)"
else
  hex="$(printf '%s' "$seed" | shasum -a 256 | cut -c1-8)"
fi
n=$((16#$hex))
letters=abcdefghijklmnopqrstuvwxyz
first="${letters:$((n % 26)):1}"
second="${letters:$(((n / 26) % 26)):1}"
block=$(((n / 676) % 31 + 1))
printf 'ws%s%s %d\n' "$first" "$second" "$block"
