/** @odoo-module **/

import { Component, useState, useRef, onWillUpdateProps } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";

// Cloudflare-internal IdP type strings (as persisted in
// access_unresolved_idp_types by the operator / _build_access_payload) →
// the SAME user-facing labels the login-method toggles use (XML li_google /
// li_otp). The fail-open widening warning must name the methods the user
// actually selected by those labels; surfacing the raw 'onetimepin' (a string
// the user never picked by that name) weakens the warning's actionability.
const IDP_TYPE_LABELS = {
  onetimepin: "Email OTP",
  google: "Google",
};

/**
 * AccessAppCard Component
 *
 * 一個 **Cloudflare Access 應用程式錨點**的設定卡：保護開關、登入方式、policy
 * 清單（含拖曳排序）與 session 長度。#746 從 SecurityAccessDetailPage.xml 的
 * per-route 區塊原地抽出，兩個 surface 共用：
 *
 *   - Security Access：錨點＝一條 Application Route（一個 SA 底下 N 張卡）
 *   - Cloud Service：錨點＝服務自己的 subdomain（一台服務一張卡，拍板 5 的
 *     整站粒度）
 *
 * 卡片**不認識任何 API**——所有寫入都往上拋 callback，由呼叫端接自己的 service
 * wrapper。這是抽取的重點：SA 與 CS 的 endpoint 形狀不同（route-pinned vs
 * service-pinned），共用的是互動與版面，不是資料通道。
 *
 * Props:
 *   - hostname (string)：這張卡的錨點主機名（顯示用）
 *   - config (Object)：access 設定現值——is_protected / login_google /
 *       login_email_otp / session_duration / access_unresolved_idp_types /
 *       access_policies（形狀＝後端 route.to_dict() 與 CS `_access_to_dict()`
 *       的交集，兩邊逐字相同）
 *   - expanded (boolean)：展開狀態由呼叫端持有（SA 要記住每條 route 各自的）
 *   - reordering (boolean, optional)：排序寫入中（顯示 saving 遮罩）
 *   - protectDisabled (boolean, optional)：沒有可掛 Access app 的錨點時鎖開關
 *   - protectConfirm (Object|null, optional)：開啟保護前的確認視窗文案
 *       （{ title, message, confirmLabel }）。**不傳＝不確認、直接開**——SA 端
 *       維持抽取前的即時行為；CS 端傳入以滿足拍板 5 的「明示後果」。
 *   - onToggleExpand / onProtectedChange / onLoginToggle / onSessionChange /
 *     onAddPolicy / onEditPolicy / onDeletePolicy / onReorderPolicies (Function)
 */
export class AccessAppCard extends Component {
  static template = "woow_paas_platform.AccessAppCard";
  static components = { WoowButton, WoowIcon };
  static props = {
    hostname: { type: String },
    config: { type: Object },
    expanded: { type: Boolean },
    reordering: { type: Boolean, optional: true },
    protectDisabled: { type: Boolean, optional: true },
    protectConfirm: { type: [Object, { value: null }], optional: true },
    onToggleExpand: { type: Function },
    onProtectedChange: { type: Function },
    onLoginToggle: { type: Function },
    onSessionChange: { type: Function },
    onAddPolicy: { type: Function },
    onEditPolicy: { type: Function },
    onDeletePolicy: { type: Function },
    onReorderPolicies: { type: Function },
  };

  setup() {
    // policy 清單保留一份**卡片自己的**副本。拖曳的即時預覽（dragover 當下就
    // 重排）必須是這個元件訂閱得到的狀態：OWL 的響應式是逐 key 追蹤的，父層只
    // 把 config 當 prop 傳下來、沒有讀到 `access_policies` 這個 key，直接改
    // props 上的陣列不會讓任何人重繪。
    this.state = useState({
      policies: this._seedPolicies(this.props.config),
      draggingPolId: null,
      // 開啟保護的確認視窗（protectConfirm 有傳才會用到）
      confirmingProtect: false,
    });
    this.protectInputRef = useRef("protectInput");
    onWillUpdateProps((nextProps) => {
      // 兩段都不能被外面送來的順序蓋掉，判準是**伺服器有沒有確認過**，不是
      // 「手指有沒有放開」：
      //
      //   1. 拖曳進行中（_dragPolId）——使用者眼前正在排的那一版才是待會要送
      //      出去的那一版。
      //   2. 放開之後、寫入回來之前（_commitPending）——父層一收到 reorder 就
      //      把 saving 狀態打開，那次 state 變動會讓它**立刻**重繪一次，而它手
      //      上的 config 還是舊順序。少了這一段，剛拖好的順序會在整個 saving
      //      視窗被打回舊值，等 API＋reload 回來才跳回去（實測：預覽正確 →
      //      52ms 後打回舊值 → 3.2s 後才跳回，與「放開當下即時」完全相反）。
      //
      // 解除時機刻意是「父層那條 promise（含它自己的 reload）resolve 之後」，
      // 不是「drop 當下」：OWL 的重繪走 animation frame，排在 promise 之後，所
      // 以旗標放行時最新那批 props 還沒繪出來，會由這個 hook 收尾——成功時是伺
      // 服器順序（與樂觀順序相同），失敗時是舊順序（樂觀順序自動還原）。
      if (this._dragPolId == null && !this._commitPending) {
        this.state.policies = this._seedPolicies(nextProps.config);
      }
    });
  }

  _seedPolicies(config) {
    return [...((config && config.access_policies) || [])];
  }

  // ---- protect 開關 ----

