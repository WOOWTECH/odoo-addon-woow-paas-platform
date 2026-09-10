#!/bin/bash
# 突變測試（mutation testing）：故意破壞 bundle，確認 build-bundles.py --check 真的會紅，
# 而且**紅在它聲稱要驗的那個地方**。
#
# ⛔ 為什麼需要這個：驗收「全部 PASS」本身不代表驗收有效。本 repo 已抓到三次同類事故——
#    全部都是「只看有沒有 [FAIL]，不看紅在哪」造成的：
#      1. 突變 G：sed 目標字串不存在 → **no-op 卻顯示 PASS**（假綠）
#      2. 第 12 項：非 git repo 假紅 → **每個 case 都紅**，失去鑑別力
#      3. 突變 K：sed 在同一份 config 命中兩處，改壞的是 p_board_read_only →
#         **紅錯地方**（看起來通過，其實 kubectl 串接偵測完全沒被驗到）
#
# ⇒ 所以每個 case 都必須宣告**失敗指紋**，由本 script 機械斷言：
#      EXPECT_ITEMS   預期紅的驗收項號（逗號分隔）。實際紅的集合必須**完全相等**——
#                     多紅一項（誤傷）與少紅一項（守線失效）都算該 case 失敗。
#      EXPECT_POLICY  CEL 相關時，預期紅的 policy 名稱。它必須出現在 FAIL 訊息裡，
#                     這正是抓「紅錯 policy」的那道斷言。
#    另外無條件斷言：突變**確實改到檔案**（內容 hash 有變），否則是 no-op 假綠。
#
# 用法：bash .omnigent/mutation-tests.sh [case 代號…]
#   不帶參數＝全跑。每個 case 把 .omnigent/ 複製到臨時目錄後才施加突變，
#   ⭕ 版控內容一律不動。

set -u
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ONLY="${*:-}"
PASS_N=0; FAIL_N=0

# 抽出實際紅的驗收項號，正規化成排序後的逗號字串（例如 "0,8"）。
_fail_items () { grep -oE '^\[FAIL\] [0-9]+[a-z]?' | sed 's/^\[FAIL\] //' | sort -V -u | paste -sd, -; }

# ⚠ 必須同時納入**權限**：突變 N 只改 chmod、不改內容，純內容 hash 會把它誤判成 no-op。
#    md5sum 對讀不到的檔案會噴到 stderr（那正是 N 要製造的狀態）⇒ 丟掉 stderr。
_tree_hash () {
  { find "$1" -type f -not -path '*/__pycache__/*' -printf '%m %p\n' | sort
    find "$1" -type f -not -path '*/__pycache__/*' -exec md5sum {} + 2>/dev/null | sort -k2
  } | md5sum
}

