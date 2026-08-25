/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowCard } from "../../../components/card/WoowCard";
import { WoowIcon } from "../../../components/icon/WoowIcon";
import { AccessAppCard } from "../../../components/access/AccessAppCard";
import { AddPolicyModal } from "../../../components/modal/AddPolicyModal";
import { smartHomeAccessService } from "../../../services/smart_home_access";

// 開啟保護前的「明示後果」。Smart Home 的對外網址就是租戶（與家人）平常用來開
// Home Assistant 的那一個——開下去之後**連他們自己**都要先過 Cloudflare Access
// 才進得來，而且手機 App／HomeKit 之類不會走瀏覽器登入流程的用戶端會直接被擋在
// 外面。這句必須在按下去之前出現，不是事後才發現。
//
// 文案形狀沿用 CS 的 PROTECT_CONFIRM（`pages/service/tabs/AccessTab.js`），語境
// 改寫成 Smart Home：那邊講的是「這個服務的網址」，這裡要講清楚被擋的是誰。
const PROTECT_CONFIRM = {
  title: _t("Turn on Access protection?"),
  message: _t(
    "Once protection is on, every visitor has to pass Access verification before this address opens, including you and your own family. Only identities matching an Access policy below get in; clients that never show a browser login page (a phone app connecting directly, for example) are shut out."
  ),
  confirmLabel: _t("Turn protection on"),
};

/**
 * SmartHomeAccessTab Component（Smart Home detail 的第二顆頁籤，#768）
 *
 * 錨點是 **route**（裁決 2：SH 走 per-route 多錨點），所以這裡是
 * `t-foreach` N 張 AccessAppCard——形狀抄 SA 的 Access 分頁
 * （`pages/security-access/SecurityAccessDetailPage.xml`，per-route N 張卡的既有
 * 先例），不是 CS 的單張。今天 `_ensure_primary_route()` 只建一條 primary，所以
 * 實際只會畫出一張；**這不是「就是一張」的簡化**——第二條 route 一落地，這個頁
 * 面不必再改一次。
 *
 * 狀態刻意留在本元件而不是回頭叫父層 `loadSmartHome()`：那支會把
 * `state.loading` 設回 true，整頁閃成 "Loading smart home..." 並把這個頁籤連同
 * 展開狀態一起卸載——每按一次開關閃一次。理由與 CS 的 AccessTab 逐字相同。
 *
 * 種子只在 setup 取一次（不掛 onWillUpdateProps）：這顆頁籤是 `t-if` 掛載的，
 * 切走再切回來自然會重新 seed；而父層在本元件寫入時**不會**重載（寫入回應本身
 * 就夠用），所以「props 比本地新」的窗口不存在。CS 的 AccessTab 同一個作法。
 *
 * Props:
 *   - home (Object)：SH detail payload（`to_dict(include_details=True)`，帶 routes）
 *   - workspaceId (number)
 */
export class SmartHomeAccessTab extends Component {
  static template = "woow_paas_platform.SmartHomeAccessTab";
  static components = { WoowCard, WoowIcon, AccessAppCard, AddPolicyModal };
  static props = {
    home: { type: Object },
    workspaceId: { type: Number },
  };

  setup() {
    this.protectConfirm = PROTECT_CONFIRM;
    this.state = useState({
      // 只有 detail 端點帶 `routes`（`to_dict(include_details=True)`）；清單與
      // refresh 端點刻意不帶（見 smart_home.to_dict 的註解）。缺席一律當空陣列
      // ——畫面會落到「還沒有對外網址」那格，不會長出一張沒有錨點的卡。
      // 上游的 SmartHomeDetailPage.refreshStatus 已經負責不讓 refresh 的回應把
      // 這個 key 洗掉；這裡的 `|| []` 是最後一道，不是唯一一道。
      routes: [...(this.props.home.routes || [])],
      // 展開狀態 per route（undefined/true = 展開，false = 使用者收起來了）
      expandedRoutes: {},
      // 排序寫入中的那條 route（reorder 會重推 Cloudflare，不是即時的）
      reorderingRouteId: null,
      // 操作失敗（我剛剛按的那一下沒成功）
      error: null,
      // 資料完整性警告（操作成功了，但伺服器回來的東西對不上）。刻意**不共用**
      // `error`：兩者的生命週期不同——error 由每次操作開頭清掉，這一格要撐到下
      // 一次重讀才重新判定。共用一格的話，後面一次成功的操作會把它擦掉，而它描
      // 述的問題還在。
      orphanWarning: null,
      showPolicyModal: false,
      editingPolicy: null,
      policyModalRoute: null,
    });
  }

