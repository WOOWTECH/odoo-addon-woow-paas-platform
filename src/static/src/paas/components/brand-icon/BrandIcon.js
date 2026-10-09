/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowIcon } from "../icon/WoowIcon";
import { brandIconUrl } from "../../utils/brand_icons";

/**
 * 服務圖示：有品牌圖檔就顯示品牌圖示，否則退回 Material 圖示（`fallback`）。
 *
 * `fill`：放在既有的彩色圖示底框裡時用——圖示鋪滿底框、白底，蓋掉分類色。
 * 不帶 `fill` 時是行內小圖示，寬高＝`size`。
 */
export class BrandIcon extends Component {
  static template = "woow_paas_platform.BrandIcon";
  static components = { WoowIcon };
  static props = {
    // 品牌鍵（brand_icons.js 的 FILES）；null／undefined＝沒有品牌圖示。
    brand: { optional: true },
    fallback: String,
    size: { type: Number, optional: true },
    fill: { type: Boolean, optional: true },
    class: { type: String, optional: true },
  };

  get url() {
    return brandIconUrl(this.props.brand);
  }

  get imgStyle() {
    if (this.props.fill || !this.props.size) return "";
    return `width: ${this.props.size}px; height: ${this.props.size}px;`;
  }
}
