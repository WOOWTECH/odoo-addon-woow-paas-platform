"""L4 reverse-orphan reconciler — Odoo 端左集蒐集 + 差集引擎（方向 A）。

operator 端（``extra/paas-operator/src/api/reconcile.py``）唯讀回 CF 帳號級 inventory（右集
ground truth）；本模組蒐集 Odoo 持有的 stored-id 左集（白名單），TS-3c 再對右集取差集
（CF 有、左集沒有 = 孤兒候選）——這是 record-driven 的 ``subscription_lifecycle_cron`` 永遠
做不到的反向。

**首版零刪除**：本檔與 cron / 報表同屬 reconcile 路徑，受 delete-gate（grep）守，禁任何
``delete_*`` / ``uninstall`` / ``unlink`` / ``scale_*`` / ``subprocess`` 等破壞動詞（見
``src/tests/test_reconcile_delete_gate.py``）。

**.sudo() 是 reconciler 的正當權限，非 ACL 反模式**：L4 是平台級反向對帳，必須看見**所有
租戶**的 stored-id 才能判定某 CF 資源是否仍被某租戶持有（漏看 = 把別租戶的活躍資源誤判孤兒）。
這與「controller 端 tenant 請求不可 .sudo() 繞 ir.rule」是不同情境（後者是反模式）；此處同
``subscription_lifecycle_cron`` 的 system-level 掃描。
"""
import logging
from dataclasses import dataclass, field

from .naming import classify_env_hint, reconstruct_mcp_subdomain
from .paas_operator import get_paas_operator_client

_logger = logging.getLogger(__name__)


@dataclass
class LeftSet:
    """Odoo 持有的 stored-id / subdomain 左集（白名單），依 kind 分組。

    id 類（與 operator inventory 的 ``CfResource.id`` 對齊，可直接差集）：
      ``tunnel_ids`` / ``access_app_ids`` / ``access_policy_ids``。
    DNS 類（待 operator inventory 補 DNS 後啟用差集；TS-3b 先蒐齊）：
      ``dns_record_ids``（id 對齊）+ ``subdomains``（bare label 第二鍵：subdomain_prefix /
      CS subdomain / 重建的 MCP subdomain / SM subdomain；覆蓋 in-flight 未回填 ``dns_record_id``
      的活躍 route。FQDN ↔ label 的對映在 DNS 差集落地時解，TS-3b 蒐 bare label 較 immutable）。
    """

    tunnel_ids: set = field(default_factory=set)
    access_app_ids: set = field(default_factory=set)
    access_policy_ids: set = field(default_factory=set)
    dns_record_ids: set = field(default_factory=set)
    subdomains: set = field(default_factory=set)
    namespace_names: set = field(default_factory=set)  # #277：CS ∪ agent 的 live ns 名（差集鍵=ns 名本身）


_EMPTY = (False, None, "")


def _clean(values):
    """丟掉 False / None / 空字串（空 stored-id 不入左集，design B.3：in-flight 半完成不誤當持有）。"""
    return {v for v in values if v not in _EMPTY}


