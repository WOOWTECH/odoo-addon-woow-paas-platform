#!/usr/bin/env bash
#
# woow_paas_platform — AI 模型分頁的沙盒身分（LLM-01b；DESIGN N14、§2.11）。
#
# 用法：bash .sandbox/llm-identity.sh <slug>   → 印出「<前綴> <block>」，例如「wsqk 17」。
#
# 多個 stg 沙盒共用一套 new-api-stg：每個沙盒要有自己的 new-api 使用者名稱前綴
# （"ws"＋2 個小寫字母，代理的 PROXY_USERNAME_REGEX 是 ^ws[a-z]{2}\d{10}$）與合成 IP block
# （1–31，代理的 PROXY_TENANT_IP_RANGE 只收 block 1–31），才不會撞名、不共用 new-api 的
# CT／GA 限流桶。兩者都由 slug 的 sha256 決定（同一個 slug 永遠得到同一組）。
#
# ⚠ 已知限制：
#   * 676 個前綴、31 個 block：兩個沙盒剛好同 block 時，限流隔離類驗收要改期（DESIGN §2.11）。
#   * 同一個 slug 銷毀後重建會得到同一個前綴；new-api 的使用者刪不掉（要 proof），舊沙盒開過的
#     wsXX0000000001 還在，新沙盒的第一個帳戶開戶會撞名、用新密碼登入失敗 ⇒ state=error。重建
#     LLM 沙盒請換 slug。
set -euo pipefail

slug="${1:?用法：llm-identity.sh <slug>}"
if command -v sha256sum >/dev/null 2>&1; then
  hex="$(printf '%s' "$slug" | sha256sum | cut -c1-8)"
else
  hex="$(printf '%s' "$slug" | shasum -a 256 | cut -c1-8)"
fi
n=$((16#$hex))
letters=abcdefghijklmnopqrstuvwxyz
first="${letters:$((n % 26)):1}"
second="${letters:$(((n / 26) % 26)):1}"
block=$(((n / 676) % 31 + 1))
printf 'ws%s%s %d\n' "$first" "$second" "$block"