  get smartHomeId() {
    return this.props.home.id;
  }

  get routes() {
    return this.state.routes;
  }

  // 卡頭副標 "N of M routes protected" 的 N（同 SA）。
  get protectedRouteCount() {
    return this.state.routes.filter((r) => r.is_protected).length;
  }

  // ---- 本地 route 狀態維護 ----

  /**
   * 用伺服器回來的那一份取代本地同 id 的 route。
   *
   * 找不到 id 是**不該出現**的一格：config 端點的回應是我們剛剛指名要寫的那條
   * route。靜默 return 的話畫面會停在舊值（開關扳回去、session 沒變），與「後端
   * 拒絕了」長得一模一樣；所以這裡要說出來。
   */
  _replaceRoute(routeData) {
    const idx = this.state.routes.findIndex((r) => r.id === routeData.id);
    if (idx < 0) {
      this.state.error = _t(
        "Route #%s came back from the server but is not on the current screen — what you are looking at may be out of date. Please refresh and try again.",
        routeData.id
      );
      return;
    }
    this.state.routes.splice(idx, 1, routeData);
  }

  // ---- config 寫入（route-pinned） ----

  async saveRouteAccess(route, vals) {
    this.state.error = null;
    const res = await smartHomeAccessService.updateRouteAccess(
      this.props.workspaceId, this.smartHomeId, route.id, vals);
    if (res.success) {
      this._replaceRoute(res.data);
    } else {
      this.state.error = res.error;
    }
  }

  // 保護中的 route，設定面板預設展開；使用者可以自己收起來。
  isRouteExpanded(route) {
    return this.state.expandedRoutes[route.id] !== false;
  }

  toggleRouteExpand(route) {
    this.state.expandedRoutes[route.id] = !this.isRouteExpanded(route);
  }

  onRouteProtectedToggle(route, checked) {
    // 剛開啟保護時把卡片展開，設定就在眼前。
    if (checked) {
      this.state.expandedRoutes[route.id] = true;
    }
    this.saveRouteAccess(route, { is_protected: checked });
  }

  // Login methods 以 toggle pill 呈現；pill 點擊讀目前 route[method] 取反。
  toggleRouteLogin(route, method) {
    this.saveRouteAccess(route, { [method]: !route[method] });
  }

  onRouteSessionChange(route, value) {
    this.saveRouteAccess(route, { session_duration: value });
  }

  // ---- policy ----

  /**
   * 重讀 policy 並依錨點分桶回各條 route。
   *
   * list 端點回的是這台 SH 底下**跨 route 的平面清單**（父層 pinned，見
   * services/smart_home_access.js 檔頭），分桶是呼叫端的事。
   *
   * 分不進任何一條 route 的那些要**明講**，不能 `continue` 吃掉：SQL CHECK 保證
   * 每筆 policy 恰有一個錨點、`smart_home_id` 又是從 `smart_home_route_id` 導出
   * 的 stored related，所以「掛在這台 SH、卻對不到任何一條 route」在資料層是不
   * 可能的。真的出現＝資料或分桶邏輯有一邊壞了，而它的症狀（policy 從畫面上消
   * 失）與「這條 route 本來就沒有 policy」完全不可區分——那正是會被當成正常而
   * 放過去的一格。
   */
  async reloadPolicies() {
    const res = await smartHomeAccessService.fetchPolicies(
      this.props.workspaceId, this.smartHomeId);
    if (!res.success) {
      this.state.error = res.error;
      return;
    }
    const known = new Set(this.state.routes.map((r) => r.id));
    const byRoute = {};
    const orphans = [];
    for (const pol of res.data || []) {
      const routeId = pol.smart_home_route_id;
      if (!routeId || !known.has(routeId)) {
        orphans.push(pol.id);
        continue;
      }
      (byRoute[routeId] = byRoute[routeId] || []).push(pol);
    }
    this.state.routes = this.state.routes.map((r) => ({
      ...r,
      access_policies: byRoute[r.id] || [],
    }));
    this.state.orphanWarning = orphans.length
      ? _t(
          "%(count)s Access policies (#%(ids)s) match none of this Smart Home's addresses, so they are not listed below — they are still in effect on Cloudflare. Please report this.",
          { count: orphans.length, ids: orphans.join(", #") }
        )
      : null;
  }