def collect_left_set(env) -> LeftSet:
    """蒐集 SA / CS / SM 三表的 stored-id 左集（白名單）。

    - **SA（2 層巢狀 walk）**：``tunnel_id`` + ``route_ids``（``dns_record_id`` /
      ``access_app_id`` / ``subdomain_prefix``）+ **巢狀** ``route_ids.access_policy_ids.cf_policy_id``。
      走 ``route_id`` binding 鏈（dotted ``mapped`` 自動展開兩層）；**不**走 policy 的 store=True
      related ``security_access_id``（design B.3 警示：那只供 ir.rule/quota，非真 binding）。
    - **CS**：``subdomain`` + 重建的 MCP sidecar subdomain（``reconstruct_mcp_subdomain``，
      與 operator deploy/teardown 同源；缺 helm 欄位的未部署 CS 不亂湊）。
    - **SM**：``tunnel_id``（dedicated 反查鍵）+ ``subdomain``。
    """
    ls = LeftSet()

    # ── SA：2 層巢狀（最高衝擊正確性點）──
    sa = env['woow_paas_platform.security_access'].sudo().search([])
    ls.tunnel_ids |= _clean(sa.mapped('tunnel_id'))
    routes = sa.mapped('route_ids')
    ls.dns_record_ids |= _clean(routes.mapped('dns_record_id'))
    ls.access_app_ids |= _clean(routes.mapped('access_app_id'))
    ls.subdomains |= _clean(routes.mapped('subdomain_prefix'))
    # 第 2 層：route_ids → access_policy_ids → cf_policy_id（dotted mapped 走完整 binding 鏈）
    ls.access_policy_ids |= _clean(sa.mapped('route_ids.access_policy_ids.cf_policy_id'))

    # ── CS ──
    cs = env['woow_paas_platform.cloud_service'].sudo().search([])
    ls.subdomains |= _clean(cs.mapped('subdomain'))
    for service in cs:
        if service.helm_release_name and service.helm_namespace:
            ls.subdomains.add(
                reconstruct_mcp_subdomain(service.helm_namespace, service.helm_release_name)
            )

    # ── SM ──
    sm = env['woow_paas_platform.smart_home'].sudo().search([])
    ls.tunnel_ids |= _clean(sm.mapped('tunnel_id'))
    ls.subdomains |= _clean(sm.mapped('subdomain'))

    # ── ns（#277）：cluster paas-ws-* 的 live 白名單 = CS 共用 ns ∪ agent per-project ns ──
    # 漏 agent ns 會把所有活躍 agent ns 誤判孤兒（災難）；CS state='deleting' 排除（過渡態，
    # 即將消失，否則誤判即將刪除的 ns 為活躍）；helm_namespace 非唯一（多 CS 共用）→ 入 set 去重。
    cs_ns = env['woow_paas_platform.cloud_service'].sudo().search(
        [('helm_namespace', '!=', False), ('state', '!=', 'deleting')]
    )
    ls.namespace_names |= _clean(cs_ns.mapped('helm_namespace'))
    ag_ns = env['woow_paas_platform.agent_session'].sudo().search(
        [('helm_namespace', '!=', False)]
    )
    ls.namespace_names |= _clean(ag_ns.mapped('helm_namespace'))

    return ls


# ── 差集引擎（右集 − 左集 = 孤兒候選）─────────────────────────────────────


@dataclass
class OrphanCandidate:
    """單筆疑似孤兒（對齊 operator ReconcileResponse 的 OrphanCandidate shape）。

    首版（TS-3）只做**結構**差集：``reconcilable`` 恆 False、``env_hint`` 恆 'unknown'
    （env 歸屬 / R1-R5 分類是 TS-4）。
    """

    id: str
    name: str
    kind: str
    env_hint: str = 'unknown'
    reconcilable: bool = False
    reason: str = ''


@dataclass
class ReconcileResult:
    """Odoo reconciler 產出（對齊 ReconcileResponse golden：dry_run / partial / orphan_candidates）。"""

    dry_run: bool = True
    partial: bool = False
    orphan_candidates: list = field(default_factory=list)


# CF 資源 kind → LeftSet 上對應的 id 集合屬性（只列「右集 inventory 目前會回」的 kind）。
_KIND_TO_LEFTSET_ATTR = {
    'tunnel': 'tunnel_ids',
    'access_app': 'access_app_ids',
    'access_policy': 'access_policy_ids',
    'namespace': 'namespace_names',   # #277：差集鍵=ns 名本身（缺此對映則 compute_orphans skip+partial 靜默失效）
    # 'dns_record': 'dns_record_ids',  # 待 operator inventory 補 DNS 後啟用
}


def compute_orphans(resources, left_set, *, partial=False) -> ReconcileResult:
    """純差集：右集（resources）− 左集（left_set 對應 id 集）= 孤兒候選。

    方向恆為 **右 − 左**（CF 有、Odoo 沒有），故右集退化 / 空 → 少報（safe miss），
    **絕不**把左集全判孤兒（= 災難）。畸形（缺 id）或尚無左集對映的 kind（如未來 dns_record
    在對映補上前）→ 跳過 + 標 partial（不亂判、不靜默漏 = no-silent-cap）。
    首版只做**結構**差集：所有候選 reconcilable=False / env_hint='unknown'（分類是 TS-4）。
    """
    candidates = []
    degraded = bool(partial)
    for r in resources:
        rid = r.get('id')
        kind = r.get('kind')
        attr = _KIND_TO_LEFTSET_ATTR.get(kind)
        if not rid or attr is None:
            degraded = True
            _logger.warning(
                "reconcile diff: 跳過無法比對的 inventory 項（id=%r kind=%r）→ 標 partial",
                rid, kind,
            )
            continue
        if rid not in getattr(left_set, attr):
            candidates.append(OrphanCandidate(
                id=rid,
                name=r.get('name', '') or '',
                kind=kind,
                env_hint='unknown',
                reconcilable=False,
                reason='CF %s 不在 Odoo stored-id 左集；結構孤兒候選（未分類，人工審）' % kind,
            ))
    return ReconcileResult(dry_run=True, partial=degraded, orphan_candidates=candidates)


