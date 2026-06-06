"""Skill bundle tar 結構驗證（fail-fast，上傳當下擋掉壞 bundle）。

契約 SSOT = paas-agent-runtime ``runtime/skills/cache.py`` + ``loader.py``，
規則演進需 lock-step（runtime 端先改了規則，這裡也要同步，否則上傳放行 →
pod 首次 invoke 才以 tool_error 浮現，正是本驗證要消滅的 UX 黑洞）。

從 runtime SSOT 精確鏡像的規則：

1. **SKILL.md 必須在 tar root**：cache.py ``_atomic_extract`` 解壓後驗
   ``(dest / "SKILL.md").exists()``；違約 raise ``BundleStructureError`` 並點名
   wrapper dir（``_no_root_skill_md_message``，訊息含 "found nested under wrapper
   dir 'X/'"）。本模組複製同一歸因訊息語意。
2. **frontmatter 解析 + 必填 name/description**：loader.py ``parse_skill_frontmatter``
   用 ``yaml.safe_load(parts[1])``（``---`` 三段切），缺 name 或 description 即 raise。
   **取捨**：Odoo 環境不保證有 PyYAML（odoo-18 requirements.txt / 本模組
   requirements.txt 皆無 yaml；不可為此 wizard 新增 external dependency），故這裡
   用 stdlib 實作「最小 frontmatter 解析」——只認 runtime 對 name / description /
   entrypoint 真正讀的「扁平 ``key: value``」語意（``---`` 分隔、首層 scalar）。
   不支援巢狀 / list / anchor —— 那些 runtime 也不從 frontmatter 頂層讀這三個 key。
   空值（``name:`` 後無內容）視同缺，對齊「空值視同缺」需求。
3. **entrypoint 預設值**：loader.py ``parse_skill_frontmatter`` 取
   ``data.get("entrypoint", "scripts/main.py")``。本模組同樣預設 ``scripts/main.py``，
   並讀 frontmatter 自訂 ``entrypoint``。
4. **解壓防護**：cache.py ``_atomic_extract`` 用 ``tar.extractall(filter="data")``
   擋 path traversal / 絕對路徑 / symlink / hardlink（Python ``data`` filter 的
   既有語意）。runtime 端沒有顯式的「member 數 / 總解壓 size」zip-bomb 數值 cap
   （它靠上游 SHA256 + Odoo wizard 10MB raw cap 把關），故這兩個數值 cap 由本
   wizard 端自訂合理值（見下方常數），honest_caveats 已註明此為 wizard-side 自訂、
   非鏡像自 runtime 既有數值。
"""

import gzip
import io
import posixpath
import tarfile

# --- entrypoint 預設（鏡像 loader.py parse_skill_frontmatter）---
DEFAULT_ENTRYPOINT = "scripts/main.py"

# --- zip-bomb caps（wizard-side 自訂；runtime cache.py 無顯式數值 cap）---
# 上游已有 10MB raw cap + SHA256；這兩個是 defense-in-depth 的二級防線。
# 數量級對齊「一個正常 skill bundle 幾十個檔、解開幾 MB」的常識。
MAX_MEMBER_COUNT = 2000
MAX_TOTAL_EXTRACTED_BYTES = 50 * 1024 * 1024  # 50 MB 解壓後總量


class BundleValidationResult:
    """硬擋（errors）與軟警告（warnings）的容器。

    errors 非空 → wizard 端包成 UserError、一次列出所有違規、不建任何 row。
    errors 空但 warnings 非空 → 照建 skill，回 sticky warning notification。
    """

    def __init__(self):
        self.errors = []
        self.warnings = []

    @property
    def ok(self):
        return not self.errors


def _is_appledouble(member_name):
    """AppleDouble / macOS metadata 偵測（basename ._* 或 __MACOSX/ 路徑）。"""
    if member_name.startswith("__MACOSX/") or "/__MACOSX/" in member_name:
        return True
    base = posixpath.basename(member_name.rstrip("/"))
    return base.startswith("._")


