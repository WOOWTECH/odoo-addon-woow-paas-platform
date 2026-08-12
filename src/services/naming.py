import hashlib
import re

# 一個 workspace 的 namespace 身分碼字面形狀：8 個小寫 hex。
#
# 這個形狀是**契約**，不是實作細節：operator 的
# ``cloudflare.py:generate_subdomain`` 用 ``namespace.replace('paas-ws-', '')``
# 取後綴、``reconstruct_mcp_subdomain`` 也依賴它，而 K8s namespace 必須是
# DNS-1123 label。維持與舊 ``md5(slug)[:8]`` 同字集同長度 ⇒ 換掉推導方式**不會**
# 動到任何跨 repo 的命名契約。
# 🔴 錨點是 `\A` / `\Z`，**不是** `^` / `$`：Python 的 `$` 會 match「字串結尾」**或**
# 「結尾換行之前」⇒ `'deadbeef\n'` 會通過 `^[0-9a-f]{8}$` 這道守門，而
# ``make_namespace`` 據此產出 ``'paas-ws-deadbeef\n'`` ——一個帶換行的**非法
# DNS-1123 label**，爆點會落在很遠的 K8s 那端（#675 覆核 F3 實測）。錨點寫在
# pattern 自己身上而不是靠呼叫端改用 ``fullmatch()``：這個常數是共用的，下一個
# 呼叫端用 ``match()`` 或 ``search()`` 都必須是安全的。
# 行為（尾隨空白一律拒絕）由 ``src/tests/test_workspace_ns_hash_677.py`` 的 N11 釘住。
WS_HASH_RE = re.compile(r'\A[0-9a-f]{8}\Z')


def generate_ws_hash(slug: str) -> str:
    """⚠ **legacy 推導**：``md5(slug)[:8]``。新的推導路徑不得再呼叫它（#677）。

    保留的兩個理由，都不是「拿它算新名字」：

    1. ``src/migrations/18.0.1.0.129/post-migrate.py`` 用它回填既有 workspace 的
       ``ws_hash`` —— 回填值必須逐字等於**舊公式的結果**，那個 workspace **之後**
       新建的服務才會落回既有服務所在的 namespace。（既有列存下來的名字本來就不會
       被重推，所以回填守的不是它們；呼叫點實查見該 migration 的檔頭。）
    2. 測試拿它當「修復前的期望值」現算（寫死字面常數會假綠）。

    🔴 為什麼不能再拿它推導新名字：``slug`` 由租戶提供的 ``name`` 逐字決定
    （``Workspace._generate_slug()`` 對 ``[a-z0-9_-]`` 是恆等函式），而 ``[:8]``
    是 **32 bit 截斷** ⇒ 租戶離線搜一個 name 就能讓自己的 workspace 映到別人的
    K8s namespace（目標值逐字印在受害者每個公開子網域上，不必猜）。即使沒有攻擊
    者，N 個 workspace 的生日碰撞機率約 N²/2^33（N=10,000 時約 1.2%）。
    ⇒ namespace 身分改成 ``workspace.ws_hash``（伺服器產生、DB UNIQUE），見
    ``models/workspace.py::_generate_ws_hash``。
    """
    return hashlib.md5(slug.encode()).hexdigest()[:8]


def generate_resource_hash(reference_id: str, name: str) -> str:
    """Generate 8-char salted hex hash for a resource."""
    return hashlib.md5((reference_id + name).encode()).hexdigest()[:8]


def _require_ws_hash(ws_hash) -> str:
    """守門：下面的 maker 收的是 ``workspace.ws_hash``，**不是** ``workspace.slug``。

    🔴 這是「漏改一個呼叫點」的絆線。沒有它，漏改的呼叫點會安靜地產出
    ``paas-ws-ws-137425`` / ``paas-cs-my-workspace-…`` 這種**看起來很像對的**名字：
    仍然是合法的 DNS label、仍然以正確前綴開頭、仍然部署得起來，只是指向一個不存在
    於任何 workspace 記錄上的 namespace。空值／``False``（欄位沒回填）同樣落在
    raise 這一側 ＝ fail-closed，不會產出 ``paas-ws-False``。
    """
    if not isinstance(ws_hash, str) or not WS_HASH_RE.match(ws_hash):
        raise ValueError(
            "ws_hash must be 8 lowercase hex chars, got %r. These helpers take "
            "workspace.ws_hash (the stored, server-generated namespace identity), "
            "NOT workspace.slug — the slug is tenant-controlled and its md5[:8] is "
            "a 32-bit truncation (#677)." % (ws_hash,)
        )
    return ws_hash


def make_namespace(ws_hash: str, prefix: str = 'paas-ws-') -> str:
    """Generate K8s namespace name: {prefix}{ws_hash}."""
    return f"{prefix}{_require_ws_hash(ws_hash)}"


def _env_token(env: str = '') -> str:
    """Normalise a deploy-env value into a DNS-safe naming token segment.

    Returned segment is inserted right after the ``paas-`` prefix
    (format ``paas-{env}-{type}-``):

    - ``''`` / ``'prod'`` -> ``''``. Production names stay byte-identical to the
      pre-env-token scheme, so existing records and hard-coded format assertions
      do not drift.
    - any other env (e.g. ``'stg'``) -> ``'{env}-'``.

    Case/whitespace are normalised as a guard against a mis-set value. The
    upstream ``deploy_env`` config is a Selection whitelist, so this is a
    defensive normaliser, not a full sanitiser.
    """
    normalised = (env or '').lower().strip()
    if normalised in ('', 'prod'):
        return ''
    # slugify to a DNS-safe label segment. deploy_env is a Selection whitelist
    # upstream, but a raw ir.config_parameter write could be anything — never let
    # it produce an illegal DNS/k8s label (spaces / underscores / non-ascii / a
    # trailing dash) that would break the CF tunnel name or DNS comment.
    slug = re.sub(r'[^a-z0-9]+', '-', normalised).strip('-')
    return f"{slug}-" if slug else ''