  onProtectedToggle(ev) {
    const checked = ev.target.checked;
    if (checked && this.props.protectConfirm) {
      // 先攔下來問；使用者按取消時要把 checkbox 扳回去（見 cancelProtect）。
      this.state.confirmingProtect = true;
      return;
    }
    this.props.onProtectedChange(checked);
  }

  confirmProtect() {
    this.state.confirmingProtect = false;
    this.props.onProtectedChange(true);
  }

  cancelProtect() {
    this.state.confirmingProtect = false;
    // checkbox 的 checked 是使用者**直接改動 DOM** 的結果，不是 render 出來的
    // ——config.is_protected 一格都沒變，OWL 沒有任何理由重繪它。不手動扳回去
    // 的話畫面會停在「已開啟」，而伺服器上是關的：一個看不出來的謊。
    if (this.protectInputRef.el) {
      this.protectInputRef.el.checked = !!this.props.config.is_protected;
    }
  }

  // ---- 登入方式 ----

  unresolvedIdpLabels() {
    const raw = this.props.config.access_unresolved_idp_types;
    if (!raw) {
      return "";
    }
    return raw
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t)
      .map((t) => IDP_TYPE_LABELS[t] || t)
      .join(", ");
  }

  // ---- P-rule：decision badge/mark、rule 摘要、拖曳排序 ---- //
  // Cloudflare 對 Bypass/Service-Auth 的評估順序先於 Allow/Block，與清單位置
  // 無關；這裡的平面可拖曳清單鏡射 CF 自己的 policy UI（分層是 CF 的，不在這
  // 裡重做一次）。

  policyBadgeClass(pol) {
    if (pol.decision === "deny") return "o_woow_badge o_woow_badge_gray";
    if (pol.decision === "bypass") return "o_woow_badge o_woow_badge_warning";
    return "o_woow_badge o_woow_badge_green";
  }

  policyBadgeLabel(pol) {
    if (pol.decision === "deny") return "Block";
    if (pol.decision === "bypass") return "Bypass";
    return "Allow";
  }

  policyMarkClass(pol) {
    const base = "o_woow_sa_policy__mark ";
    if (pol.decision === "deny") return base + "o_woow_sa_policy__mark--deny";
    if (pol.decision === "bypass") return base + "o_woow_sa_policy__mark--bypass";
    return base + "o_woow_sa_policy__mark--allow";
  }

  policyMarkIcon(pol) {
    if (pol.decision === "deny") return "block";
    if (pol.decision === "bypass") return "bolt";
    return "check";
  }

  policySummary(pol) {
    const fmt = (r) => (r.rule_type === "everyone" ? "everyone" : r.rule_value);
    const inc = (pol.include || []).map(fmt).join(", ");
    const extra = [];
    if ((pol.require || []).length) extra.push(`+${pol.require.length} require`);
    if ((pol.exclude || []).length) extra.push(`-${pol.exclude.length} exclude`);
    return extra.length ? `${inc} · ${extra.join(" · ")}` : inc;
  }

  onPolicyDragStart(pol, ev) {
    this._dragPolId = pol.id;
    this._dropHandled = false;
    // 記下伺服器順序，讓「原地放下」可以什麼都不做。
    this._dragOrigOrder = this.state.policies.map((p) => p.id);
    this.state.draggingPolId = pol.id;
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = "move";
      // Firefox 只有在設過 data 之後才會真的開始拖曳。
      ev.dataTransfer.setData("text/plain", String(pol.id));
    }
  }

  onPolicyDragOver(targetPol, ev) {
    ev.preventDefault(); // 不 preventDefault 就不允許 drop
    if (ev.dataTransfer) {
      ev.dataTransfer.dropEffect = "move";
    }
    // 即時預覽：把被拖的那列排進游標所在的位置。使用者看到什麼就會存什麼，
    // 所以放開的當下感覺是即時的——（慢的）API 呼叫只是事後把它寫下去。
    const dragId = this._dragPolId;
    if (!dragId || dragId === targetPol.id) {
      return;
    }
    const pols = this.state.policies;
    const from = pols.findIndex((p) => p.id === dragId);
    const to = pols.findIndex((p) => p.id === targetPol.id);
    if (from < 0 || to < 0 || from === to) {
      return;
    }
    pols.splice(to, 0, pols.splice(from, 1)[0]); // 響應式 → 清單重排
  }

  onPolicyDrop(ev) {
    ev.preventDefault();
    // dragover 已經把清單排成想要的樣子；把使用者眼前這一版存下去。
    this._commitPolicyOrder();
  }

  onPolicyDragEnd() {
    this.state.draggingPolId = null;
    // 在任何一列之外放開（取消）——丟掉即時預覽，回到外面傳進來的順序。真正的
    // drop 會先把 _dropHandled 設起來。
    if (!this._dropHandled) {
      this._dragPolId = null;
      this.state.policies = this._seedPolicies(this.props.config);
    }
  }

  async _commitPolicyOrder() {
    this._dropHandled = true;
    this._dragPolId = null;
    this.state.draggingPolId = null;
    const ids = this.state.policies.map((p) => p.id);
    // 放回原位 ⇒ 沒有東西需要寫（省掉一次 CF re-sync）。
    if (this._dragOrigOrder && ids.join() === this._dragOrigOrder.join()) {
      return;
    }
    // 寫入期間擋掉 props 回灌（見 onWillUpdateProps 的第 2 段）。呼叫端的
    // callback 是 async 且**自己會 reload**，所以 await 它就等於「等伺服器確認
    // 完、而且新資料已經寫進父層 state」——旗標在那之後才放行。
    this._commitPending = true;
    try {
      await this.props.onReorderPolicies(ids);
    } finally {
      this._commitPending = false;
    }
  }
}
