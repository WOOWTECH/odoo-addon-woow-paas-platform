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
#      定位斷言       改檔的突變一律走 `mutation_lib.edit()` / `edit_span()`，它會在**套用前**
#                     斷言定位字串仍然有效（命中 0 次、或命中多處卻沒給錨點 ⇒ fail loud）。
#                     ⛔ 不要回頭寫 `s.replace(old, new, 1)`——那正是三次靜默 no-op 的來源。
#      EXPECT_KEY     FAIL 訊息裡**必須出現**的關鍵字。CEL 相關時放 policy 名（抓「紅錯
#                     policy」）；dispatcher 相關時放情境名（抓「紅錯情境」）。這一欄是
#                     項號比對抓不到的那一層：突變 K 的舊寫法項號與預期完全相同，只有
#                     policy 名能揭露它其實紅在別支 policy 上。
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
  local code="$1" label="$2" expect_items="$3" expect_key="$4" mutation="$5"
  if [ -n "$ONLY" ] && ! grep -qw "$code" <<< "$ONLY"; then return 0; fi

  local TD; TD=$(mktemp -d "${TMPDIR:-/tmp}/omni-mut-XXXX")
  cp -a "$WT/.omnigent" "$TD/"
  local before; before=$(_tree_hash "$TD/.omnigent")
  # ⚠ 突變的 stderr **不可以吞掉**：mutation_lib 的定位斷言是靠 raise 表達的，吞掉就退回成
  #   「靜默 no-op」——正是這道 gate 要修的問題。MUT_CODE 讓錯誤訊息能說出是哪個突變。
  local mut_err; mut_err=$( cd "$TD/.omnigent" && MUT_CODE="$code" eval "$mutation" 2>&1 >/dev/null )
  local mut_rc=$?
  local after; after=$(_tree_hash "$TD/.omnigent")

  local problems=()
  # (0) 🔴 套用前的定位斷言（mutation_lib）失敗 ⇒ 這個 case 直接判死，不必再跑驗收。
  #     這層抓的是「定位字串失效／命中多處沒給錨點」，而它的症狀原本是靜默 no-op。
  if [ "$mut_rc" -ne 0 ]; then
    FAIL_N=$((FAIL_N+1))
    printf '✗ %-2s %s\n' "$code" "$label"
    printf '     ⚠ 突變套用失敗（定位斷言不通過，尚未改到任何檔案）\n'
    # 從 Error 那行起全部保留——訊息的後續行（實際字串、提示）才是能修的資訊；
    # ⛔ 不要用 `grep -v '^    '` 去掉 traceback，那會連訊息本體一起砍掉。
    printf '%s\n' "$mut_err" | awk '/Error: /{f=1} f' | sed 's/^/       /'
    chmod -R u+rwX "$TD" 2>/dev/null || true
    rm -rf "$TD"
    return 0
  fi

  local out; out=$( cd "$TD/.omnigent" && python3 build-bundles.py --check 2>&1 )
  local actual_items; actual_items=$(printf '%s\n' "$out" | _fail_items)

  # (1) 突變必須真的改到東西。⚠ 保留這層：它抓的是「有套用、但內容／權限沒有實際差異」
  #     （例如突變 N 只 chmod），與 (0) 的定位斷言管不同的事。
  [ "$before" = "$after" ] && problems+=("突變未改到任何檔案（no-op，可能是定位字串已失效）")
  # (2) 紅的項號集合必須完全相等
  [ "$actual_items" != "$expect_items" ] \
    && problems+=("項號不符：預期 [$expect_items]，實際 [${actual_items:-無}]")
  # (3) CEL 相關：預期的 policy 名必須出現在 FAIL 訊息裡
  if [ -n "$expect_key" ]; then
    printf '%s\n' "$out" | grep '^\[FAIL\]' | grep -qF "$expect_key" \
      || problems+=("FAIL 訊息裡找不到預期的關鍵字「$expect_key」← 可能紅錯地方")
  fi

  if [ ${#problems[@]} -eq 0 ]; then
    PASS_N=$((PASS_N+1))
    printf '✓ %-2s %-58s 紅 [%s]%s\n' "$code" "$label" "$actual_items" \
      "${expect_key:+ ← $expect_key}"
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
# run_mut <代號> <說明> <預期紅的項號> <FAIL 訊息必須含的關鍵字（選填）> <突變指令>
#
# ⚠ 突變一律用 python 寫（不用 sed）：sed 只能按字面定位，突變 K 的歷史事故就是它在同一份
#   config 命中兩處、改壞了另一支 policy ⇒ 紅錯地方。
#
# ⚠ 改 `generate-worker.py` 裡 CEL 的 case 末尾都要 `python3 build-bundles.py` 重產：
#   第 8 項是從**已 parse 的樹**抽運算式，靜態 config 沒重產就還是舊的 CEL ⇒ 驗不到。
#
# ⚠ 改 dispatcher **來源**的 case 末尾都要 `python3 build-bundles.py` 重產：否則副本沒跟著
#   變，會連帶紅第 0（產物不同步）與第 16（兩份漂移）項，把焦點從「dispatcher 邏輯變弱」
#   模糊掉。要驗「兩份漂移」本身，用專門的 case AB。
#   （動態 worker 的部分會立刻反映，但 p_model／p_board 只掛在 leader 的靜態 config 上。）

run_mut A "把 repo-leader 的 skills 副本換成 symlink" "7,10" "" \
'python3 - <<"PY"
import pathlib, shutil, os
d = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/skills")
shutil.rmtree(d)
os.symlink("../../skills", d)
PY'

run_mut B "issue-leader 的 tools.agents 少列一個 repo-leader" "0,6" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/config.yaml")
p.write_text("\n".join(l for l in p.read_text().splitlines()
                       if "- repo_leader_pi" not in l) + "\n")
PY'

run_mut C "把 worker 名字加回 tools.agents（退回舊架構的徵兆）" "0,1,2,6,10" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
# ⚠ 用換行界定：`spawn: true` 在這份 config 裡有兩處——註解裡一處、真正的指令一處。
#   不界定的話會改到註解那處（FIX ROUND 9 的定位斷言抓到的）。
mutation_lib.edit("woow-issue-leader/agents/repo_leader_cc/config.yaml",
                  old="\nspawn: true\n",
                  new="\nspawn: true\ntools:\n  agents:\n    - implement_cc\n")
PY'

run_mut D "repo-leader 的 prompt 改成 instructions: <相對路徑>" "0,3" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
d = pathlib.Path("woow-issue-leader/agents/repo_leader_cc")
(d / "AGENTS.md").unlink()
mutation_lib.edit(str(d / "config.yaml"), old="skills: all",
                  new="instructions: ../../../_shared/repo_leader/AGENTS.md\nskills: all")
PY'

run_mut E "模板的 skill frontmatter name 用底線" "4,5b,8" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
for p in pathlib.Path("woow-issue-leader/_templates").rglob("scout-pi-recon/SKILL.md"):
    mutation_lib.edit(str(p), old="name: scout-pi-recon", new="name: scout_pi_recon")
PY'

run_mut F "內建 policy function.path 打錯字" "9" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
mutation_lib.edit(str(p), old="orchestration.read_only_os",
                  new="orchestration.read_only_oss")
PY'

run_mut G "P-tag 拿掉 refs/tags 與 v<版號> 形態（守線變弱）" "8" "p_tag_no_agent_tag_push" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
# 從 alternation 開頭一路換到 `v[0-9])` 結尾（整段砍成只剩 --tags）
mutation_lib.edit_span(str(p), start="(--tags|--follow-tags|refs/tags/|",
                       through="v[0-9])", new="(--tags)")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut H "P-model 拿掉 has() 護欄（缺欄位 → fail-OPEN）" "8" "p_model_fast_tier_only" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
old = """&& !(has(event.data.arguments.args)
     && has(event.data.arguments.args.model)
     && event.data.arguments.args.model in"""
mutation_lib.edit(str(p), old=old, new="&& !(event.data.arguments.args.model in")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut I "模板裡不同目錄、frontmatter 同名（first-wins 吞噬）" "5b" "" \
'python3 - <<"PY"
import pathlib
body = ("---\nname: researcher-upstream-addon\n"
        "description: 故意與 researcher 的 skill 同名（不同目錄）\n---\n\nx\n")
d = pathlib.Path("woow-issue-leader/_templates/scout_pi/skills/another-dir")
d.mkdir(parents=True, exist_ok=True)
(d / "SKILL.md").write_text(body)
PY'

run_mut J "P-approve 退回「且匹配工具名」的連言" "8" "p_approve_no_agent_signoff" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
old = chr(38)*2 + """ event.data.arguments.command.matches("/approve")"""
mutation_lib.edit(str(p), old=old, new=old + "\n" + chr(38)*2
                  + """ event.data.arguments.command.matches("\\\\btea\\\\b")""")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut K "P-kubectl 拿掉串接偵測（精準定位該 policy block）" "8" "p_kubectl_namespace_scope" \
'python3 - <<"PY"
import mutation_lib
import pathlib, subprocess
# ⚠ SHELL_CHAIN_RE 同時被 p_board_read_only 用到 ⇒ 不能改它的定義。
#   kubectl 那一行是 `|| event…`，board 那一行是 `&& !event…` ⇒ 用前綴唯一定位。
p = pathlib.Path("woow-issue-leader/generate-worker.py")
q = chr(34)
needle = "|| event.data.arguments.command.matches(" + q + "{SHELL_CHAIN_RE}" + q + ")"
repl = "|| event.data.arguments.command.matches(" + q + "(?!x)x" + q + ")"
mutation_lib.edit(str(p), old=needle, new=repl)
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'



run_mut L "config.yaml 手改壞（YAML 語法錯）" "0,1" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/config.yaml")
p.write_text(p.read_text() + "\nblast_radius:\n  bad: [\n")
PY'

run_mut M "一支 CEL 編譯失敗（是否遮蔽其餘運算式與向量）" "8" "p_approve_no_agent_signoff" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
old = """event.data.arguments.command.matches("/approve")"""
# 少一個右括號 → compile error
mutation_lib.edit(str(p), old=old, new=old[:-1])
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut N "模板的 SKILL.md 讀不到（驗例外被轉成 FAIL 而非中斷整輪）" "2,4,5b,8,10,13" "" \
'chmod 000 woow-issue-leader/_templates/tracker/skills/tracker-cross-repo-tea/SKILL.md'

run_mut O "P-kubectl context 判斷退回不認引號" "8" "p_kubectl_namespace_scope" \
'python3 - <<"PY"
import mutation_lib
import pathlib, subprocess
# _CTX 的 ["\x27]? 是「容許引號」那一段；拿掉它就退回 FIX ROUND 3 之前的行為。
p = pathlib.Path("woow-issue-leader/generate-worker.py")
# ⚠ 直接定位 `_CTX` 的定義行：`[\"\x27]?` 這個片段在 namespace matcher 那行也有兩處，
#   只給片段會改到那裡 ⇒ 變成在驗 namespace 的引號容許度，不是 context 的
#   （FIX ROUND 9 的定位斷言抓到的）。
old = "_CTX = r" + chr(34) + "--context(=|" + chr(92)*2 + "s+)[" + chr(92) + chr(34) + chr(39) + "]?" + chr(34)
mutation_lib.edit(str(p), old=old,
                  new="_CTX = r" + chr(34) + "--context(=|" + chr(92)*2 + "s+)" + chr(34))
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'


run_mut Q "手改 repo-leader 的 AGENTS.md 副本（產物 drift）" "0" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/AGENTS.md")
p.write_text(p.read_text() + "\n手改的內容，來源池沒有這一行\n")
PY'

# ⚠ 第 10 項也紅是對的：絕對 cwd 會讓 deployed server（enforce=True）拒收整個 bundle
#   ⇒ 這件事同時被兩道檢查守住（第 10 項模擬上傳、第 11 項掃全樹）。
run_mut R "把 os_env.cwd 改回絕對路徑（deployed server 會拒收）" "10,11" "" \
'python3 - <<"PY"
import mutation_lib, pathlib, subprocess
p = pathlib.Path("woow-issue-leader/generate-worker.py")
mutation_lib.edit(str(p), old="  cwd: .", new="  cwd: /home/elmo/woow-repos")
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'


run_mut S "刪掉一份模板的 skills（整批 skills 消失）" "4,8" "" \
'rm -rf woow-issue-leader/_templates/tracker/skills'

# ── 本輪新增（§八 要求）────────────────────────────────────────────────────────

run_mut T "hooks.json 改成靠 shebang（bundle 打包會掉 exec bit）" "15" "" \
'python3 - <<"PY"
import pathlib, json
for p in pathlib.Path(".").rglob("hooks/hooks.json"):
    cfg = json.loads(p.read_text())
    for groups in cfg["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                # 拿掉 python3 前綴，改成直接執行（靠 shebang）
                h["command"] = h["command"].replace("python3 ", "", 1)
    p.write_text(json.dumps(cfg, indent=2))
PY'

run_mut U "worker config 設 skills_filter: none（壓掉 dispatcher 與所有 repo hooks）" "4,8" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
q = chr(34)
mutation_lib.edit(str(p), old=q + "skills: all" + q, new=q + "skills: none" + q)
PY'


run_mut V "產生器拿掉防線 1（harness × model 家族檢查）" "4" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/generate-worker.py")
# ⚠ 只比對到呼叫本身，不含行尾註解——ROUND 7 的定位字串寫了一個**從來不存在**的
#   `# <- DEFENCE1` 註解，靠一個 fallback 才矇對（FIX ROUND 9 的定位斷言抓到的）。
#   行尾註解會隨文案調整而變，⛔ 不要拿它當定位依據（同 ROUND 7 突變 Y 的教訓）。
mutation_lib.edit(str(p), old="    _check_model_family(harness, model)",
                  new="    pass  # 防線 1 被拿掉  #")
PY'


run_mut W "dispatcher 不支援 PostToolUse（repo 有 2 支 PostToolUse hook）" "0,15" "" \
'python3 - <<"PY"
import pathlib, json
p = pathlib.Path("woow-issue-leader/hooks/hooks.json")
cfg = json.loads(p.read_text())
cfg["hooks"].pop("PostToolUse")
p.write_text(json.dumps(cfg, indent=2))
PY'

run_mut X "dispatcher 不覆寫 CLAUDE_PROJECT_DIR（repo hook 找不到檔）" "14" "" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
old = """                env[\"CLAUDE_PROJECT_DIR\"] = str(repo_root)"""
mutation_lib.edit(str(p), old=old, new="                pass  # 不覆寫")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'


run_mut Y "dispatcher 拿掉 path-token 解析（cd ../repoA 會零命中）" "14" "cd ../repoA" \
'python3 - <<"PY"
import mutation_lib
import pathlib
# 退回 FIX ROUND 6 之前：只有 A（cwd）與 B（字串比對）兩層，沒有 C（path-token）。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")

# 定位 path-token 命中那一行（⚠ 不要比對行尾註解的層號字母——FIX ROUND 7 重編層號時
# 它從 C 變成 B，舊定位就靜默失效成 no-op 了）。
old = "if any(c == repo or repo in c.parents for c in candidates):"
mutation_lib.edit(str(p), old=old, new="if False:  # path-token 命中判斷被拿掉")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

# ── FIX ROUND 7：原本的突變 Z 只錐在「timeout」──────────────────────────────────
# 🔴 所以三個 fail-open 漏洞（非-timeout 失敗、settings.json 解析失敗）存在時，26 個
#    case 仍然全綠：**測試的盲點跟程式的盲點一模一樣**。拆成三個各自錐不同失敗形態的
#    case，每個都用 EXPECT_KEY 斷言紅在對應的場景名上。

run_mut Z1 "PreToolUse 的 timeout 改回 fail-open" "14" "PreToolUse hook timeout" \
'python3 - <<"PY"
import mutation_lib, pathlib
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
# ⚠ 定位字串必須包到第三行的 f-string：「if event != "PreToolUse":」本身全檔有兩份
#   （_hook_failure 與 _untrusted 各一），只有接在後面那行訊息是 _hook_failure 獨有的。
#   曾經靠 anchor=「縮範圍」解，但「用縮排推區塊邊界」連補三輪每次都冒新漏，
#   FIX ROUND 12 已拆除；現在一律把定位字串加長到全域唯一（詳見 mutation_lib.py 檔頭）。
old = """    if event != "PreToolUse":
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {event} hook {kind}（工具已執行完，不擋）："
"""
new = """    if True:  # 退回 fail-open
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {event} hook {kind}（工具已執行完，不擋）："
"""
mutation_lib.edit(str(p), old=old, new=new)
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut Z2 "PreToolUse 的非-timeout 失敗（exit 7）改回 fail-open" "14" "hook exit 7" \
'python3 - <<"PY"
import mutation_lib
import pathlib
# 退回 FIX ROUND 6：非 0 的 exit code 原樣回傳。⚠ 那等於 fail-open——hook 協定裡
# 只有 exit 2 代表 block，exit 7 會被視為「非阻斷性錯誤」而照跑工具。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
old = """                if proc.returncode != 0:
                    rc = _hook_failure("""
new = """                if proc.returncode != 0:
                    return proc.returncode
                if False:
                    rc = _hook_failure("""
mutation_lib.edit(str(p), old=old, new=new)
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut Z3 "settings.json 解析失敗改回 fail-open" "14" "settings.json 解析失敗" \
'python3 - <<"PY"
import mutation_lib
import pathlib
# 退回 FIX ROUND 6：讀不到／解不開就 continue ⇒ 宣稱有守線却載不起來，卻照樣放行。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
old = """        return None, _hook_failure(event, repo, str(path),
                                   "settings.json 載不起來", str(exc))"""
mutation_lib.edit(str(p), old=old, new="        return None, 0  # 退回 fail-open")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut Z4 "repo hook 刻意的 exit 2 被我們的診斷訊息包裝掉" "14" "包裝掉" \
'python3 - <<"PY"
import mutation_lib
import pathlib
# exit 2 是 repo hook 刻意的 block，它的 stderr 就是給使用者看的理由。
# 拿掉原樣透傳那條 ⇒ 會掉進「非預期 exit code」而被我們的訊息蓋掉。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
old = """                if proc.returncode == BLOCK_EXIT_CODE:
                    return BLOCK_EXIT_CODE"""
mutation_lib.edit(str(p), old=old, new="                pass  # 原樣透傳被拿掉")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AA "targets() 退回裸字串 regex（commit message 提到 repo 名就誤命中）" \
  "14" "git commit" \
'python3 - <<"PY"
import mutation_lib
import pathlib
# 退回 FIX ROUND 6：把 _loose_match 從「只在解析失敗時 fallback」變成常態判定。
# ⚠ 那層把 repo.name 當裸字串比對 ⇒ `git commit -m "fix repoA bug"` 誤命中，
#   而誤命中與 fail-closed 相乘會擋掉一條完全合法的 git commit。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
old = "        if degraded and _loose_match(repo, text):                      # fallback"
mutation_lib.edit(str(p), old=old,
                  new="        if _loose_match(repo, text):  # 退回常態裸字串比對")
import subprocess; subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AB "兩份 dispatcher 漂移（手改其中一份）" "0,16" "漂移" \
'python3 - <<"PY"
import pathlib
# ⚠ 一份補了守線、一份沒補 ⇒ 守線只在其中一條派工路徑生效，而且不會報錯（靜默失效）。
p = pathlib.Path("woow-issue-leader/agents/repo_leader_cc/hooks/dispatch-hook.py")
p.write_text(p.read_text() + "\n# 手改的副本，與來源漂移了\n")
PY'


run_mut AC "dispatcher 自己拿不到判斷時改回 rc=0（fail-open）" "14" "CLAUDE_PROJECT_DIR 未設" \
'python3 - <<"PY"
import mutation_lib
import pathlib, subprocess
# 退回 FIX ROUND 7：dispatcher 自己壞掉就 return 0。⚠ 那比 repo hook 失敗更危險——
# CLAUDE_PROJECT_DIR 沒設時 targets() 回空 ⇒ 所有 repo 的守線一起靜默消失。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
# ⚠ 同 Z1：前兩行在 _hook_failure 裡也有一份，必須包到第三行那句 _untrusted 獨有的
#   訊息才全域唯一（anchor 縮範圍機制已於 FIX ROUND 12 拆除）。
old = """    if event != "PreToolUse":
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {event}：dispatcher 自己拿不到判斷（{reason}），"
"""
new = """    if True:  # 退回 fail-open
        sys.stderr.write(
            f"[dispatch-hook] ⚠ {event}：dispatcher 自己拿不到判斷（{reason}），"
"""
mutation_lib.edit(str(p), old=old, new=new)
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AD "緊急逆轉門改成預設開啟" "14" "hook exit 7" \
'python3 - <<"PY"
import mutation_lib
import pathlib, subprocess
# 逆轉門預設開啟 ⇒ 等於所有守線預設關閉，而且只會留一行 stderr 警告。
p = pathlib.Path("woow-issue-leader/hooks/dispatch-hook.py")
old = "def _bypassed(text: str = \"\") -> bool:"
mutation_lib.edit(str(p), old=old, new=old + "\n    return True  # 預設開啟")
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AE "頂層 leader 的 harness 改回 claude-native（UI picker 選不到）" "17" "Custom agents" \
'python3 - <<"PY"
import mutation_lib
import pathlib, subprocess
# 真實事故的重演：改回 *-native，Omnigent Web UI 的 Custom agents 選單就**選不到**
# woow_issue_leader，而且完全不報錯（useAvailableAgents.ts 的 enrich 自我剔除）。
# ⚠ 突變改的是**產生器**並立刻重產，所以第 0 項（產物同步）不該紅——紅點必須單獨落在
#   第 17 項，否則就是「紅錯地方」（開頭歷史事故 3 的形態）。
p = pathlib.Path("build-bundles.py")
old = "        _executor(\"claude-sdk\", \"auto\", model=model, effort=effort),"
new = "        _executor(\"claude-native\", \"auto\", model=model, effort=effort),"
mutation_lib.edit(str(p), old=old, new=new)
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AF "P-merge 退回寬鬆版（PR 標題含 merge 被誤擋）" "8" "p_merge_no_agent_merge" \
'python3 - <<"PY"
import mutation_lib, pathlib, subprocess
p = pathlib.Path("woow-issue-leader/generate-worker.py")
b = chr(92) * 4
old = "(tea|gh|glab)" + b + "s+(pulls?|pr|mr)" + b + "s+merge"
new = "(tea|gh|glab)" + b + "b[" + b + "s" + b + "S]*" + b + "bmerge"
mutation_lib.edit(str(p), old=old, new=new)
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AG "P-merge 拿掉 CLI 的 merge 子命令路徑" "8" "p_merge_no_agent_merge" \
'python3 - <<"PY"
import mutation_lib, pathlib, subprocess
p = pathlib.Path("woow-issue-leader/generate-worker.py")
b = chr(92) * 4
old = b + "b(tea|gh|glab)" + b + "s+(pulls?|pr|mr)" + b + "s+merge" + b + "b|"
mutation_lib.edit(str(p), old=old, new="(?!x)x|")
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

run_mut AH "P-merge 拿掉 forge API 的 merge endpoint" "8" "p_merge_no_agent_merge" \
'python3 - <<"PY"
import mutation_lib, pathlib, subprocess
p = pathlib.Path("woow-issue-leader/generate-worker.py")
b = chr(92) * 4
old = b + "b(gh|glab)" + b + "s+api" + b + "b[" + b + "s" + b + "S]*/merge" + b + "b|"
mutation_lib.edit(str(p), old=old, new="(?!x)x|")
subprocess.run(["python3", "build-bundles.py"], check=True, capture_output=True)
PY'

echo
if [ "$FAIL_N" -eq 0 ]; then
  echo "總計：$PASS_N 個 case 全部符合預期指紋"
else
  echo "總計：$FAIL_N 個 case 的失敗指紋不符（共 $((PASS_N+FAIL_N)) 個）"
  echo "⚠ 指紋不符＝那個 case 沒在驗它聲稱要驗的東西，必須修 case 本身。"
fi
exit $(( FAIL_N > 0 ? 1 : 0 ))