run_mut () {
  local code="$1" label="$2" expect_items="$3" expect_policy="$4" mutation="$5"
  if [ -n "$ONLY" ] && ! grep -qw "$code" <<< "$ONLY"; then return 0; fi

  local TD; TD=$(mktemp -d "${TMPDIR:-/tmp}/omni-mut-XXXX")
  cp -a "$WT/.omnigent" "$TD/"
  local before; before=$(_tree_hash "$TD/.omnigent")
  ( cd "$TD/.omnigent" && eval "$mutation" ) >/dev/null 2>&1
  local after; after=$(_tree_hash "$TD/.omnigent")

  local out; out=$( cd "$TD/.omnigent" && python3 build-bundles.py --check 2>&1 )
  local actual_items; actual_items=$(printf '%s\n' "$out" | _fail_items)
  local problems=()

  # (1) 突變必須真的改到東西——否則是 no-op 假綠（突變 G 的歷史教訓）
  [ "$before" = "$after" ] && problems+=("突變未改到任何檔案（no-op，可能是定位字串已失效）")
  # (2) 紅的項號集合必須完全相等
  [ "$actual_items" != "$expect_items" ] \
    && problems+=("項號不符：預期 [$expect_items]，實際 [${actual_items:-無}]")
  # (3) CEL 相關：預期的 policy 名必須出現在 FAIL 訊息裡
  if [ -n "$expect_policy" ]; then
    printf '%s\n' "$out" | grep '^\[FAIL\]' | grep -q "$expect_policy" \
      || problems+=("FAIL 訊息裡找不到預期的 policy「$expect_policy」← 可能紅錯地方")
  fi

  if [ ${#problems[@]} -eq 0 ]; then
    PASS_N=$((PASS_N+1))
    printf '✓ %-2s %-58s 紅 [%s]%s\n' "$code" "$label" "$actual_items" \
      "${expect_policy:+ ← $expect_policy}"
  else
    FAIL_N=$((FAIL_N+1))
    printf '✗ %-2s %s\n' "$code" "$label"
    local pr; for pr in "${problems[@]}"; do printf '     ⚠ %s\n' "$pr"; done
    printf '%s\n' "$out" | grep '^\[FAIL\]' | sed 's/^/     /' | cut -c1-160
  fi
  chmod -R u+rwX "$TD" 2>/dev/null || true   # 突變 N 會 chmod 000，否則刪不掉
  rm -rf "$TD"
}

# ── baseline：不施加任何突變，必須全部 PASS ────────────────────────────────────
# ⚠ 突變測試自己的健全性檢查。基線若是紅的（例如某檢查在臨時目錄環境下假紅），
#   每個 case 都會紅 ⇒ 整套失去鑑別力、比沒有更糟。實際踩過：第 12 項在非 git repo 假紅。
if [ -z "$ONLY" ] || grep -qw baseline <<< "$ONLY"; then
  BASE_TD=$(mktemp -d "${TMPDIR:-/tmp}/omni-base-XXXX")
  cp -a "$WT/.omnigent" "$BASE_TD/"
  BASE_OUT=$( cd "$BASE_TD/.omnigent" && python3 build-bundles.py --check 2>&1 )
  BASE_FAILS=$(printf '%s\n' "$BASE_OUT" | _fail_items)
  if [ -z "$BASE_FAILS" ]; then
    PASS_N=$((PASS_N+1)); echo "✓ baseline（無突變）                                               全部 PASS"
  else
    FAIL_N=$((FAIL_N+1)); echo "✗ baseline（無突變）竟然有 FAIL [$BASE_FAILS] ← 其餘 case 的結果都不可信"
    printf '%s\n' "$BASE_OUT" | grep '^\[FAIL\]' | sed 's/^/     /'
  fi
  rm -rf "$BASE_TD"
fi

# ── 各 case ───────────────────────────────────────────────────────────────────
# run_mut <代號> <說明> <預期紅的項號> <預期紅的 CEL policy（選填）> <突變指令>
#
# ⚠ 突變一律用 python 寫（不用 sed）：sed 只能按字面定位，突變 K 的事故就是它在同一份
#   config 命中兩處、改壞了另一支 policy。python 可以先定位 policy block 再改。

run_mut A "把一份 skills 實體副本換回 symlink" "7,10" "" \
'python3 - <<"PY"
import pathlib, shutil, os
d = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/agents/scout_pi_via_cc/skills")
shutil.rmtree(d)
os.symlink("../../../../../_workers/scout_pi/skills", d)
PY'

run_mut B "thin shell 從上層 tools.agents 拿掉" "0,6" "" \
'python3 - <<"PY"
import pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_pi/config.yaml")
p.write_text("\n".join(l for l in p.read_text().splitlines(keepends=False)
                       if "- tracker_via_pi" not in l) + "\n")
PY'

run_mut C "prompt 改寫成 instructions: <相對路徑>（靜默把路徑當 prompt）" "0,3" "" \
'python3 - <<"PY"
import pathlib
d = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/agents/ops_k8s_via_cc")
(d / "AGENTS.md").unlink()
p = d / "config.yaml"
p.write_text(p.read_text().replace(
    "skills: all", "instructions: ../../../../_workers/ops_k8s/AGENTS.md\nskills: all", 1))
PY'

run_mut D "agent name 重複（全樹唯一被破壞）" "0,1,2,4,10" "" \
'python3 - <<"PY"
import pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_pi/agents/ver_pi_via_pi/config.yaml")
p.write_text(p.read_text().replace("name: ver_pi_via_pi", "name: ver_cc_via_pi", 1))
PY'

# ⚠ 預期不含第 0 項：本突變同時改來源池與副本 ⇒ 兩邊一致、沒有 drift。
#   含第 10 項：不合法的 skill name 連 server 端 validate 也會拒（原本漏推導）。
run_mut E "skill frontmatter name 用底線（違反 ^[a-z0-9-]+\$）" "1,2,5b,10" "" \
'python3 - <<"PY"
import pathlib
for p in pathlib.Path(".").rglob("scout-pi-recon/SKILL.md"):
    p.write_text(p.read_text().replace("name: scout-pi-recon", "name: scout_pi_recon", 1))
PY'

run_mut F "內建 policy function.path 打錯字" "0,9,10" "" \
'python3 - <<"PY"
import pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/agents/ver_cc_via_cc/config.yaml")
p.write_text(p.read_text().replace("orchestration.read_only_os", "orchestration.read_only_oss"))
PY'

run_mut G "P-tag 拿掉 refs/tags 與 v<版號> 形態（守線變弱）" "0,8" "p_tag_no_agent_tag_push" \
'python3 - <<"PY"
import pathlib
needle = "(--tags|--follow-tags|refs/tags/|"
for p in pathlib.Path(".").rglob("config.yaml"):
    s = p.read_text()
    i = s.find(needle)
    if i == -1: continue
    j = s.index(")", s.index("v[0-9]", i)) + 1
    p.write_text(s[:i] + "(--tags)" + s[j:])
PY'

run_mut H "P-model 拿掉 has() 護欄（缺欄位 → fail-OPEN）" "0,8" "p_model_fast_tier_only" \
'python3 - <<"PY"
import pathlib
old = """&& !(has(event.data.arguments.args)
                 && has(event.data.arguments.args.model)
                 && event.data.arguments.args.model in"""
new = """&& !(event.data.arguments.args.model in"""
for p in pathlib.Path(".").rglob("config.yaml"):
    s = p.read_text()
    if old in s:
        p.write_text(s.replace(old, new))
PY'

# ⚠ 刻意**同時**加到來源池與所有副本：否則第 0 項（drift）與第 4 項（skills 數不符）
#   也會紅，焦點就從「frontmatter 同名查重」模糊掉了。這個 case 要單獨驗 5b。
run_mut I "不同目錄、frontmatter 同名（first-wins 吞噬）" "5b" "" \
'python3 - <<"PY"
import pathlib
body = ("---\nname: researcher-upstream-addon\n"
        "description: 故意與 researcher 的 skill 同名（不同目錄）\n---\n\nx\n")
roots = [pathlib.Path("_workers/scout_pi/skills")]
roots += [q for q in pathlib.Path(".").rglob("scout_pi_via_*/skills")]
for r in roots:
    d = r / "another-dir"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)
PY'

run_mut J "P-approve 退回「且匹配工具名」的連言（覆核者的繞過復活）" "0,8" "p_approve_no_agent_signoff" \
'python3 - <<"PY"
import pathlib
old = chr(38)*2 + """ event.data.arguments.command.matches("/approve")"""
new = old + "\n            " + old.lstrip()
for p in pathlib.Path(".").rglob("config.yaml"):
    s = p.read_text()
    if old in s:
        p.write_text(s.replace(old, old + "\n            " + chr(38)*2
                               + """ event.data.arguments.command.matches("\\\\btea\\\\b")""", 1))
PY'

run_mut K "P-kubectl 拿掉串接偵測（精準定位該 policy block）" "0,8" "p_kubectl_namespace_scope" \
'python3 - <<"PY"
import pathlib
# ⚠ 先定位 p_kubectl_namespace_scope 的 YAML block 再改——SHELL_CHAIN_RE 同時被
#   p_board_read_only 用到，按字面找第一個命中會改壞 board policy（歷史事故）。
needle = """event.data.arguments.command.matches("(""" + chr(38)*2 + "|"
for p in pathlib.Path(".").rglob("config.yaml"):
    s = p.read_text()
    i = s.find("p_kubectl_namespace_scope:")
    if i == -1: continue
    j = s.find("\n    p_", i + 1)
    if j == -1: j = len(s)
    block = s[i:j]
    k = block.find(needle)
    if k == -1: continue
    end = block.index("\n", k)
    block = block[:k] + """event.data.arguments.command.matches("(?!x)x")""" + block[end:]
    p.write_text(s[:i] + block + s[j:])
PY'

run_mut L "config.yaml 手改壞（YAML 語法錯）" "0,1" "" \
'python3 - <<"PY"
import pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/agents/ops_k8s_via_cc/config.yaml")
p.write_text(p.read_text() + "\nblast_radius:\n  bad: [\n")
PY'

run_mut M "一支 CEL 編譯失敗（是否遮蔽其餘運算式與向量）" "0,8" "p_approve_no_agent_signoff" \
'python3 - <<"PY"
import pathlib
old = """event.data.arguments.command.matches("/approve")"""
for p in pathlib.Path(".").rglob("config.yaml"):
    s = p.read_text()
    if "p_approve_no_agent_signoff" in s and old in s:
        p.write_text(s.replace(old, old[:-1], 1))   # 少一個右括號 → compile error
PY'

# ⚠ chmod 的是**副本**不是來源池：parse 讀的是 thin shell 裡的副本，改來源池的權限
#   第 1 項不會紅（這是 symlink 時代留下的錯誤假設，FIX ROUND 5 修正）。
#   驗的是「讀檔失敗被 r.guard 轉成 FAIL，不是 traceback 中斷整輪」。
run_mut N "SKILL.md 讀不到（驗例外被轉成 FAIL 而非中斷整輪）" "0,1" "" \
'chmod 000 woow-issue-leader/agents/repo_leader_cc/agents/tracker_via_cc/skills/tracker-cross-repo-tea/SKILL.md'

run_mut O "P-kubectl context 判斷退回不認引號（FIX ROUND 3 的回歸）" "0,8" "p_kubectl_namespace_scope" \
'python3 - <<"PY"
import pathlib
for p in pathlib.Path(".").rglob("config.yaml"):
    s = p.read_text()
    if "docker-desktop" not in s: continue
    p.write_text(s.replace("""--context(=|\\\\s+)[\\"\x27]?docker-desktop""",
                           "--context[=\\\\s]+docker-desktop"))
PY'

run_mut Q "手改一份產生出來的 AGENTS.md 副本（實體副本方案的唯一守線）" "0" "" \
'python3 - <<"PY"
import pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/agents/impl_cc_via_cc/AGENTS.md")
p.write_text(p.read_text() + "\n手改的內容，來源池沒有這一行\n")
PY'

run_mut R "把 os_env.cwd 改回絕對路徑（deployed server 會拒收）" "0,11" "" \
'python3 - <<"PY"
import pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/agents/impl_cc_via_cc/config.yaml")
p.write_text(p.read_text().replace("  cwd: .", "  cwd: /home/elmo/woow-repos", 1))
PY'

run_mut S "刪掉一份 skills 副本（整批 skills 消失的量級縮小版）" "0,4" "" \
'rm -rf woow-issue-leader/agents/repo_leader_pi/agents/tracker_via_pi/skills'

echo
if [ "$FAIL_N" -eq 0 ]; then
  echo "總計：$PASS_N 個 case 全部符合預期指紋"
else
  echo "總計：$FAIL_N 個 case 的失敗指紋不符（共 $((PASS_N+FAIL_N)) 個）"
  echo "⚠ 指紋不符＝那個 case 沒在驗它聲稱要驗的東西，必須修 case 本身。"
fi
exit $(( FAIL_N > 0 ? 1 : 0 ))