def _is_path_traversal(member_name):
    """member path 是否逃出 tar root（含 ../ 或絕對路徑）。

    鏡像 ``filter="data"`` 的 traversal / absolute-path 防護語意，但這裡主動
    enumerate 以便產生可行動的 UserError（filter 只會丟出泛用 TarError）。
    """
    if member_name.startswith("/") or member_name.startswith("\\"):
        return True
    # 正規化後仍以 .. 開頭 → 逃出 root
    normalized = posixpath.normpath(member_name)
    if normalized == ".." or normalized.startswith("../"):
        return True
    # 任一路徑片段是 ..（防 a/../../etc 這種中段逃逸）
    return ".." in member_name.replace("\\", "/").split("/")


def parse_frontmatter_min(skill_md_text):
    """stdlib 最小 frontmatter 解析，語意對齊 loader.parse_skill_frontmatter
    對 name / description / entrypoint 的讀法（見 module docstring 取捨說明）。

    回傳 (frontmatter_dict, error_message_or_None)。frontmatter_dict 只含成功
    parse 出的扁平 ``key: value``；error_message 在「缺 frontmatter 區塊」或
    「缺必填 name / description（含空值）」時非 None。
    """
    stripped = skill_md_text.lstrip()
    if not stripped.startswith("---"):
        return {}, "SKILL.md frontmatter missing (must start with '---')"
    # 對齊 loader：以 '---' 切三段，需要至少 head / body / rest 三段。
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, "SKILL.md frontmatter not closed with '---'"

    data = {}
    for line in parts[1].splitlines():
        # 跳過空行與註解
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        # 首層扁平 scalar：取冒號後值、去引號與註解尾
        value = value.strip()
        if value and value[0] in ("\"", "'") and value[-1:] == value[0]:
            value = value[1:-1]
        data[key] = value

    missing = [k for k in ("name", "description") if not data.get(k)]
    if missing:
        return data, (
            "SKILL.md frontmatter missing required field(s): %s "
            "(empty value counts as missing)" % ", ".join(missing)
        )
    return data, None