# ── 分類層（TS-4 S3+S4）：env attribution + 結構 allowlist + protected + tripwire ──────
#
# 與 compute_orphans（純結構差集）刻意**分離**：diff 產 raw 候選（env_hint='unknown' /
# reconcilable=False），classify_candidates 再 enrich。pipeline 順序固定：**diff → classify**，
# 且 classify 內 **protected-override 在 reconcilable predicate 之後**（即使首版 predicate 不產
# True，順序是正確性結構一部分）。

# 結構 allowlist（R1）：唯有「kind ∈ 可載 owner-marker 類」才有資格升 reconcilable=True。
# **首版空集**：三類 inventory（tunnel / access_app / access_policy）全 un-reconcilable
# （dedicated tunnel CNAME 指自身、Access app 無 metadata 欄位蓋 owner tag、legacy 無 token）；
# 唯一可 True 的 SA route DNS（有 owner comment）待 DNS 進 inventory（TS-3b+）才納。故 TS-4 後
# 對任何**真實** inventory 項 reconcilable 恆 False（True-path 僅供合成注入測 tripwire）。
_RECONCILABLE_ELIGIBLE_KINDS = frozenset()


def _is_reconcilable(candidate, env_hint, pinned_env):
    """結構 allowlist predicate（R1）+ ``unknown`` / cross-env 硬不變式（對抗式批判 HIGH#2 + code review）。

    升 ``reconcilable=True`` 需同時：① ``env_hint != 'unknown'``（**硬不變式**：prod 空 token 與
    legacy byte-identical 都落 unknown） ② ``env_hint == pinned_env``（**cross-env 結構 gate，與
    unknown 對稱**：別 env 的資源結構上不歸我刪；否則未來開 eligible kind 時，正常 cross-env 常態
    會被 _is_reconcilable 放成 True 再被 tripwire (b) 整輪 abort —— 把正常情境誤升 hard-fail）
    ③ kind ∈ owner-marker-eligible（首版空集 → 恆 False）。
    首版恆回 False（無 eligible kind）。未來開 SA route DNS 時亦受 ownership 證據（DP-MARKER）gate
    —— **name 相符不可作為 ownership 證據**（手動建資源可照抄命名）。tripwire 是此結構 gate 的
    backstop（正常路徑不 fire，僅抓繞過本 predicate 的其他寫入路徑）。
    """
    if env_hint == 'unknown':           # 硬不變式：unknown 一律不可 reconcilable
        return False
    if env_hint != pinned_env:          # cross-env 結構 gate（別 env 資源不歸我刪，與 unknown 對稱）
        return False
    if candidate.kind not in _RECONCILABLE_ELIGIBLE_KINDS:
        return False
    # 此處未來補 ownership 證據檢查（DP-MARKER）；首版 eligible 集為空、走不到。
    return True


def _ns_env_hint(name: str) -> str:
    """#277 ns 專用 env 歸屬（classify_env_hint 的 ``paas-{env}-{sa|sm|cs}`` 格式不認 ``paas-ws-*`` ns）。

    鏡像 operator ``config._env_hint_from_prefix``：dev（test/dev）先判、再 stg、bare ``paas-ws-`` 落
    prod（prod 部署的實際 prefix，含 agent 的 ``paas-ws-pa-*``）。與 operator 同源避免兩端 env 判定漂移。
    """
    if name.startswith('paas-ws-test') or name.startswith('paas-ws-dev'):
        return 'dev'
    if name.startswith('paas-ws-stg-'):
        return 'stg'
    if name.startswith('paas-ws-'):
        return 'prod'
    return 'unknown'


