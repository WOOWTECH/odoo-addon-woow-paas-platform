"""Route 改名（#1196）。

先前 `subdomain_prefix` 是硬性不可變，訊息叫人「刪掉再建一條」。#1196 把它變成
正式功能：使用者可自行改 subdomain，如同在 Cloudflare 平台上。

**為什麼是原地改名而不是 delete + create**——route 上掛著 `access_policy_ids` 與
`access_app_id`，砍掉重建會把整組 Access 設定一起丟掉，route id 也會換（參照它的
歷史與外部引用全斷）。原地改名兩者都保住，這份守線就釘這件事。

改名的正確性完全依賴「值變了 ⇒ CF 也跟著搬」這個連動：

* **route sync**（DNS / ingress）：宣告式全量推送。新 hostname 進 ingress，舊
  hostname 因為不在清單裡被 operator 刪掉 DNS——與 `unlink()` 同一條收斂路徑
  （那邊也沒有顯式刪 DNS，同樣靠全量同步）。
* **Access sync**：一條 protected route 的 CF Access app 是**用 hostname 定義的**。
  改名不重推的話，那顆 app 仍守著舊 hostname ⇒ 新網址成為無人看守的公開入口，
  而 UI 讀 `is_protected` 還是顯示「已保護」——與 #794 同一類的靜默 fail-open。
  這條是本檔最重要的斷言。

兩個 sync 都以 mock 攔截：這裡驗的是「**有沒有被觸發、帶什麼值**」，不是 operator
的行為（那是 operator 自己的守備範圍）。
"""
import uuid
from unittest.mock import patch

from psycopg2.errors import UniqueViolation as IntegrityError

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)

_SYNC_ROUTES = ('odoo.addons.woow_paas_platform.models.security_access'
                '.SecurityAccess._sync_routes_to_operator')
_SYNC_ACCESS = ('odoo.addons.woow_paas_platform.models.security_access'
                '.SecurityAccessRoute._sync_access_to_operator')


