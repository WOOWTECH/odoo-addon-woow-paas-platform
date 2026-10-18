# -*- coding: utf-8 -*-
"""INFRA-FIX-019-1：租戶憑證不進 helm release 歷史（純函式，不碰 ORM／operator）。

## 缺陷

helm 把**每個** revision 的 user values 與渲染後的 manifest 存在
``sh.helm.release.v1.<release>.v<N>`` Secret。租戶在服務設定填的 ngrok authtoken
（headscale／emqx 的 ``ngrok.authtoken``）與 Tailscale auth key（``tailscale.authKey``）
原本照一般 values 送進 helm ⇒ 租戶清除或換掉之後，舊值仍完整留在每個 superseded
revision（Headscale 238 實測：v19／v21／v22／v25 仍是 49 字元 token）。

## 修法

範本在 ``helm_value_specs`` 的 password 欄位標 ``secret_ref``::

    {"key": "ngrok.authtoken", "type": "password", ...,
     "secret_ref": {"key": "ngrok_authtoken", "since_chart": "0.1.7"}}

部署時（chart 版本 ≥ ``since_chart``）平台把這些值從 values 拿出來：

* values 裡原路徑改成 ``""``（明寫，不是刪掉：任何沿用舊 values 的路徑都蓋得掉舊值）；
* values 加 ``tenantSecret = {name, revision, nonEmptyKeys}``——chart 只引用 Secret 名稱
  （woow-paas-charts headscale 0.1.7、tailscale 0.1.8、emqx 0.1.11 的契約）；
* 值本身另外交給 operator（``tenant_secret = {name, data}``），operator 在 helm 之前寫進
  ``<release>-tenant-secret``（不屬於 release 的 Secret，server-side apply）。

``revision`` 讓 chart 在值變了時重啟 pod（agent 只在啟動時讀 token）。它是**以平台密鑰
做的 HMAC**，不是 token 的 sha256：manifest 也存在 release 歷史裡，純雜湊就是一枚可以
拿來比對的 token 指紋（INFRA-FIX-019 的稽核正是用 sha256 前 12 碼辨識 token）。

chart 版本低於 ``since_chart``（還沒升上新 chart 的既有服務、reset 釘住舊版）照舊走 values，
否則舊 chart 讀不到 Secret、token 會被清成空。

已經寫進舊 revision 的值清不掉：撤銷後仍要到 ngrok 後台輪換 token、到 Tailscale／Headplane
撤銷 key（範本說明與 chart values 註解都寫明）。
"""
import copy
import hashlib
import hmac
import json
import re

from .chart_version import chart_version_not_lower

#: ``<release>-tenant-secret``——與 operator（``extra/paas-operator/src/services/tenant_secret.py``）
#: 及 chart（``tenantSecret.name``）的約定；operator 只接受這個名稱。
TENANT_SECRET_SUFFIX = '-tenant-secret'
#: chart values 裡的鍵（woow-paas-charts headscale／tailscale／emqx）。
TENANT_SECRET_VALUES_KEY = 'tenantSecret'

_SECRET_KEY_RE = re.compile(r'[-._A-Za-z0-9]{1,253}')
_SPEC_KEY_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*')
_SEMVER_RE = re.compile(r'\d+\.\d+\.\d+')


def tenant_secret_name(release):
    return f'{release}{TENANT_SECRET_SUFFIX}'


def secret_ref_problems(spec):
    """一個 spec 的 ``secret_ref`` 哪裡不合格（空串列＝合格或沒有 ``secret_ref``）。"""
    if not isinstance(spec, dict) or 'secret_ref' not in spec:
        return []
    ref = spec.get('secret_ref')
    problems = []
    if spec.get('type') != 'password':
        problems.append('secret_ref only belongs on a password-type spec')
    if not isinstance(spec.get('key'), str) or not _SPEC_KEY_RE.fullmatch(spec.get('key') or ''):
        problems.append('spec key must be a dotted helm path')
    if not isinstance(ref, dict):
        return problems + ['secret_ref must be an object']
    if not isinstance(ref.get('key'), str) or not _SECRET_KEY_RE.fullmatch(ref.get('key') or ''):
        problems.append('secret_ref.key must be a Secret data key')
    if not isinstance(ref.get('since_chart'), str) or not _SEMVER_RE.fullmatch(ref.get('since_chart') or ''):
        problems.append('secret_ref.since_chart must be a chart version (x.y.z)')
    return problems


def secret_refs(specs, chart_version):
    """``specs`` 中、對 ``chart_version`` 生效的 ``(helm 路徑, Secret 鍵)``。

    ``chart_version`` 空或讀不懂 ⇒ 一律不生效（fail-safe：照舊走 values，至少 token 送得到
    chart）。不合格的 ``secret_ref`` 也不生效（由範本測試守線，不在部署當下爆）。
    """
    if not chart_version or not isinstance(specs, dict):
        return []
    refs = []
    for group in ('required', 'optional'):
        for spec in specs.get(group) or []:
            if not isinstance(spec, dict) or 'secret_ref' not in spec or secret_ref_problems(spec):
                continue
            ref = spec['secret_ref']
            if chart_version_not_lower(chart_version, ref['since_chart']):
                refs.append((spec['key'], ref['key']))
    return refs


def has_secret_refs(specs):
    """範本是否宣告了任何 ``secret_ref``（不論版本）——回復版本要保留 ``tenantSecret``。"""
    if not isinstance(specs, dict):
        return False
    return any(
        isinstance(spec, dict) and 'secret_ref' in spec
        for group in ('required', 'optional') for spec in specs.get(group) or [])


def _get(values, path):
    node = values
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set(values, path, value):
    keys = path.split('.')
    node = values
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    node[keys[-1]] = value


def _normalise(value):
    """與 chart 舊路徑相同的整理：前後空白（貼上時多的換行）去掉；非字串／缺席＝空。"""
    if value is None or value is False:
        return ''
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def split_tenant_secret(values, refs, release, revision_key):
    """把 ``refs`` 指的值從 ``values`` 拿出來。回傳 ``(新 values, operator 的 tenant_secret)``。

    ``refs`` 空 ⇒ ``(values, None)``（原物件、不複製）。``revision_key``：平台密鑰（bytes／str），
    不可為空——沒有密鑰就做不出不可比對的 revision，寧可 raise 也不退回純雜湊。
    """
    if not refs:
        return values, None
    if not revision_key:
        raise ValueError('tenant secret revision needs a platform key')
    if isinstance(revision_key, str):
        revision_key = revision_key.encode('utf-8')
    result = copy.deepcopy(values) if values else {}
    data = {}
    for path, secret_key in refs:
        data[secret_key] = _normalise(_get(result, path))
        _set(result, path, '')
    name = tenant_secret_name(release)
    digest = hmac.new(
        revision_key,
        json.dumps({'name': name, 'data': data}, sort_keys=True, separators=(',', ':')).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    result[TENANT_SECRET_VALUES_KEY] = {
        'name': name,
        'revision': digest[:24],
        'nonEmptyKeys': sorted(key for key, value in data.items() if value),
    }
    return result, {'name': name, 'data': data}