def make_service_subdomain(ws_hash: str, reference_id: str, name: str, env: str = '') -> str:
    """Generate cloud service subdomain: paas-{env}-cs-{ws_hash}-{svc_hash}."""
    return f"paas-{_env_token(env)}cs-{_require_ws_hash(ws_hash)}-{generate_resource_hash(reference_id, name)}"


def make_smarthome_subdomain(ws_hash: str, reference_id: str, name: str, env: str = '') -> str:
    """Generate smart home subdomain: paas-{env}-sm-{ws_hash}-{sm_hash}."""
    return f"paas-{_env_token(env)}sm-{_require_ws_hash(ws_hash)}-{generate_resource_hash(reference_id, name)}"


def make_security_access_subdomain(ws_hash: str, reference_id: str, name: str, env: str = '') -> str:
    """Generate security access subdomain: paas-{env}-sa-{ws_hash}-{sa_hash}."""
    return f"paas-{_env_token(env)}sa-{_require_ws_hash(ws_hash)}-{generate_resource_hash(reference_id, name)}"


def reconstruct_mcp_subdomain(namespace: str, release_name: str) -> str:
    """Reconstruct a CloudService MCP sidecar route's Cloudflare subdomain.

    Used by the L4 reverse-orphan reconciler to recognise a live MCP DNS /
    ingress rule as NOT-an-orphan. It reconstructs the subdomain from the
    **stored** ``helm_release_name`` + ``helm_namespace`` (not re-derived from
    slug), so it matches whatever was actually deployed.

    MUST stay byte-identical to the operator's deploy/teardown transform:

    - operator ``cloudflare.py:generate_subdomain(namespace, release_name)`` =
      ``f"{release_name}-{namespace.replace('paas-ws-', '')}"``;
    - operator ``releases.py`` then appends ``"-mcp"`` for the MCP route.

    So full MCP subdomain = ``f"{release_name}-{ns_suffix}-mcp"``. Any drift from
    the operator formula would falsely flag a live MCP route as orphan — guarded
    by golden cases in ``test_naming.py`` (cross-repo replica, no shared import).
    """
    ns_suffix = (namespace or '').replace('paas-ws-', '')
    return f"{release_name}-{ns_suffix}-mcp"


# L4 reconciler 分類用：從 CF 資源 name 解析 env token + 資源類別 ──────────────
# literal ``stg`` 白名單（**非**寬鬆 ``[a-z0-9]+``）：deploy_env 是 {prod,stg} Selection，
# _env_token 只會吐 '' 或 'stg-'。寬鬆 class 會把 ``paas-foo-sa-`` 誤判 env='foo'（違反
# env∈{prod,stg} 不變量），且 _env_token 可吐多 label token（'foo bar'→'paas-foo-bar-sa-'）
# 寬鬆 single-label class 反而 match 失敗靜默 unknown。literal 白名單對非白名單 token 一律
# 不 match → 落 unknown（fail-safe，不 over-accept 也不靜默漏）。
_PAAS_NAME_RE = re.compile(r'^paas-(?:(stg)-)?(sa|sm|cs)-')


def parse_paas_name(name):
    """從 paas 資源 CF name 解析 ``(env_token, kind)``，鏡像 ``_env_token`` 對 {prod,stg} 白名單輸出。

    name 格式 ``paas-{env_token}{type}-...``（``_env_token`` 對 prod/'' 吐空、stg 吐 'stg-'）：

    - ``paas-stg-sa-...`` → ``('stg', 'sa')``（explicit env token）
    - ``paas-sa-...`` → ``(None, 'sa')``（無 token：**prod 與 legacy byte-identical、不可分** →
      env_token=None，分類層落 ``env_hint='unknown'``，prod 確認靠 DP-DIFF prod-DB-diff）
    - ``paas-foo-sa-...`` / ``paas-foo-bar-sa-...`` → ``(None, None)``（非白名單 token 不 over-accept
      成 env='foo'，整體不 match → unknown）
    - 非 paas 名（官網 / apex / 行銷子網域）→ ``(None, None)``

    ``cs`` 分支對現行三類 inventory（tunnel/access_app/access_policy）是 **DNS 類（TS-3b+）預留**
    dead-branch —— CS 無 dedicated tunnel、access app/policy 為 SA 專屬；待 DNS 進 inventory 才命中。
    """
    m = _PAAS_NAME_RE.match(name or '')
    if not m:
        return (None, None)
    return (m.group(1), m.group(2))


def classify_env_hint(name, pinned_env):
    """回 ``(env_hint, is_cross_env)``：依資源 name 的 env token 與 reconciler 鎖定的 ``pinned_env``。

    - explicit token（如 'stg'）：``env_hint=token``；``is_cross_env = token != pinned_env``
      （prod-pinned 看到 ``paas-stg-*`` = cross-env，屬 stg、非本 env 孤兒）。
    - **無 token**（prod / legacy / 非 paas 名）：``('unknown', False)``。prod 空 token 與 legacy
      不可分、不瞎猜（單向：只抓得到「stg 資源被 prod reconciler 看到」；反向 prod 資源被 stg
      reconciler 看到只能落 unknown）。``is_cross_env`` 對 unknown 一律 False（不據以排除/動作）。
    """
    env_token, _kind = parse_paas_name(name)
    if env_token is None:
        return ('unknown', False)
    return (env_token, env_token != pinned_env)
