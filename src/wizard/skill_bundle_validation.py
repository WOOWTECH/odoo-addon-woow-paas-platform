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
   本模組以 **``yaml.safe_load`` 為主要 parser**，精確鏡像 runtime：同樣三段切 ``---``、
   同樣 ``yaml.safe_load`` frontmatter 區塊，故 runtime 會在 pod 端 raise 的 YAML 文法
   錯（tab 縮排 / 未閉合引號 / 壞 flow-style）這裡就先擋掉。``yaml`` 已是本模組
   requirements.txt 宣告的依賴（``pyyaml>=6.0``，經 ai_base_gt），manifest
   ``external_dependencies`` 也宣告 ``yaml``，install 時即驗其存在。
   **fallback 路徑**：若 import yaml 失敗（``yaml is None``），退回 stdlib 最小解析
   （``_parse_frontmatter_stdlib``），只認扁平 ``key: value``。此 fallback **無法偵測
   YAML 文法錯**（已知契約缺口）—— 它不會 raise，壞文法的 frontmatter 會被當成
   「缺 name/description」或部分誤判；正常部署有 yaml，不走此路徑。
3. **entrypoint 預設值**：loader.py ``parse_skill_frontmatter`` 取
   ``data.get("entrypoint", "scripts/main.py")``。本模組同樣預設 ``scripts/main.py``，
   並讀 frontmatter 自訂 ``entrypoint``。
4. **解壓防護**：cache.py ``_atomic_extract`` 用 ``tar.extractall(filter="data")``。
   Python ``data`` filter 的語意（已實測 ``tarfile._get_filtered_attrs``）：
   - 擋 path traversal / 絕對路徑（``OutsideDestinationError`` / ``AbsolutePathError``）。
   - member type 只放行 ``isreg`` / ``islnk`` / ``isdir`` / ``issym``；其餘
     （FIFO / char / block / 其他 special file）一律 ``raise SpecialFileError`` →
     整包 ``extractall`` abort、pod 每次 invoke 都炸。本模組鏡像此 member-type 白名單
     （見下方 ``_ALLOWED_MEMBER_TYPE_CHECKS``）硬擋 special files。
   - link（symlink / hardlink）：data filter **放行 in-bounds 相對 link**，只擋絕對
     linkname / 逃出 dest 的 link。本 wizard 採 **defense-in-depth、刻意比 runtime 嚴**：
     無條件硬擋任何 link member（理由：skill bundle 不該有 link，全擋最省事且最安全），
     訊息誠實標明此為 wizard policy（stricter than runtime）。
   - runtime 端沒有顯式的「member 數 / 總解壓 size / member name 長度」zip-bomb 數值 cap
     （它靠上游 SHA256 + Odoo wizard 10MB raw cap 把關），故這幾個數值 cap 由本
     wizard 端自訂合理值（見下方常數），honest_caveats 已註明此為 wizard-side 自訂、
     非鏡像自 runtime 既有數值。為避免「物化整包 member list」本身成為 DoS 向量
     （``getmembers()`` 對百萬空 member 會吃大量記憶體/CPU），本模組改 **streaming 迭代**
     ``for member in tar:``，逐筆 yield、先擋後讀，超 cap 立即中止（最壞只解析到 cap+1 個
     member）。