def _run_tripwires(candidates, *, pinned_env, shared_tunnel_id):
    """三條 R5 tripwire（fail-loud）：任一 reconcilable=True 候選若違反 → raise（代表 classifier
    有 bug）。首版 reconcilable 恆 False → 正常不 fire；以合成注入測其真的會炸（非擺設）。

    (a) ``env_hint=='unknown'`` 不得 reconcilable=True（unknown 硬不變式的 assert 對位）。
    (b) cross-env（explicit token != pinned_env）不得 reconcilable=True（別 env 的資源不歸我刪）。
    (c) shared tunnel 本體（id == operator-reported shared_tunnel_id）不得 reconcilable=True
        （永不刪 shared tunnel 本體；只清指向它的 DNS/ingress）。
    """
    for c in candidates:
        if not c.reconcilable:
            continue
        if c.env_hint == 'unknown':
            raise RuntimeError(
                "reconcile tripwire (a)：env_hint='unknown' 的候選 %r 竟 reconcilable=True"
                "（classifier bug：unknown 不可刪）→ fail-loud abort。" % (c.id,))
        if c.env_hint != pinned_env:
            raise RuntimeError(
                "reconcile tripwire (b)：cross-env 候選 %r（env_hint=%s != pinned=%s）竟 "
                "reconcilable=True（別 env 資源不歸我刪）→ fail-loud abort。"
                % (c.id, c.env_hint, pinned_env))
        if shared_tunnel_id and c.id == shared_tunnel_id:
            raise RuntimeError(
                "reconcile tripwire (c)：shared tunnel 本體 %r 竟 reconcilable=True"
                "（永不刪 shared tunnel 本體）→ fail-loud abort。" % (c.id,))


def classify_candidates(result, *, pinned_env, shared_tunnel_id):
    """對 compute_orphans 的 raw 候選做 env attribution + reconcilable predicate + protected 標記。

    每筆：① ``env_hint`` 由 name 的 env token 判（``classify_env_hint``）② ``reconcilable`` 走結構
    predicate（首版恆 False）③ **protected-override（在 predicate 之後）**：id == operator-reported
    ``shared_tunnel_id`` → 標 protected、reconcilable=False（避免人照報表刪 shared tunnel 本體）
    ④ reason 區分 protected / cross-env / unknown(prod·legacy 不可分) ⑤ access_policy 補 resync
    漂移人工核對註記（C.2：resync 重建 policy → 新 cf_policy_id 活躍卻不在左集 → 誤標孤兒）。
    最後跑 tripwire。**保留全部候選**（不 silent drop；cross-env/protected 靠 reconcilable=False
    達成 skip，報表層須全列呈現、不得以 reconcilable 過濾 = no-silent-drop invariant）。
    """
    for c in result.orphan_candidates:
        if c.kind == 'namespace':
            # #277 ns 名（paas-ws-*）不符 classify_env_hint 的 paas-{env}-{sa|sm|cs} 格式 → 另判；
            # 走 _ns_env_hint 後 is_cross 由 env_hint 與 pinned_env 推得（與 classify_env_hint 對稱）。
            env_hint = _ns_env_hint(c.name)
            is_cross = env_hint != 'unknown' and env_hint != pinned_env
        else:
            env_hint, is_cross = classify_env_hint(c.name, pinned_env)
        c.env_hint = env_hint
        c.reconcilable = _is_reconcilable(c, env_hint, pinned_env)

        if shared_tunnel_id and c.id == shared_tunnel_id:
            # protected-override：shared tunnel 本體永不刪（只清指向它的 DNS/ingress rule）。
            c.reconcilable = False
            c.reason = ('protected：operator 回報的 shared tunnel 本體，永不刪'
                        '（只清指向它的 DNS / ingress rule，不刪 tunnel 本體）')
        elif is_cross:
            c.reason = ('cross-env：屬 %s、非本 (%s) env 孤兒；reconcilable=False、不動作'
                        '（別 env 的 reconciler 自會處理）' % (env_hint, pinned_env))
        elif env_hint == 'unknown':
            # reason 拼接硬化：real pipeline 經 compute_orphans 必非空，但防未來繞過直餵空 reason。
            _note = ('env_hint=unknown（prod / legacy 無 env token、byte-identical 不可分，不瞎猜 '
                     '→ 人工審；prod 確認須 DP-DIFF prod-DB-diff）')
            c.reason = (c.reason + '；' + _note) if c.reason else _note

        if c.kind == 'access_policy':
            c.reason += ('；policy 可能為 resync 漂移後仍活躍（DB cf_policy_id 漂移 → 不在左集 → '
                         '誤標孤兒），人工刪前須核對 CF app 實際引用（C.2）')

    _run_tripwires(result.orphan_candidates, pinned_env=pinned_env,
                   shared_tunnel_id=shared_tunnel_id)
    return result


