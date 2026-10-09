# 服務品牌圖示

/woow 各處的服務圖示（市集卡片、設定頁、工作區服務卡片、服務詳情、訂閱列表、智慧家庭與安全存取）
改用各軟體的原生品牌圖示（使用者 2026-10-09 決定）：

- MCP 伺服器用它所連軟體的圖示（例：Odoo MCP Server 用 Odoo 圖示）。
- 智慧家庭連結用 WoowTech 藍色商標；安全存取用 Cloudflare。
- 圖示與 HA App Store／MCP add-on 盡量一致：商店已有的直接取用（逐位元相同），商店沒有的取自各軟體官方 repo。

對照表在 `static/src/paas/utils/brand_icons.js`（模板代碼 → 品牌），顯示元件是
`static/src/paas/components/brand-icon/BrandIcon`；沒登記的模板退回原本的分類圖示。

圖示與商標屬於各軟體的權利人，這裡只用來標示服務所使用的軟體；本平台與這些軟體的開發者沒有隸屬關係。

| 檔案 | 品牌 | 用在 | 來源 | sha256 |
|---|---|---|---|---|
| `odoo.png` | Odoo | Odoo、Odoo MCP Server、Odoo Manage MCP | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/odoo18ce/icon.png) `odoo18ce/icon.png` | `ea79692f3e825368f68911699cf56ef76b690a9e50cb0d2518725cb3d41b5571` |
| `n8n.svg` | n8n | n8n、n8n MCP Server | [n8n-io/n8n `bc0a9cb`](https://github.com/n8n-io/n8n/blob/bc0a9cb/packages/frontend/@n8n/design-system/src/components/N8nLogo/logo-icon.svg) `N8nLogo/logo-icon.svg`（商店那張只有 48×48，改用官方向量檔） | `fea58f324bd105a5d8a054e6cb80940c1e63fc51b606b780b73e9187e5bfdfa9` |
| `nextcloud.png` | Nextcloud | Nextcloud、MCP：Nextcloud | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow-nextcloud-office/icon.png) `woow-nextcloud-office/icon.png` | `c1be2c912b0d4c80fedbfbb105b8659219dbe9b0665bd71a93b61ad1323b21f5` |
| `litellm.png` | LiteLLM | AI 模型閘道（LiteLLM）、MCP：AI 模型閘道（LiteLLM） | [BerriAI/litellm `a6f6c64`](https://github.com/BerriAI/litellm/blob/a6f6c64b6ec37782d871b7cda2f2fd0d9202d85c/litellm/proxy/logo_monogram.png) `litellm/proxy/logo_monogram.png`（與 Woow LiteLLM MCP Server add-on 同一張） | `5903e2fcc009a17c7f518db364fafccdca23192206419966585039ffbb635df0` |
| `hermes.png` | Hermes Agent | Hermes Agent、Hermes MCP Server | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow-hermes/icon.png) `woow-hermes/icon.png` | `799c862dca5cb00213ef6c1a61ffb22909d0d9b4ef7cf25ff7a5a03f8dbde9e7` |
| `opendesign.png` | Open Design | Open Design、Open Design MCP | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow_ha_opendesign/icon.png) `woow_ha_opendesign/icon.png` | `4b1c8f1948898dbe7de912c84cf98a24d9166c080184cf49343c203b73e04d0c` |
| `kubernetes.svg` | Kubernetes | Kubernetes MCP | [kubernetes/kubernetes `fa03e68`](https://github.com/kubernetes/kubernetes/blob/fa03e68/logo/logo.svg) `logo/logo.svg` | `a8f6c3b81e588eb0eee4b5ae707ad11b16d0c5ce132719871e559eed0e61e6bf` |
| `vibe-kanban.png` | Vibe Kanban | Vibe Kanban MCP | [BloopAI/vibe-kanban `d5cbb53`](https://github.com/BloopAI/vibe-kanban/blob/d5cbb53/crates/tauri-app/icons/128x128@2x.png) `crates/tauri-app/icons/128x128@2x.png` | `1fcc4f128cd3bee0cc92ba7efff0b6cef83466cf4860606f645fa169e460a228` |
| `home-assistant.png` | Home Assistant | Home Assistant 完整版、Home Assistant OS | [home-assistant/brands `c3b8287`](https://github.com/home-assistant/brands/blob/c3b8287/core_integrations/_homeassistant/icon@2x.png) `core_integrations/_homeassistant/icon@2x.png` | `b6528267eb85d9662bbf765cbd4fa13627b2169459a9ce93c71691fe619b7767` |
| `emqx.png` | EMQX | EMQX | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/emqx/icon.png) `emqx/icon.png` | `0ca9e6cf4c66ae5fe009562f49434b96a5f019629fa9b2399ee05d65331673f9` |
| `omnigent.png` | Omnigent | Omnigent | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/omnigent/icon.png) `omnigent/icon.png` | `33ac41168fc6f3e0eddc8f92fb900dcf218b5d28e2ad18edfd1d55a71db50ec4` |
| `pi.png` | pi | pi Coding Agent | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow_ha_pi_agent/icon.png) `woow_ha_pi_agent/icon.png` | `9f7d0ca820a8ff1d5d6d7048a372077676df016450561137a696033b4d194dcf` |
| `code-server.png` | Code Server | Code Server | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow_ha_code_server/icon.png) `woow_ha_code_server/icon.png`（使用者選商店這張，不用 Coder 官方的「C▮」） | `877ecb59982c85609e2a6f66d939432556d19c0fc83366c7b35ef9bc60297160` |
| `headscale.png` | Headscale | VPN 控制台（Headscale） | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/headscale/icon.png) `headscale/icon.png` | `cee610db3cd93520e0dc31bf419610644c0175030a7680ae6880d545d2ba1c17` |
| `tailscale.png` | Tailscale | VPN 節點（相容 Tailscale） | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow-tailscale/icon.png) `woow-tailscale/icon.png` | `8ad380161cf9a018c1bf390fe30c93da5836f933216d16b7fa45e3e6bba3af3b` |
| `cloudflare.png` | Cloudflare | 安全存取（遠端安全連結） | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/cloudflared/icon.png) `cloudflared/icon.png` | `1a42122b16eef226c371006be3b451bfb09d6d043cd0aa3c19f83fdee72bbe2c` |
| `woowtech.png` | WoowTech | 智慧家庭連結 | [HA App Store](https://github.com/WOOWTECH/Woow_HA_App_Store/blob/0c4c21f/woow_ha_core_1/icon.png) `woow_ha_core_1/icon.png`（WoowTech 藍色商標，使用者指定） | `53e23fbcd9d8b55c62351971f04712ff001ed28a946adc233de841974223cd7b` |

換圖示時：照上表重新取檔、更新雜湊；新增服務時在 `brand_icons.js` 的 `TEMPLATE_BRANDS` 補上模板代碼。
