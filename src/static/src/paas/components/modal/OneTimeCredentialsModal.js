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
    };

    setup() {
        // 每列各自記錄「已複製」——共用一個布林的話，按下第一顆之後所有列都顯示
        // Copied，租戶會以為兩個值都複製過了而只貼上其中一個。
        this.state = useState({ copiedIndex: null });
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
    // 帳號可由租戶自設的模板（後端 admin_username 非空，例 open-design）：帳號不一定是
    // admin，連同帳號一起交付，並用那個模板的入口（app ＋ web console）說明。其餘模板
    // 後端回 null，沿用下方原本的文案。
    if (data && data.admin_password && data.admin_username) {
        credentials.push({
            label: _t("Username"),
            value: data.admin_username,
            hint: _t("Signs in to the app and the web console, together with the password below."),
        });
        credentials.push({
            label: _t("Admin Password"),
            value: data.admin_password,
            hint: _t("You can change the username and password later from Admin Credentials on the service page."),
        });
    } else if (data && data.admin_password) {
        credentials.push({
            label: _t("Admin Password"),
            value: data.admin_password,
            hint: _t(
                "Shared by the console and the dashboard (username: admin). Also enter it in the Dashboard REST API section of the MCP connection settings, together with the Dashboard API URL below."
            ),
        });
    }
    if (data && data.gateway_api_key) {
        credentials.push({
            label: _t("Gateway API Key"),
            value: data.gateway_api_key,
            hint: _t("Enter this in the Gateway API Server section of the MCP connection settings."),
        });
    }
    if (data && data.gateway_url) {
        credentials.push({
            label: _t("Gateway API URL"),
            value: data.gateway_url,
            hint: _t(
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
