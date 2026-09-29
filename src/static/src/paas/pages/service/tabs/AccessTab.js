/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { AccessAppCard } from "../../../components/access/AccessAppCard";
import { AddPolicyModal } from "../../../components/modal/AddPolicyModal";
import { cloudServiceAccessService } from "../../../services/cloud_service_access";

// 拍板 5 的「明示後果」。開整站保護會把**所有**訪客（含對外站的匿名流量）擋在
// Cloudflare Access 登入頁外面——這句必須在按下去之前出現，不是事後才發現。
const PROTECT_CONFIRM = {
  title: _t("Turn on Access protection?"),
  message: _t("Once protection is on, every visitor must pass Access authentication before this service's URL will open, including the people who can browse it anonymously today. Only identities matching an Access policy below can get in."),
  confirmLabel: _t("Turn on Access protection"),
};

// props.service.access 缺席時的安全預設（舊快取／list payload 沒有 access 區塊）
// ——一律當作「沒開保護、沒有 policy」，不要讓畫面長出無法對應到伺服器的狀態。
const EMPTY_ACCESS = {
  is_protected: false,
  session_duration: "24h",
  login_google: false,
  login_email_otp: false,
  access_unresolved_idp_types: "",
  // #794 fail-open 旗標：缺席當 false（舊快取沒有這個 key）。列在這裡是為了讓
  // 「形狀＝`_access_to_dict()`」這件事在前端也看得到，不是為了改變行為。
  access_protection_lost: false,
  hostname: "",
  access_policies: [],
};

/**
 * AccessTab Component（Cloud Service detail 的第五顆頁籤，#746）
 *
 * 一台服務＝一個 Cloudflare Access 錨點（拍板 5 的整站粒度），所以這裡只有**一張**
 * AccessAppCard；SA 那邊是 per-route N 張，卡片本體同一個元件。
 *
 * 狀態刻意留在本元件而不是回頭叫父層 `loadService()`：那支會把
 * `state.loading` 設回 true，整頁閃成 "Loading service..." 並把這個頁籤連同展開
 * 狀態一起卸載——每按一次開關閃一次。兩支 access 端點的回應本來就夠用（config
 * 回整包 `_access_to_dict()`，policy 側再 list 一次），不需要重載整台服務。
 *
 * Props:
 *   - service (Object)：CS detail payload（`_format_service(include_details=True)`）
 *   - workspaceId (number)
 */
export class AccessTab extends Component {
  static template = "woow_paas_platform.ServiceAccessTab";
  static components = { WoowCard, WoowIcon, AccessAppCard, AddPolicyModal };
  static props = {
    service: { type: Object },
    workspaceId: { type: Number },
  };

  setup() {
    this.protectConfirm = PROTECT_CONFIRM;
    this.state = useState({
      access: { ...EMPTY_ACCESS, ...(this.props.service.access || {}) },
      expanded: true,
      reordering: false,
      error: null,
      showPolicyModal: false,
      editingPolicy: null,
    });
  }

  get serviceId() {
    return this.props.service.id;
  }

  // 沒有對外主機名就沒有可以掛 Access app 的錨點——後端
  // `_update_service_access_config` 會直接擋下開啟，這裡把開關鎖住，不長出一顆
  // 按了必然失敗的開關。
  get hasHostname() {
    return !!this.state.access.hostname;
  }

  // #794：宣稱受保護、但 Cloudflare 上的 app 已經被拆掉。卡頭副標不能再說
  // 「every visitor must pass Access」（卡片本身另有紅色警示）。
  get protectionLost() {
    return !!this.state.access.access_protection_lost;
  }

  get policyCount() {
    return (this.state.access.access_policies || []).length;
  }

  // ---- config 寫入 ----

  async saveConfig(vals) {
    this.state.error = null;
    const res = await cloudServiceAccessService.updateConfig(
      this.props.workspaceId, this.serviceId, vals);
    if (res.success) {
      this.state.access = { ...EMPTY_ACCESS, ...res.data };
    } else {
      this.state.error = res.error;
    }
  }

  onProtectedChange(checked) {
    // 剛開啟保護時把卡片展開，設定就在眼前。
    if (checked) {
      this.state.expanded = true;
    }
    this.saveConfig({ is_protected: checked });
  }

  onLoginToggle(method) {
    this.saveConfig({ [method]: !this.state.access[method] });
  }

  onSessionChange(value) {
    this.saveConfig({ session_duration: value });
  }

  toggleExpand() {
    this.state.expanded = !this.state.expanded;
  }

  // ---- policy ----

  async reloadPolicies() {
    const res = await cloudServiceAccessService.fetchPolicies(
      this.props.workspaceId, this.serviceId);
    if (res.success) {
      this.state.access = { ...this.state.access, access_policies: res.data };
    } else {
      this.state.error = res.error;
    }
  }

  openAddPolicy() {
    this.state.editingPolicy = null;
    this.state.showPolicyModal = true;
  }

  openEditPolicy(policy) {
    this.state.editingPolicy = policy;
    this.state.showPolicyModal = true;
  }

  closePolicyModal() {
    this.state.showPolicyModal = false;
  }

  // AddPolicyModal 的注入點：modal 只知道「存一筆 policy」這件事，不知道它存去
  // 哪個錨點。建立／編輯由 modal 依自己的 `policy` prop 判定並回傳 policyId。
  savePolicy = (vals, policyId) => {
    if (policyId) {
      return cloudServiceAccessService.updatePolicy(
        this.props.workspaceId, this.serviceId, policyId, vals);
    }
    return cloudServiceAccessService.createPolicy(
      this.props.workspaceId, this.serviceId, vals);
  };

  onPolicySaved = async () => {
    this.state.showPolicyModal = false;
    await this.reloadPolicies();
  };

  async deletePolicy(policy) {
    // 確認視窗在 AccessAppCard 裡（三個 surface 共用），走到這裡＝使用者已經
    // 確認過了。
    this.state.error = null;
    const res = await cloudServiceAccessService.deletePolicy(
      this.props.workspaceId, this.serviceId, policy.id);
    if (res.success) {
      await this.reloadPolicies();
    } else {
      this.state.error = res.error;
    }
  }

  async commitPolicyOrder(ids) {
    this.state.error = null;
    // reorder 端點會重推一次 Cloudflare Access，不是即時的——清單保持可見但標成
    // 忙碌中（卡片自己已經排好了，這裡只等寫入）。
    this.state.reordering = true;
    const res = await cloudServiceAccessService.reorderPolicies(
      this.props.workspaceId, this.serviceId, ids);
    this.state.reordering = false;
    if (!res.success) {
      this.state.error = res.error;
    }
    // 成功：確認伺服器順序；失敗：把樂觀順序還原。兩條都重讀。
    await this.reloadPolicies();
  }
}