def _parse_inventory(inventory):
    """驗 operator inventory body shape，malformed → fail-loud raise；
    回 ``(resources, partial, paas_env, shared_tunnel_id)``。

    HTTP status fail-loud（``_request``）只守『門』；body shape 是沒守的『窗』——operator 回
    200 但 body 是**缺鍵的 dict**（gateway/auth-proxy 的 ``{"detail": ...}`` envelope、欄位改名、
    半序列化）會被 ``.get(key, default)`` 靜默變成 ``partial=False`` + 空 ``resources`` = 假性
    『掃完、無孤兒』（正是本工具要防的「退化掃描被當完整」）。故缺鍵 / 型別不符一律 raise，
    不 default 成『安全/完整』值。

    必填鍵含 **``paas_env``**（兩端一致比對用）與 **``shared_tunnel_id``**（標 shared tunnel
    本體 protected 用）—— 兩者缺漏同樣 fail-loud（與其自身『缺鍵不 default 成安全值』哲學一致，
    且 ``shared_tunnel_id`` 缺漏會讓 shared tunnel 本體無從保護 = 災難）。另：``failed_kinds``
    任一非空 → 視為 partial（即使 operator 的 ``partial`` 旗標防禦性地為 False，也不假設其內部一致）。
    """
    if not isinstance(inventory, dict):
        raise RuntimeError("operator inventory 非 dict（malformed body）→ fail-loud，不當『無孤兒』。")
    missing = [k for k in ('resources', 'partial', 'paas_env', 'shared_tunnel_id')
               if k not in inventory]
    if missing:
        raise RuntimeError(
            "operator inventory 缺 %s 鍵（malformed body）→ fail-loud；缺鍵與『掃完無孤兒』"
            "無法區分，不 default 成空 / 完整 / 安全值。" % ', '.join(missing)
        )
    resources = inventory['resources']
    if not isinstance(resources, list):
        raise RuntimeError("operator inventory.resources 非 list（malformed body）→ fail-loud。")
    if not all(isinstance(r, dict) for r in resources):
        # 半序列化 / envelope 污染（如 resources=["foo"]）→ clean fail-loud RuntimeError，
        # 不放任 compute_orphans 的 r.get(...) 對 str 觸發未受控 AttributeError。
        raise RuntimeError("operator inventory.resources 含非 dict 元素（malformed body）→ fail-loud。")
    paas_env = inventory['paas_env']
    if not isinstance(paas_env, str):
        raise RuntimeError("operator inventory.paas_env 非 str（malformed body）→ fail-loud。")
    shared_tunnel_id = inventory['shared_tunnel_id']
    if not isinstance(shared_tunnel_id, str):
        raise RuntimeError("operator inventory.shared_tunnel_id 非 str（malformed body）→ fail-loud。")
    partial = bool(inventory['partial']) or bool(inventory.get('failed_kinds'))
    return resources, partial, paas_env, shared_tunnel_id


_PINNED_ENVS = ('prod', 'stg')