def validate_skill_bundle(raw_bytes, *, wizard_name=None):
    """parse 整包 tar、回 BundleValidationResult（硬擋 errors + 軟警告 warnings）。

    本函式刻意不依賴任何 odoo import，以便在無 Odoo 環境下獨立 smoke test；
    UserError 包裝由 wizard ``action_upload`` 負責。

    :param raw_bytes: 解碼後的 bundle raw bytes（gzip tar）
    :param wizard_name: wizard 表單填的 skill name，用於「name 不一致」軟警告
    """
    result = BundleValidationResult()

    # --- 硬擋 1：合法 gzip tar ---
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz")
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as exc:
        result.errors.append(
            "Bundle is not a valid gzip tar archive (%s); upload a .tar.gz "
            "whose root contains SKILL.md." % type(exc).__name__
        )
        # 無法 parse tar，後續結構檢查全部跳過（沒有 member 可看）。
        return result

    try:
        members = tar.getmembers()
    except (tarfile.TarError, EOFError, OSError) as exc:
        result.errors.append(
            "Bundle tar is corrupt and cannot be read (%s)."
            % type(exc).__name__
        )
        tar.close()
        return result

    # --- 硬擋 - 安全項：traversal / 絕對路徑 / symlink / hardlink / 炸彈 ---
    total_size = 0
    appledouble_hits = []
    file_paths = set()  # 普通檔的正規化相對路徑，供 entrypoint / SKILL.md 檢查
    for member in members:
        name = member.name
        if _is_path_traversal(name):
            result.errors.append(
                "Unsafe member path escapes bundle root: '%s' "
                "(contains '..' or absolute path)." % name
            )
            # 不信任此 member 的其他屬性，不納入 file_paths
            continue
        if member.issym() or member.islnk():
            kind = "symlink" if member.issym() else "hardlink"
            result.errors.append(
                "Bundle contains a %s member '%s'; links are not allowed "
                "(mirror runtime data-filter rejection)." % (kind, name)
            )
            continue
        if member.isfile():
            total_size += member.size
            file_paths.add(posixpath.normpath(name))
        if _is_appledouble(name):
            appledouble_hits.append(name)

    if len(members) > MAX_MEMBER_COUNT:
        result.errors.append(
            "Bundle has %d members, exceeding the %d-member cap "
            "(zip-bomb guard)." % (len(members), MAX_MEMBER_COUNT)
        )
    if total_size > MAX_TOTAL_EXTRACTED_BYTES:
        result.errors.append(
            "Bundle extracts to %d bytes, exceeding the %d-byte cap "
            "(zip-bomb guard)." % (total_size, MAX_TOTAL_EXTRACTED_BYTES)
        )

    # --- 硬擋 2：SKILL.md 必須在 tar root ---
    skill_md_member = None
    for member in members:
        if member.isfile() and posixpath.normpath(member.name) == "SKILL.md":
            skill_md_member = member
            break

    if skill_md_member is None:
        # 點名 wrapper dir（鏡像 cache._no_root_skill_md_message 歸因語意）
        nested = _find_wrapper_dir_skill_md_member(members)
        if nested is not None:
            hint = "found nested under wrapper dir '%s/'" % (
                posixpath.normpath(nested.name).split("/", 1)[0])
        else:
            hint = "SKILL.md not present at any level"
        result.errors.append(
            "SKILL.md not found at bundle root (%s); the bundle must place "
            "SKILL.md at the tar root (Claude Code skill convention) — repack "
            "the contents, not the directory." % hint
        )
        # 即使 SKILL.md 巢在 wrapper dir，仍 parse 它的 frontmatter，把缺
        # name / description 一併列出（多重違規一次回，不只報 wrapper）。
        if nested is not None:
            try:
                nested_text = tar.extractfile(nested).read().decode(
                    "utf-8", errors="replace")
            except (tarfile.TarError, OSError, AttributeError):
                nested_text = ""
            _, fm_error = parse_frontmatter_min(nested_text)
            if fm_error:
                result.errors.append(fm_error)
        # 有硬擋時不加軟警告（壞 bundle 不會建 skill，警告無意義）。
        tar.close()
        return result

    # --- 硬擋 3：frontmatter 缺必填 name / description ---
    try:
        skill_md_text = tar.extractfile(skill_md_member).read().decode(
            "utf-8", errors="replace")
    except (tarfile.TarError, OSError, AttributeError) as exc:
        result.errors.append(
            "SKILL.md at bundle root could not be read (%s)."
            % type(exc).__name__)
        tar.close()
        return result

    frontmatter, fm_error = parse_frontmatter_min(skill_md_text)
    if fm_error:
        result.errors.append(fm_error)

    tar.close()

    # 若已有任何硬擋違規，仍把全部硬擋收齊一次回（不只報第一個）；
    # 軟警告在有硬擋時不額外加（壞 bundle 不會建 skill，警告無意義）。
    if not result.ok:
        return result

    # === 以下為軟警告（bundle 結構合格、skill 照建）===

    # 軟警告 A：entrypoint 檔不存在
    entrypoint = (frontmatter.get("entrypoint") or DEFAULT_ENTRYPOINT)
    entrypoint_norm = posixpath.normpath(entrypoint)
    if entrypoint_norm not in file_paths:
        result.warnings.append(
            "Declared entrypoint '%s' is not present in the bundle; the pod "
            "will fail to launch this skill until the file is added." % entrypoint
        )

    # 軟警告 B：AppleDouble
    _append_appledouble_warning(result, appledouble_hits)

    # 軟警告 C：frontmatter name 與 wizard 填的 name 不一致
    fm_name = frontmatter.get("name")
    if wizard_name and fm_name and fm_name != wizard_name:
        result.warnings.append(
            "SKILL.md frontmatter name '%s' differs from the name '%s' you "
            "entered; the pod identifies the skill by the frontmatter name."
            % (fm_name, wizard_name)
        )

    return result


def _find_wrapper_dir_skill_md_member(members):
    """若 SKILL.md 巢在單層 wrapper dir 下，回該 member（鏡像 cache.py 歸因）。"""
    for member in members:
        if not member.isfile():
            continue
        norm = posixpath.normpath(member.name)
        parts = norm.split("/")
        if len(parts) == 2 and parts[1] == "SKILL.md":
            return member
    return None


def _append_appledouble_warning(result, appledouble_hits):
    if appledouble_hits:
        sample = ", ".join(sorted(appledouble_hits)[:3])
        result.warnings.append(
            "AppleDouble / macOS metadata detected (e.g. %s); these can "
            "confuse the pod loader. Repack with COPYFILE_DISABLE=1 to "
            "avoid them." % sample
        )