  openAddPolicy(route) {
    this.state.policyModalRoute = route;
    this.state.editingPolicy = null;
    this.state.showPolicyModal = true;
  }

  openEditPolicy(route, policy) {
    this.state.policyModalRoute = route;
    this.state.editingPolicy = policy;
    this.state.showPolicyModal = true;
  }

  closePolicyModal() {
    this.state.showPolicyModal = false;
    this.state.policyModalRoute = null;
  }

  // AddPolicyModal 的注入點：modal 只知道「存一筆 policy」這件事，不知道它存去
  // 哪個錨點。建立／編輯由 modal 依自己的 `policy` prop 判定並回傳 policyId
  // ——這裡不從 state.editingPolicy 再推一次（同一件事判兩次，遲早漂移）。
  savePolicy = (vals, policyId) => {
    if (policyId) {
      return smartHomeAccessService.updatePolicy(
        this.props.workspaceId, this.smartHomeId, policyId, vals);
    }
    return smartHomeAccessService.createPolicy(
      this.props.workspaceId, this.smartHomeId,
      this.state.policyModalRoute.id, vals);
  };

  onPolicySaved = async () => {
    this.state.showPolicyModal = false;
    this.state.policyModalRoute = null;
    await this.reloadPolicies();
  };

  async deletePolicy(route, policy) {
    // 刪除只認 policy id（route 只是為了呼叫端對稱／日後 per-route 消歧義；
    // 後端會在已 gate 過的 SH 底下自己解出這筆 policy）。
    this.state.error = null;
    const res = await smartHomeAccessService.deletePolicy(
      this.props.workspaceId, this.smartHomeId, policy.id);
    if (res.success) {
      await this.reloadPolicies();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- precedence 寫入 ----
  // 拖曳的互動（即時預覽、原地放下不寫、取消還原）在 AccessAppCard 裡；這裡只
  // 負責把卡片算好的最終順序寫回端點，並持有 saving 狀態。
  async commitPolicyOrder(route, ids) {
    this.state.error = null;
    // reorder 端點會對每一條被動到的 route 重推一次 Cloudflare Access，不是即時
    // 的——清單保持可見但標成忙碌中（卡片自己已經排好了，這裡只等寫入）。
    this.state.reorderingRouteId = route.id;
    const res = await smartHomeAccessService.reorderPolicies(
      this.props.workspaceId, this.smartHomeId, ids);
    this.state.reorderingRouteId = null;
    if (!res.success) {
      this.state.error = res.error;
    }
    // 成功：確認伺服器順序；失敗：把樂觀順序還原。兩條都重讀。
    // （reloadPolicies 只寫 orphanWarning，不會蓋掉上面剛設的 error。）
    await this.reloadPolicies();
    // 後端對認不出來的 policy_id 是**靜默略過**（只回它真的寫進去的那些）。
    // 少寫了幾筆而不吭聲的話，畫面重讀完看起來只是「順序沒完全照做」，與
    // 「拖曳沒抓準」不可區分。只在整趟成功時才判，免得蓋掉真正的錯誤。
    const written = (res.success && res.data && res.data.reordered) || [];
    if (res.success && !this.state.error && written.length !== ids.length) {
      this.state.error = _t(
        "Sent %(sent)s policies to reorder, the server only wrote %(written)s — the order shown below is the real order on the server. Please refresh and order them again.",
        { sent: ids.length, written: written.length }
      );
    }
  }
}
