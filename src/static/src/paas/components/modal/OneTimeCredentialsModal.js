/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

/**
 * 一次性憑證視窗：列出一個或多個具名憑證，每個可獨立複製。
 *
 * #530：原本是單值設計（單一 password 字串 prop ＋ 寫死的單句文案）。Hermes Agent
 * 建立與 Reset 會同時交付 Admin Password 與 Gateway API Key，兩者用途不同、要填進
 * 不同欄位——擠在同一個輸入框會讓租戶把 dashboard 密碼填進 Gateway API Key 欄位，
 * 而失敗形狀（MCP 的連線測試只回一個紅點）完全看不出是填錯了哪一個。
 */
export class OneTimeCredentialsModal extends Component {
    static template = "woow_paas_platform.OneTimeCredentialsModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        // [{ label, value, hint? }]；順序即顯示順序
        credentials: { type: Array, element: Object },
        onClose: { type: Function },
        // "provision"（開通頁：服務第一次啟動）或 "reset"（服務頁：Reset／自設憑證後重啟）。
        // 兩者的狀態說明不同——開通時對租戶說「重啟中」是錯的（服務從沒跑過），
        // 反之亦然。預設 "reset" 以保持既有呼叫端行為。
        // "in_place"（#1370：Odoo／n8n 的 Reset 直接改 app DB，不重啟、立刻生效）。
        mode: { type: String, optional: true, validate: (m) => ["provision", "reset", "in_place"].includes(m) },
        // in_place 的後果依 app 而異（鏡射 template.app_admin_reset_kind）：Odoo／n8n 會登出
        // 舊 session 並關 2FA；Omnigent（omnigent_exec）與上游 Members → Reset 一樣，不登出；
        // EMQX（emqx_exec，#1774）登出已登入的 Dashboard，但開源版沒有 2FA 可關。
        appResetKind: { type: String, optional: true },
    };
    static defaultProps = { mode: "reset" };

    setup() {
        // 每列各自記錄「已複製」——共用一個布林的話，按下第一顆之後所有列都顯示
        // Copied，租戶會以為兩個值都複製過了而只貼上其中一個。
        this.state = useState({ copiedIndex: null });
    }

    // 文案放在 JS（_t 單行字串）而不是模板的多行文字節點：模板文字的 msgid 會連同換行
    // 與縮排一起抽出，排版一動翻譯就對不上（這段 zh_TW 就是這樣一直是空的）。
    get title() {
        return _t("Save your credentials");
    }

    get intro() {
        return _t("These values are shown only once. Save them before closing this dialog.");
    }

    /**
     * #461：這段狀態說明是正確性的一部分，不是裝飾——「成功」＝憑證已是叢集權威值，
     * 但服務要（重新）啟動才會吃到它。少了這句，租戶會拿著正確的憑證撞一個還沒起來
     * 的服務，並回報成「密碼不對」。
     */
    get statusHint() {
        if (this.props.mode === "in_place" && this.props.appResetKind === "omnigent_exec") {
            return _t("The new password works right away; the service does not restart. Devices that are already signed in stay signed in.");
        }
        if (this.props.mode === "in_place" && this.props.appResetKind === "emqx_exec") {
            return _t("The new password works right away; the service does not restart. Anyone signed in to the dashboard with the old password is signed out.");
        }
        if (this.props.mode === "in_place") {
            return _t("The new password works right away; the service does not restart. Devices signed in with the old password are signed out, and two-factor sign-in has been turned off — turn it on again after you sign in.");
        }
        if (this.props.mode === "provision") {
            return _t("The service is starting for the first time (usually 1–3 minutes). Sign in with these values once its status shows Running.");
        }
        return _t("The service is restarting to apply them (usually 1–3 minutes) and may be briefly unreachable. The status will return to Running when it is ready.");
    }

    async copyValue(index) {
        await navigator.clipboard.writeText(this.props.credentials[index].value);
        this.state.copiedIndex = index;
    }

    onBackdropClick(event) {
        if (event.target === event.currentTarget) {
            this.props.onClose();
        }
    }
}

