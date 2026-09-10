#!/bin/bash
# 突變測試（mutation testing）：故意破壞 bundle，確認 build-bundles.py --check 真的會紅。
#
# ⛔ 為什麼需要這個：驗收「全部 PASS」本身不代表驗收有效。本 repo 已經抓到三次「檢查看
#    起來綠、其實沒在驗」——例如斷鏈 symlink 之下 parse/validate 仍回 errors=[]（突變 A）、
#    CEL 拿掉 has() 護欄後缺欄位變成 fail-OPEN（突變 H）、以及突變 G 自己的 sed 因為目標
#    文字被改掉而變成 no-op 卻仍顯示 PASS。每加一項驗收，就要有一個對應的突變證明它有牙齒。
#
# 用法：bash .omnigent/mutation-tests.sh
#   每個 case 會把 .omnigent/ 複製到臨時目錄、在複本上施加突變、跑 --check、印出 FAIL 行。
#   ⭕ 版控內容一律不動（所有突變都只作用在臨時複本上）。
#
# 判讀：每一個 case 都**必須**出現至少一行 [FAIL]。某個 case 顯示「全部 PASS」＝那個突變
#   已經失效（通常是它比對的字面值被改過），⚠ 這比沒有測試更危險——請修突變本身。

set -u
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run_mut () {
  local label="$1"; shift
  local TD
  TD=$(mktemp -d "${TMPDIR:-/tmp}/omni-mut-XXXX")
  cp -a "$WT/.omnigent" "$TD/"
  ( cd "$TD/.omnigent" && eval "$@" ) >/dev/null 2>&1
  echo "### 突變：$label"
  ( cd "$TD/.omnigent" && python3 build-bundles.py --check 2>&1 \
      | grep -E '^\[FAIL\]|^總計' | sed 's/^/    /' )
  chmod -R u+rwX "$TD" 2>/dev/null || true   # 突變 N 會 chmod 000，否則刪不掉
  rm -rf "$TD"
  echo
}

# ── baseline：不施加任何突變，**必須全部 PASS** ────────────────────────────────
# ⚠ 這一項是突變測試自己的健全性檢查。基線若是紅的（例如某個檢查在臨時目錄環境下假紅），
#   每個 case 都會紅 ⇒ 整套突變測試失去鑑別力、比沒有更糟。實際踩過：第 12 項
#   （git index 無 symlink）在非 git repo 的臨時目錄裡 `git ls-files` 會失敗。
echo "### baseline（無突變，預期全部 PASS）"
BASE_TD=$(mktemp -d "${TMPDIR:-/tmp}/omni-base-XXXX")
cp -a "$WT/.omnigent" "$BASE_TD/"
( cd "$BASE_TD/.omnigent" && python3 build-bundles.py --check 2>&1 \
    | grep -E '^\[FAIL\]|^總計' | sed 's/^/    /' )
rm -rf "$BASE_TD"
echo

run_mut "A. 把 skills 實體副本換回 symlink（FIX ROUND 4 要擋的就是這個）" \
  'rm -rf woow-issue-leader/agents/repo_leader_cc/agents/scout_pi_via_cc/skills; ln -s ../../../../../_workers/scout_pi/skills woow-issue-leader/agents/repo_leader_cc/agents/scout_pi_via_cc/skills'

run_mut "B. thin shell 從上層 tools.agents 拿掉" \
  "sed -i '/- tracker_via_pi/d' woow-issue-leader/agents/repo_leader_pi/config.yaml"

run_mut "C. prompt 改寫成 instructions: <相對路徑>（靜默把路徑當 prompt）" \
  "rm woow-issue-leader/agents/repo_leader_cc/agents/ops_k8s_via_cc/AGENTS.md; sed -i 's|^skills: all|instructions: ../../../../_workers/ops_k8s/AGENTS.md\nskills: all|' woow-issue-leader/agents/repo_leader_cc/agents/ops_k8s_via_cc/config.yaml"

run_mut "D. agent name 重複（全樹唯一被破壞）" \
  "sed -i 's/^name: ver_pi_via_pi/name: ver_cc_via_pi/' woow-issue-leader/agents/repo_leader_pi/agents/ver_pi_via_pi/config.yaml"

run_mut "E. skill frontmatter name 用底線（違反 ^[a-z0-9-]+\$）" \
  "sed -i 's/^name: scout-pi-recon/name: scout_pi_recon/' _workers/scout_pi/skills/scout-pi-recon/SKILL.md"

run_mut "F. 內建 policy function.path 打錯字" \
  "sed -i 's/orchestration.read_only_os/orchestration.read_only_oss/' woow-issue-leader/agents/repo_leader_cc/agents/ver_cc_via_cc/config.yaml"

