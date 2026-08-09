/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
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
 * ⚠ 完整 gateway 網址一律用後端給的 `gateway_url`，**前端不拼字串**：子路徑是
 * chart 的值（seed 的 gateway.externalPath），在前端再拼一次就等於多一份會與
 * chart 無聲漂移的副本，而漂移的結果是一條看起來正常的死鏈。
 *
 * （chart 0.1.3 起 hermes-agent 移除 webui container，nginx `/` 直連 dashboard；
 * 憑證的對應入口只剩 console 與 dashboard 兩個，admin_password 仍是同一組）
 */
export function buildOneTimeCredentials(data) {
    const credentials = [];
    if (data && data.admin_password) {
        credentials.push({
            label: "Admin Password",
            value: data.admin_password,
            hint: "console 與 dashboard 共用這一組（帳號 admin）。",
        });
    }
    if (data && data.gateway_api_key) {
        credentials.push({
            label: "Gateway API Key",
            value: data.gateway_api_key,
            hint: "填進 MCP 連線設定的 Gateway API Server 區。",
        });
    }
    if (data && data.gateway_url) {
        credentials.push({
            label: "Gateway API URL",
            value: data.gateway_url,
            hint: "這是完整位址（含子路徑）；只填服務根網址會連不上。",
        });
    }
    return credentials;
}