/**
 * 把建立服務／Reset 的回應轉成一次性憑證清單（順序即顯示順序）。
 *
 * #530：收斂在這裡而非各頁自己組——兩個入口交付同一組憑證，各寫一份會讓文案、
 * 順序與「有沒有帶 gateway」的判斷漂移，而租戶只會看到其中一邊。
 *
 * ⚠ 網址一律用後端給的值（`gateway_url` / `dashboard_url`），**前端不拼字串**：
 * gateway 的子路徑是 chart 的值（seed 的 gateway.externalPath），在前端再拼一次
 * 就等於多一份會與 chart 無聲漂移的副本，而漂移的結果是一條看起來正常的死鏈。
 * dashboard 同理——**不要拿 `gateway_url` 去掉 `/gateway` 反推**，那樣兩條網址
 * 會被綁成同一個假設，而它們的形態刻意不同（見下方 hint）。
 *
 * （chart 0.1.3 起 hermes-agent 移除 webui container，nginx `/` 直連 dashboard；
 * 憑證的對應入口只剩 console 與 dashboard 兩個，admin_password 仍是同一組）
 *
 * #672：dashboard 這一格原本整個沒有交付。MCP 的「Dashboard REST API」區要填
 * URL＋帳號＋密碼三格，平台只交付了密碼，而三格缺一格＝MCP 管理面整組失效。
 * 缺的不只是值，也是**指路**——原本只有 Gateway API URL 的 hint 寫了「填進 MCP
 * 哪一區」，Admin Password 沒寫，於是租戶只把有指路的那一格填進 MCP。
 */