run_mut "G. P-tag 拿掉 refs/tags 與 v<版號> 形態（守線變弱）" \
  "python3 - <<'PY'
import pathlib
for p in pathlib.Path('.').rglob('config.yaml'):
    s = p.read_text()
    i = s.find('(--tags|--follow-tags|refs/tags/|')
    if i == -1: continue
    j = s.index(')', s.index('v[0-9]', i)) + 1
    p.write_text(s[:i] + '(--tags)' + s[j:])
PY"

run_mut "H. P-model 拿掉 has() 護欄（缺欄位 → fail-OPEN）" \
  "python3 - <<'PY'
import pathlib
old = '''&& !(has(event.data.arguments.args)
     && has(event.data.arguments.args.model)
     && event.data.arguments.args.model in'''
new = '''&& !(event.data.arguments.args.model in'''
for p in pathlib.Path('.').rglob('config.yaml'):
    s = p.read_text()
    if old.replace(chr(10), chr(10)+'            ') in s:
        p.write_text(s.replace(old.replace(chr(10), chr(10)+'            '), new))
PY"

run_mut "I. 不同目錄、frontmatter 同名（first-wins 吞噬）" \
  "mkdir -p _workers/scout_pi/skills/another-dir && printf -- '---\nname: researcher-upstream-addon\ndescription: x\n---\n\nx\n' > _workers/scout_pi/skills/another-dir/SKILL.md"

run_mut "J. P-approve 退回「且匹配工具名」的連言（覆核者的繞過復活）" \
  "python3 - <<'PY'
import pathlib
old = '&& event.data.arguments.command.matches(\"/approve\")'
new = old + chr(10) + '            && event.data.arguments.command.matches(\"\\\\\\\\btea\\\\\\\\b\")'
for p in pathlib.Path('.').rglob('config.yaml'):
    s = p.read_text()
    if old in s: p.write_text(s.replace(old, new))
PY"

run_mut "K. P-kubectl 拿掉串接偵測（保持 YAML 合法的語意弱化）" \
  "python3 - <<'PY'
import pathlib
old = 'event.data.arguments.command.matches(\"(&&|'
for p in pathlib.Path('.').rglob('config.yaml'):
    s = p.read_text()
    i = s.find(old)
    if i == -1: continue
    j = s.index(chr(10), i)
    # 用一個恆為 false 的條件取代串接偵測，YAML 結構不變
    s = s[:i] + 'event.data.arguments.command.matches(\"(?!x)x\")' + s[j:]
    p.write_text(s)
PY"

run_mut "L. config.yaml 手改壞（YAML 語法錯）" \
  "printf 'blast_radius:\\n  bad: [\\n' >> woow-issue-leader/agents/repo_leader_cc/agents/ops_k8s_via_cc/config.yaml"

run_mut "M. 一支 CEL 編譯失敗（是否遮蔽其餘運算式與向量）" \
  "python3 - <<'PY'
import pathlib
for p in pathlib.Path('.').rglob('config.yaml'):
    s = p.read_text()
    if 'p_approve_no_agent_signoff' in s:
        s = s.replace('event.data.arguments.command.matches(\"/approve\")',
                      'event.data.arguments.command.matches(\"/approve\"', 1)
        p.write_text(s)
PY"

run_mut "N. SKILL.md 讀不到（frontmatter helper 是否炸掉整輪）" \
  "chmod 000 _workers/tracker/skills/tracker-cross-repo-tea/SKILL.md"

run_mut "O. P-kubectl context 判斷退回不認引號（FIX ROUND 3 的回歸）" \
  "python3 - <<'PY'
import pathlib
for p in pathlib.Path('.').rglob('config.yaml'):
    s = p.read_text()
    if 'docker-desktop' in s:
        s = s.replace('--context(=|\\\\\\\\s+)[\\\\\"\\']?docker-desktop', '--context[=\\\\\\\\s]+docker-desktop')
        p.write_text(s)
PY"

run_mut "Q. 手改一份產生出來的 AGENTS.md 副本（實體副本方案的唯一守線）" \
  "printf '\\n手改的內容，來源池沒有這一行\\n' >> woow-issue-leader/agents/repo_leader_cc/agents/impl_cc_via_cc/AGENTS.md"

run_mut "R. 把 os_env.cwd 改回絕對路徑（deployed server 會拒收）" \
  "sed -i 's|^  cwd: \\.$|  cwd: /home/elmo/woow-repos|' woow-issue-leader/agents/repo_leader_cc/agents/impl_cc_via_cc/config.yaml"

run_mut "S. 刪掉一份 skills 副本（整批 skills 消失的量級縮小版）" \
  "rm -rf woow-issue-leader/agents/repo_leader_pi/agents/tracker_via_pi/skills"