def _resolve_pinned_deploy_env(env) -> str:
    """讀 Odoo ``deploy_env``（R3 env-pin fail-closed）：不帶 default、值不在 {prod,stg} → raise。

    **禁** ``get_param(key, 'prod')`` fallback：``res_config_settings`` / ``smart_home`` /
    ``security_access`` 既有讀取點都帶 ``,'prod'`` default → 「漏設」會偽裝成「成功讀 prod」→
    naive self-check 的 abort 分支永遠跑不到（su-env-exempt-false-green 同家族）。故此處
    ``get_param`` **不帶 default**（缺值回 falsy ∉ {prod,stg} → raise）。
    """
    deploy_env = env['ir.config_parameter'].sudo().get_param('woow_paas_platform.deploy_env')
    if deploy_env not in _PINNED_ENVS:
        raise RuntimeError(
            "L4 reconcile env-pin fail-closed：Odoo deploy_env 未鎖定為 prod|stg（目前 %r）→ "
            "拒絕在無法判定環境時反向對帳（避免跨環境誤判 / 誤刪）。" % (deploy_env,)
        )
    return deploy_env


def reconcile_orphans(env, client=None) -> ReconcileResult:
    """端到端：env-pin self-check + 取右集 inventory + 兩端一致 + 蒐左集 + 差集 → dry-run 候選。

    - **① R3 env-pin self-check（最先、fail fast）**：Odoo ``deploy_env`` 未鎖定 prod|stg → abort
      零候選（在打 operator / 蒐左集前擋下）。
    - client 未設定（operator_url / api_key 缺）→ raise，**不**降級回空候選（空候選會被當
      『無孤兒』，與『未設定 / 沒掃到』無法區分，B.0 fail-loud）。
    - ``client.get_reconcile_inventory()`` 對 404 / 503 / 連線 fail-loud raise；body malformed
      由 ``_parse_inventory`` fail-loud raise（不靜默變空 / 完整）。
    - **② F-ENV-1b 両端一致**：``inventory.paas_env`` 必須 == Odoo ``deploy_env``，否則部署接線
      錯置（Odoo 打到別環境的 operator）→ abort。此 check 語意是「兩端 env 標籤一致」，**非**
      資源範圍/外洩防護（shared CF account 下 operator 不依 env 過濾資源，cross-env 資源照常出現，
      那是分類層的事）。
    - **③ ``shared_tunnel_id`` 必填非空**：operator 報不出自己掛的 shared tunnel → 無法保證
      shared tunnel 本體不被誤判孤兒 → fail-closed abort（不靜默放行）。
    - **先取右集（inventory）再蒐左集**：provision 是「先建 CF → 再寫 Odoo stored-id」，左集讀得
      越晚、Odoo 寫回越有時間落地，越能縮短「CF 已建、id 未回填」的 in-flight 資源被誤判窗。
    """
    deploy_env = _resolve_pinned_deploy_env(env)        # ① env-pin（fail fast）
    if client is None:
        client = get_paas_operator_client(env)
    if client is None:
        raise RuntimeError(
            "PaaS Operator 未設定（operator_url / operator_api_key）→ 無法反向對帳；"
            "fail-loud，不降級回空候選（空候選會被當『無孤兒』）。"
        )
    inventory = client.get_reconcile_inventory()        # 右集 T1（先讀）
    resources, partial, paas_env, shared_tunnel_id = _parse_inventory(inventory)  # body fail-loud
    if paas_env != deploy_env:                          # ② 両端一致
        raise RuntimeError(
            "L4 reconcile 兩端 env 不一致：Odoo deploy_env=%r 但 operator inventory.paas_env=%r"
            "（部署接線錯置：Odoo 打到別環境的 operator）→ abort，避免用錯『我的 env』分類。"
            % (deploy_env, paas_env)
        )
    if not shared_tunnel_id:                            # ③ shared_tunnel_id 必填非空
        raise RuntimeError(
            "L4 reconcile：operator inventory.shared_tunnel_id 為空 → 無法保證 shared tunnel "
            "本體不被誤判孤兒 → abort（不靜默放行）。"
        )
    left_set = collect_left_set(env)                    # 左集 T2（後讀，縮 in-flight 誤判窗）
    result = compute_orphans(resources, left_set, partial=partial)        # diff（raw 候選）
    # 分類層：env attribution + 結構 allowlist + protected（shared tunnel 本體）+ tripwire。
    # in-flight 一級誤刪向量（先建 CF→再寫 Odoo id）首版靠「reconcilable 結構恆 False」擋住刪除；
    # env attribution 會給 in-flight 活躍資源 confident env_hint，但結構 predicate 不據此升 True。
    return classify_candidates(result, pinned_env=deploy_env, shared_tunnel_id=shared_tunnel_id)