export function buildOneTimeCredentials(data) {
    const credentials = [];
    // #1370：app 自管、平台以 operator exec 重設的模板（Odoo／n8n，
    // template.app_admin_reset_kind）。開通（Odoo 的初始密碼）與 Reset 共用這段：
    // 帳號是 app 的登入帳號（Odoo login／n8n owner email），忘記時回服務頁 Reset。
    // 判準鏡射後端欄位，不用 slug；必須排在 admin_reset_supported 分支之前——這類模板
    // 的 admin_reset_supported 為 true，落到下方會變成 hermes 的 MCP 說明。
    if (data && data.admin_password && data.template
            && data.template.app_admin_reset_kind
            && data.template.app_admin_reset_kind !== "none") {
        // Omnigent／EMQX 的帳號由 chart 固定（admin）；後端沒帶時沿用 app_managed 分支的同一個預設。
        const chartFixedAdmin = ["omnigent_exec", "emqx_exec"].includes(data.template.app_admin_reset_kind);
        const username = data.admin_username || (chartFixedAdmin ? "admin" : null);
        if (username) {
            credentials.push({
                label: _t("Username"),
                value: username,
                hint: _t("Signs in to this service, together with the password below."),
            });
        }
        credentials.push({
            label: _t("Admin Password"),
            value: data.admin_password,
            hint: _t("If you forget it, use Reset Admin Password on the service page to issue a new one."),
        });
        return credentials;
    }
    // 帳號可由租戶自設的模板（後端 admin_username 非空，例 open-design）：帳號不一定是
    // admin，連同帳號一起交付，並用那個模板的入口（app ＋ web console）說明。其餘模板
    // 後端回 null，沿用下方原本的文案。
    // app_managed 的模板（omnigent，template.admin_reset_supported === false）：平台只
    // 供給初始密碼，之後密碼由 app 自己保管、平台重設不了——文案不能指向 Admin
    // Credentials，也不能套下方 hermes 的 MCP 說明。帳號由 chart 固定（admin）。
    if (data && data.admin_password && data.template
            && data.template.admin_reset_supported === false) {
        credentials.push({
            label: _t("Username"),
            value: data.admin_username || "admin",
            hint: _t("Signs in to this service, together with the password below."),
        });
        credentials.push({
            label: _t("Initial Password"),
            value: data.admin_password,
            hint: _t("Change it inside the app after you sign in. The platform cannot reset it later."),
        });
    } else if (data && data.admin_password && data.admin_username) {
        credentials.push({
            label: _t("Username"),
            value: data.admin_username,
            hint: _t("Signs in to this service (and its web console, if it has one), together with the password below."),
        });
        credentials.push({
            label: _t("Admin Password"),
            value: data.admin_password,
            hint: _t("You can change the username and password later from Admin Credentials on the service page."),
        });
    } else if (data && data.admin_password && data.template
            && data.template.admin_credentials_settable === true) {
        // 只有密碼、沒有帳號的服務（code-server）：可 Reset 也可自設。不能落到下方
        // hermes 的 MCP 說明（username: admin 在這裡是錯的）。
        credentials.push({
            label: _t("Password"),
            value: data.admin_password,
            hint: _t("Signs in to this service (there is no username). You can reset it or set your own later from Admin Credentials on the service page."),
        });
    } else if (data && data.admin_password && data.template
            && data.template.admin_password_carrier === "config_json") {
        // #1542：mcp-server 家族（8 個 mcp-*，config_json 載體）。後台只有一組密碼、沒有
        // 帳號，也沒有 console／dashboard——不能落到下方 hermes-agent 的說明。登入網址由
        // 後端給（`admin_url`），沒有對外網址時就只列密碼。
        credentials.push({
            label: _t("Admin Password"),
            value: data.admin_password,
            hint: _t("Signs in to the MCP admin page (password only, no username). If you forget it, use Reset Admin Password on the service page to issue a new one."),
        });
        if (data.admin_url) {
            credentials.push({
                label: _t("Admin Page URL"),
                value: data.admin_url,
                hint: _t("Next: open this page, sign in with the password above, and fill in the connection settings under Connection. The MCP endpoint for your AI client is on the Tokens page."),
            });
        }
    } else if (data && data.admin_password) {
        // hermes-agent（helm_secret 載體、有 gateway／dashboard）的說明。
        credentials.push({
            label: _t("Admin Password"),
            value: data.admin_password,
            hint: _t(
                "Shared by the console and the dashboard (username: admin). Also enter it in the Dashboard REST API section of the MCP connection settings, together with the Dashboard API URL below."
            ),
        });
    } else if (data && data.gateway_api_key && data.admin_username) {
        // 租戶自填密碼（後端不回傳明文）、但 gateway key 換發了（例 LiteLLM 的 master
        // key）：視窗只為交付新 key 而開。帳號不是敏感值，順帶列出方便對照；密碼格整個
        // 不出現（不是空格），提示也不能寫「下方密碼」——這裡沒有密碼。
        credentials.push({
            label: _t("Username"),
            value: data.admin_username,
            hint: _t("Signs in to this service, together with the password you set."),
        });
    }
    // P3：gateway 兩格的提示依範本（後端 `gateway_api_key_hint`／`gateway_url_hint`，
    // 範本的資料值、seed 原文）。沒帶時沿用 hermes-agent 的 MCP 說明（程式碼字串，走 po）——
    // 其他有 gateway 的模板（例 LiteLLM：master key ＋ OpenAI 相容 Base URL）會帶自己的。
    if (data && data.gateway_api_key) {
        credentials.push({
            label: _t("Gateway API Key"),
            value: data.gateway_api_key,
            hint: data.gateway_api_key_hint
                || _t("Enter this in the Gateway API Server section of the MCP connection settings."),
        });
    }
    if (data && data.gateway_url) {
        credentials.push({
            label: _t("Gateway API URL"),
            value: data.gateway_url,
            hint: data.gateway_url_hint || _t(
                "Enter this in the Gateway API Server section of the MCP connection settings. This is the full address including its sub-path; the service root URL on its own will not connect. This applies to this field only — the Dashboard API URL below is the opposite and needs the root URL with no sub-path."
            ),
        });
    }
    if (data && data.dashboard_url) {
        credentials.push({
            label: _t("Dashboard API URL"),
            value: data.dashboard_url,
            hint: _t(
                "Enter this in the Dashboard REST API section of the MCP connection settings (username: admin, password: the Admin Password above). This is the service root URL, with no sub-path."
            ),
        });
    }
    return credentials;
}