"""

import gzip
import io
import posixpath
import tarfile
import zlib

# frontmatter 主要 parser = yaml.safe_load（精確鏡像 runtime loader）；缺 yaml 時
# 退回 stdlib 最小解析（見 module docstring fallback 說明）。
try:
    import yaml
except ImportError:  # pragma: no cover - 正常部署有 pyyaml（requirements.txt 宣告）
    yaml = None

# --- entrypoint 預設（鏡像 loader.py parse_skill_frontmatter）---
DEFAULT_ENTRYPOINT = "scripts/main.py"

# --- zip-bomb caps（wizard-side 自訂；runtime cache.py 無顯式數值 cap）---
# 上游已有 10MB raw cap + SHA256；這幾個是 defense-in-depth 的二級防線。
# 數量級對齊「一個正常 skill bundle 幾十個檔、解開幾 MB」的常識。
MAX_MEMBER_COUNT = 2000
MAX_TOTAL_EXTRACTED_BYTES = 50 * 1024 * 1024  # 50 MB 解壓後總量
# member name 長度上限：擋「少量 member 但每個 name 超長」放大 DoS（getmembers 物化成本）。
MAX_MEMBER_NAME_LENGTH = 4096


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


def _is_allowed_member_type(member):
    """member type 是否落在 data-filter 放行的白名單。

    鏡像 ``tarfile._get_filtered_attrs`` 的 member-type 處理：data filter 只放行
    regular file / hardlink / dir / symlink，其餘（FIFO / char dev / block dev /
    其他 special file）一律 ``raise SpecialFileError`` → 整包 extract abort。
    """
    return (
        member.isfile()
        or member.isdir()
        or member.issym()
        or member.islnk()
    )


def parse_frontmatter_min(skill_md_text):
    """解析 SKILL.md frontmatter，語意對齊 loader.parse_skill_frontmatter
    對 name / description / entrypoint 的讀法。

    主要路徑用 ``yaml.safe_load`` 精確鏡像 runtime；若 yaml 不可用退回 stdlib
    最小解析（見 module docstring fallback 缺口說明）。

    回傳 (frontmatter_dict, error_message_or_None)。error_message 在「缺
    frontmatter 區塊」「YAML 文法錯」「frontmatter 不是 mapping」或「缺必填
    name / description（含空值）」時非 None。
    """
    stripped = skill_md_text.lstrip()
    if not stripped.startswith("---"):
        return {}, "SKILL.md frontmatter missing (must start with '---')"
    # 對齊 loader：以 '---' 切三段，需要至少 head / body / rest 三段。
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, "SKILL.md frontmatter not closed with '---'"

    if yaml is not None:
        return _parse_frontmatter_yaml(parts[1])
    return _parse_frontmatter_stdlib(parts[1])


def _parse_frontmatter_yaml(block):
    """以 ``yaml.safe_load`` 解析 frontmatter 區塊（精確鏡像 runtime loader）。"""
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        # runtime 端 loader.parse_skill_frontmatter 也走 yaml.safe_load，會以同樣的
        # ScannerError / ParserError 在 pod 首次 invoke 時炸；這裡先擋。
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return {}, (
            "SKILL.md frontmatter is not valid YAML (%s); the runtime parses it "
            "with yaml.safe_load on the pod and would fail the same way at first "
            "invoke." % reason
        )
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, (
            "SKILL.md frontmatter must be a YAML mapping (key: value), not a %s; "
            "the runtime reads name / description as top-level keys."
            % type(data).__name__
        )
    missing = _missing_required(data)
    if missing:
        return data, _missing_required_message(missing)
    return data, None


def _parse_frontmatter_stdlib(block):
    """stdlib 最小 frontmatter 解析（yaml 不可用時的 fallback）。

    只認扁平 ``key: value``、去引號與註解尾。**無法偵測 YAML 文法錯**（tab 縮排 /
    未閉合引號等不會 raise）—— 這是 fallback 的已知契約缺口，正常部署有 pyyaml 不走
    此路徑（見 module docstring）。
    """
    data = {}
    for line in block.splitlines():
        # 跳過空行與整行註解
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

    missing = _missing_required(data)
    if missing:
        return data, _missing_required_message(missing)
    return data, None


def _missing_required(data):
    """name / description 缺或空值（含 None / 空字串）視同缺。"""
    missing = []
    for key in ("name", "description"):
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing


def _missing_required_message(missing):
    return (
        "SKILL.md frontmatter missing required field(s): %s "
        "(empty value counts as missing)" % ", ".join(missing)
    )


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
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError, OSError) as exc:
        result.errors.append(
            "Bundle is not a valid gzip tar archive (%s); upload a .tar.gz "
            "whose root contains SKILL.md." % type(exc).__name__
        )
        # 無法 parse tar，後續結構檢查全部跳過（沒有 member 可看）。
        return result

    try:
        _scan_members(result, tar, wizard_name)
    finally:
        tar.close()
    return result


def _scan_members(result, tar, wizard_name):
    """streaming 迭代 tar member、收齊硬擋 + 軟警告（不物化整包 member list）。

    迭代用 ``for member in tar``（TarFile 是 iterator，逐筆 yield），先擋後讀：超
    member 數 / size / name 長度 cap 立即中止（最壞只到 cap+1）；special file /
    link / traversal 即時硬擋。需要的 member（root SKILL.md、wrapper-nested
    SKILL.md）邊迭代邊記下，迴圈後再 extractfile 讀其 frontmatter。
    """
    total_size = 0
    member_count = 0
    appledouble_hits = []
    file_paths = set()  # 普通檔的正規化相對路徑，供 entrypoint / SKILL.md 檢查
    skill_md_member = None  # root SKILL.md
    nested_skill_md_member = None  # 單層 wrapper dir 下的 SKILL.md（歸因用）
    aborted = False  # 是否因 cap 提早中止（中止後結構檢查不完整，避免誤報）

    try:
        member_iter = iter(tar)
        while True:
            try:
                member = next(member_iter)
            except StopIteration:
                break

            member_count += 1
            # 先擋數量 cap：最壞只解析到 cap+1 個 member 就中止，不物化全部。
            if member_count > MAX_MEMBER_COUNT:
                result.errors.append(
                    "Bundle has more than %d members, exceeding the member cap "
                    "(zip-bomb guard); stopped scanning at member %d."
                    % (MAX_MEMBER_COUNT, member_count)
                )
                aborted = True
                break

            name = member.name
            # member name 長度 cap：擋「少量 member 但 name 超長」放大 DoS。
            if len(name) > MAX_MEMBER_NAME_LENGTH:
                result.errors.append(
                    "Bundle contains a member whose name is %d characters, "
                    "exceeding the %d-character limit (zip-bomb guard)."
                    % (len(name), MAX_MEMBER_NAME_LENGTH)
                )
                aborted = True
                break

            if _is_path_traversal(name):
                result.errors.append(
                    "Unsafe member path escapes bundle root: '%s' "
                    "(contains '..' or absolute path)." % name
                )
                # 不信任此 member 的其他屬性，不納入 file_paths
                continue

            # member type 白名單（鏡像 data filter）：先擋 special files，再擋 link。
            if not _is_allowed_member_type(member):
                result.errors.append(
                    "Bundle contains a non-regular member '%s' (links/device/"
                    "special files are not permitted); the runtime's data "
                    "extract filter aborts the whole bundle on such members."
                    % name
                )
                continue
            if member.issym() or member.islnk():
                kind = "symlink" if member.issym() else "hardlink"
                result.errors.append(
                    "Bundle contains a %s member '%s'; wizard policy disallows "
                    "any link member (stricter than the runtime, which permits "
                    "in-bounds relative links)." % (kind, name)
                )
                continue

            if member.isfile():
                total_size += member.size
                if total_size > MAX_TOTAL_EXTRACTED_BYTES:
                    result.errors.append(
                        "Bundle extracts to more than %d bytes, exceeding the "
                        "extracted-size cap (zip-bomb guard)."
                        % MAX_TOTAL_EXTRACTED_BYTES
                    )
                    aborted = True
                    break
                norm = posixpath.normpath(name)
                file_paths.add(norm)
                if norm == "SKILL.md":
                    skill_md_member = member
                elif (
                    nested_skill_md_member is None
                    and len(norm.split("/")) == 2
                    and norm.split("/")[1] == "SKILL.md"
                ):
                    nested_skill_md_member = member

            if _is_appledouble(name):
                appledouble_hits.append(name)
    except (tarfile.TarError, zlib.error, EOFError, OSError) as exc:
        result.errors.append(
            "Bundle tar is corrupt and cannot be read (%s)."
            % type(exc).__name__
        )
        return

    # cap 提早中止：結構不完整，先把已收的硬擋回掉，不再做 SKILL.md / frontmatter 檢查
    # （避免「因為提早 break 而誤報缺 SKILL.md」）。
    if aborted:
        return

    # --- 硬擋 2：SKILL.md 必須在 tar root ---
    if skill_md_member is None:
        # 點名 wrapper dir / 目錄誤植（鏡像 cache._no_root_skill_md_message 歸因語意）
        if nested_skill_md_member is not None:
            hint = "found nested under wrapper dir '%s/'" % (
                posixpath.normpath(nested_skill_md_member.name).split("/", 1)[0])
        elif "SKILL.md" in {posixpath.normpath(p) for p in _dir_paths(tar)}:
            hint = "SKILL.md exists as a directory, not a file"
        else:
            hint = "SKILL.md not present at any level"
        result.errors.append(
            "SKILL.md not found at bundle root (%s); the bundle must place "
            "SKILL.md at the tar root (Claude Code skill convention) — repack "
            "the contents, not the directory." % hint
        )
        # 即使 SKILL.md 巢在 wrapper dir，仍 parse 它的 frontmatter，把缺
        # name / description 一併列出（多重違規一次回，不只報 wrapper）。
        if nested_skill_md_member is not None:
            nested_text = _read_member_text(tar, nested_skill_md_member)
            _, fm_error = parse_frontmatter_min(nested_text)
            if fm_error:
                result.errors.append(fm_error)
        # 有硬擋時不加軟警告（壞 bundle 不會建 skill，警告無意義）。
        return

    # --- 硬擋 3：frontmatter 缺必填 name / description ---
    skill_md_text = _read_member_text(tar, skill_md_member, track_error=result)
    if skill_md_text is None:
        return

    frontmatter, fm_error = parse_frontmatter_min(skill_md_text)
    if fm_error:
        result.errors.append(fm_error)

    # 若已有任何硬擋違規，仍把全部硬擋收齊一次回（不只報第一個）；
    # 軟警告在有硬擋時不額外加（壞 bundle 不會建 skill，警告無意義）。
    if not result.ok:
        return

    # === 以下為軟警告（bundle 結構合格、skill 照建）===

    # 軟警告 A：entrypoint 檔不存在
    entrypoint = (frontmatter.get("entrypoint") or DEFAULT_ENTRYPOINT)
    entrypoint_norm = posixpath.normpath(str(entrypoint))
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


def _dir_paths(tar):
    """已開啟 tar 的目錄 member 路徑（供「SKILL.md 為目錄」歸因；二次輕量迭代）。

    僅在 root SKILL.md 缺失的錯誤分支才呼叫（罕見路徑），故再迭代一次可接受。
    """
    dirs = []
    try:
        for member in tar:
            if member.isdir():
                dirs.append(member.name)
    except (tarfile.TarError, zlib.error, EOFError, OSError):
        pass
    return dirs


def _read_member_text(tar, member, *, track_error=None):
    """讀 member 內容為 utf-8 文字；失敗回 ""（或 None 並記 error）。"""
    try:
        return tar.extractfile(member).read().decode("utf-8", errors="replace")
    except (tarfile.TarError, zlib.error, OSError, AttributeError) as exc:
        if track_error is not None:
            track_error.errors.append(
                "SKILL.md at bundle root could not be read (%s)."
                % type(exc).__name__)
            return None
        return ""


def _append_appledouble_warning(result, appledouble_hits):
    if appledouble_hits:
        sample = ", ".join(sorted(appledouble_hits)[:3])
        result.warnings.append(
            "AppleDouble / macOS metadata detected (e.g. %s); these can "
            "confuse the pod loader. Repack with COPYFILE_DISABLE=1 to "
            "avoid them." % sample
        )