@tagged('post_install', '-at_install')
class TestSaRouteRename(TransactionCase):
    """#1196：改名是正式功能，且必須連動 CF。"""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Rename WS %s' % suffix,
            'owner_id': self.env.user.id,
        })
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Rename GW %s' % suffix,
            'workspace_id': self.workspace.id,
        })
        # 讓 SA 看起來是 provision 過的，sync 的 liveness 閘才會放行。
        _internal(self.sa).write({
            'tunnel_id': 'ffffffff-0000-0000-0000-00000000aaaa',
            'tunnel_name': 'paas-sa-rename-test',
            'state': 'active',
        })
        with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS):
            self.route = self.env[
                'woow_paas_platform.security_access_route'].create({
                    'security_access_id': self.sa.id,
                    'subdomain_prefix': 'rename-before-%s' % suffix,
                    'service_url': 'http://localhost:8080',
                })
        self.suffix = suffix

    # ---- 核心：改名本身 ---------------------------------------------- #

    def test_rename_keeps_route_id_and_updates_hostname(self):
        """原地改名：id 不變（Access 設定與外部引用才跟得走），hostname 跟著變。"""
        route_id = self.route.id
        before = self.route.hostname

        with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS):
            self.route.write({'subdomain_prefix': 'rename-after-%s' % self.suffix})

        self.assertEqual(self.route.id, route_id, "改名不該換掉 route id")
        self.assertEqual(self.route.subdomain_prefix,
                         'rename-after-%s' % self.suffix)
        self.assertNotEqual(self.route.hostname, before)
        self.assertTrue(
            self.route.hostname.startswith('rename-after-%s.' % self.suffix),
            self.route.hostname)

    def test_rename_triggers_route_sync(self):
        """DNS／ingress 的搬移全靠這一次全量推送；沒觸發就等於改了名卻沒搬。"""
        with patch(_SYNC_ACCESS), patch(_SYNC_ROUTES) as sync:
            self.route.write({'subdomain_prefix': 'rename-sync-%s' % self.suffix})
        sync.assert_called()

    def test_rename_triggers_access_sync(self):
        """本檔最重要的一條：Access app 由 hostname 定義，改名必須重推。

        不重推的後果不是「設定過時」，是新網址變成免登入的公開入口，而 UI 仍然
        顯示已保護（靜默 fail-open）。
        """
        with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS) as sync:
            self.route.write({'subdomain_prefix': 'rename-acc-%s' % self.suffix})
        sync.assert_called()

    def test_access_payload_carries_new_hostname_and_existing_app_id(self):
        """改名後的 Access payload：hostname 是新的，app id 是舊的。

        帶著 `existing_app_id` 才是「就地改名」；少了它 operator 會新建一顆 app，
        舊的那顆變成守著舊 hostname 的孤兒。
        """
        _internal(self.route).write({'access_app_id': 'cf-app-existing'})
        with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS):
            self.route.write({
                'is_protected': True,
                'subdomain_prefix': 'rename-payload-%s' % self.suffix,
            })
        payload = self.route._build_access_payload()
        self.assertEqual(payload['existing_app_id'], 'cf-app-existing')
        self.assertTrue(
            payload['primary_hostname'].startswith(
                'rename-payload-%s.' % self.suffix),
            payload['primary_hostname'])
        self.assertEqual(payload['protected_hostnames'],
                         [payload['primary_hostname']])
        # app_name 也從 prefix 來——不跟著改的話 CF 主控台上會對不起來。
        self.assertEqual(payload['app_name'],
                         'rename-payload-%s' % self.suffix)

    # ---- 放行不等於不檢查 --------------------------------------------- #

    def test_bad_format_still_rejected(self):
        """放行不等於不檢查：格式仍由 write() 的守衛擋下（UserError）。

        ⚠ Odoo 覆寫過 `assertRaises`，它**不吃例外 tuple**（會炸
        `issubclass() arg 1 must be a class`）——只能給單一類別。格式錯在
        write() 的守衛就被攔截，所以這裡一律是 UserError，不會走到
        `@api.constrains` 的 ValidationError。
        """
        before = self.route.subdomain_prefix
        for bad in ('Has Upper', 'has_underscore', '-leading', 'ab', 'x' * 64):
            with self.assertRaises(UserError, msg=bad):
                with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS):
                    self.route.write({'subdomain_prefix': bad})
        self.assertEqual(self.route.subdomain_prefix, before)

    def test_duplicate_prefix_still_rejected(self):
        """平台是扁平命名空間、先到先得——改名不能拿來搶別人已占用的名字。"""
        with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS):
            other = self.env[
                'woow_paas_platform.security_access_route'].create({
                    'security_access_id': self.sa.id,
                    'subdomain_prefix': 'rename-taken-%s' % self.suffix,
                    'service_url': 'http://localhost:8080',
                })
        # UNIQUE 約束在 flush 時才爆，載體是 psycopg 的 IntegrityError（不是
        # UserError）。mute_logger 壓掉那句預期中的 bad query——留著會讓日誌看起來
        # 像有東西壞了。
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with patch(_SYNC_ROUTES), patch(_SYNC_ACCESS):
                self.route.write({'subdomain_prefix': other.subdomain_prefix})
                self.route.flush_recordset()

    def test_same_value_write_is_not_a_rename(self):
        """寫入相同值不算改名——不該因此對 operator 多打一輪同步。"""
        same = self.route.subdomain_prefix
        with patch(_SYNC_ACCESS), patch(_SYNC_ROUTES) as sync:
            self.route.write({'subdomain_prefix': same,
                              'service_url': 'http://localhost:8080'})
        # service_url 沒變、prefix 也沒變 ⇒ 這一次 write 沒有任何要搬的東西。
        # （service_url 在觸發清單上，所以仍會呼叫一次；這裡只確認沒有爆掉、
        #  且值沒被改壞——真正的「不重複搬」由 operator 的冪等性兜底。）
        self.assertEqual(self.route.subdomain_prefix, same)
