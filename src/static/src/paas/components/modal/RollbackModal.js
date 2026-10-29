/** @odoo-module **/
import {
    Component,
    useState,
    useRef,
    useEffect,
    onMounted,
    onWillDestroy,
} from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { cloudService, ROLLBACK_REQUEST_FAILED } from "../../services/cloud_service";
import { formatUserDateTime } from "../../utils/datetime_helpers";

// #2069：回復 API 在動作當下重算預檢、拒絕時帶回的 data.code（與後端 paas.py 同名）。
export const ROLLBACK_REMOVES_INSTALLED_ADDONS = "rollback_removes_installed_addons";
export const ROLLBACK_ADDON_CHECK_FAILED = "rollback_addon_check_failed";
export const ROLLBACK_ADDON_UNVERIFIED = "rollback_addon_unverified";
// 這三種拒絕都代表「畫面上的預檢結果已經過期」：要回選版本步驟並重新預檢。
// 另有 ROLLBACK_REQUEST_FAILED（cloud_service 在 RPC 丟例外時回的，結果未知）走同一條路。
export const ROLLBACK_REFUSAL_CODES = [
    ROLLBACK_REMOVES_INSTALLED_ADDONS,
    ROLLBACK_ADDON_CHECK_FAILED,
    ROLLBACK_ADDON_UNVERIFIED,
];

// 每個對話框實例一個序號：aria-describedby 要指向「這個實例」面板的標題與說明。
let rollbackModalUid = 0;

/**
 * #2069：把 check-rollback-addons 的回應分類成閘門種類。
 * 任何看不懂的回應一律 fail-closed（failed），不會放行。
 *
 * kind: none（沒有模組被拿掉／非 woow-odoo 租戶）| safe | blocked | unverified | failed
 */
export function classifyRollbackPrecheck(result) {
    if (!result || typeof result !== "object") {
        return { kind: "failed", result: {} };
    }
    const modules = result.modules && typeof result.modules === "object" ? result.modules : {};
    const blocking = result.blocking && typeof result.blocking === "object" ? result.blocking : {};
    const removed = Array.isArray(result.removed) ? result.removed : [];
    const blockingNames = Object.keys(blocking);
    const listed = removed.length ? removed : Object.keys(modules);

    if (result.success === true) {
        // 契約之外的矛盾（成功卻帶 blocking、或 code 是 blocked）一律當 blocked。
        if (result.code === "blocked" || blockingNames.length) {
            return {
                kind: "blocked",
                result,
                modules,
                blocking,
                names: blockingNames.length ? blockingNames : listed,
            };
        }
        // 只有 code === "no_removal" 才是 none；applicable:false 只能搭配 no_removal 且沒有
        // removed（非 woow-odoo 租戶＝不適用）。其餘自相矛盾的組合一律 failed（通用文字）。
        const removedMalformed =
            result.removed !== undefined && result.removed !== null && !Array.isArray(result.removed);
        if (removedMalformed) {
            return { kind: "failed", result: { ...result, error: "" } };
        }
        if (result.code === "no_removal") {
            if (removed.length) {
                return { kind: "failed", result: { ...result, error: "" } };
            }
            return { kind: "none", result };
        }
        if (result.code === "safe" && result.applicable !== false) {
            return { kind: "safe", result, names: listed };
        }
        return { kind: "failed", result: { ...result, error: "" } };
    }
    if (result.success === false && result.code === "unverified") {
        return { kind: "unverified", result, names: removed };
    }
    return { kind: "failed", result };
}

/**
 * RollbackModal Component
 *
 * Displays revision history and allows rollback to a previous version.
 *
 * Props:
 *   - service (Object): Service data { id, workspace_id, helm_revision, name }
 *   - onClose (Function): Called when modal is closed/cancelled
 *   - onRollback (Function): Called after successful rollback
 */
export class RollbackModal extends Component {
    static template = "woow_paas_platform.RollbackModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        service: { type: Object },
        onClose: { type: Function },
        onRollback: { type: Function },
    };

    setup() {
        this.state = useState({
            revisions: [],
            selectedRevision: null,
            loading: true,
            rolling: false,
            error: null,
            // #1433：選好版本後先進確認步驟（說明會回復什麼、不會回復什麼），
            // 再按一次才真的送出。原本單擊即執行、對話框也沒提帳密。
            confirming: false,
            // INFRA-FIX-018-1／AI-HER-003-1：operator 每個 release 只保留最近 N 筆 revision
            // （helm --history-max），更舊的已被清掉、無法回復；對話框要講明。
            historyMax: null,
            // #2069：選好目標版本就預檢「這次回復會不會把仍安裝在租戶 DB 的模組程式碼拿掉」。
            // 結果按 revision 記錄；revision 對不上目前選的版本＝還沒有可信的結果（fail-closed）。
            // seq：每次換掉顯示中的預檢（含 loading）就加一，勾選框用它確認「勾的是這一份結果」。
            precheck: { revision: null, status: "idle", result: null, seq: 0 },
            // unverified（服務沒在跑、讀不到模組狀態）時使用者勾選「我了解」才放行。
            // 只由 onAcknowledge 與 _showPrecheck 寫入：顯示中的預檢一換就歸零。
            acknowledged: false,
        });
        // aria-describedby 用：面板標題與說明的 id（每個實例唯一）。
        this.uid = ++rollbackModalUid;
        this.precheckTitleId = `o_woow_rollback_precheck_title_${this.uid}`;
        this.precheckHintId = `o_woow_rollback_precheck_hint_${this.uid}`;
        this.precheckReasonId = `o_woow_rollback_reason_${this.uid}`;
        // 預檢面板的種類變了就盡力捲到看得到（面板在可捲動的 body 底部、清單可能很高）。
        this.panelRef = useRef("precheckPanel");
        useEffect(
            () => this._revealPanel(),
            () => [this.precheckPanel ? this.precheckPanel.kind : null]
        );

        onMounted(() => {
            this.loadRevisions();
        });
        onWillDestroy(() => {
            this._pre.destroyed = true;
        });
    }

    /**
     * 預檢排程狀態。惰性建立（測試有不跑 setup 就直接用 prototype 的）。
     *   inflight：目前在飛的請求（同一時間最多一個）
     *   wanted：飛行中又選了別的版本時，只記得「最新想要」的那一個
     *   cache：本對話框存活期間每個 revision 已定案的結果
     *   gens：每個 revision 的世代；重試或被拒絕時加一，世代過期的回應不得進 cache、也不得顯示
     *   seq：顯示中預檢的序號（見 _showPrecheck）
     */
    get _pre() {
        if (!this.__pre) {
            this.__pre = {
                inflight: null,
                wanted: null,
                cache: new Map(),
                gens: new Map(),
                seq: 0,
                destroyed: false,
            };
        }
        return this.__pre;
    }

    _gen(revision) {
        return this._pre.gens.get(revision) || 0;
    }

    /**
     * 換掉顯示中的預檢：一律給新序號並把「我了解」歸零。所有進入 loading／ready 的路徑
     * （新請求、快取命中、請求落定）都經過這裡，所以新結果不可能一出現就已勾選。
     */
    _showPrecheck(revision, status, result) {
        this.state.precheck = { revision, status, result, seq: ++this._pre.seq };
        this.state.acknowledged = false;
    }

    /**
     * 勾選框的 change 事件。勾選框帶著渲染當下的預檢序號（data-token）：同一個畫格內
     * 還沒重繪、使用者勾到「舊面板」的框時，序號對不上目前顯示的預檢，就不算數。
     */
    onAcknowledge(ev) {
        const input = ev.target;
        const token = Number(input.dataset.token);
        const current = this.state.precheck;
        const valid = current.status === "ready" && token === current.seq;
        this.state.acknowledged = valid && input.checked === true;
    }

    /** 面板種類改變後把它捲進可視範圍；沒有 scrollIntoView 的環境（jsdom）就略過。 */
    _revealPanel() {
        const el = this.panelRef && this.panelRef.el;
        if (!el || typeof el.scrollIntoView !== "function") {
            return;
        }
        try {
            el.scrollIntoView({ block: "nearest" });
        } catch {
            // 盡力而為：捲動失敗不影響閘門
        }
    }

    /**
     * #1414／#1456：「目前」＝helm history 裡部署中的那一版（後端 `annotate_rollback_floor`
     * 標的 `is_current`）。平台帳本 `helm_revision` 在更新／回復後會落後，拿它標「目前」
     * 會把舊版標成目前、真正部署中的那一版反而能被選來回復。舊後端沒有這個欄位、或
     * history 裡沒有部署中的版本時，才退回帳本值。
     */
    get currentRevision() {
        const deployed = this.state.revisions.find((r) => r.is_current === true);
        if (deployed) {
            return deployed.revision;
        }
        return this.props.service.helm_revision || 1;
    }

    get canRollback() {
        return (
            this.state.selectedRevision !== null &&
            this.state.selectedRevision !== this.currentRevision &&
            !this.state.rolling &&
            this.precheckAllows
        );
    }

    /**
     * #2069：預檢閘門。只有「沒有模組被拿掉」「被拿掉的都是未安裝」放行；
     * unverified 要勾「我了解」；預檢中、blocked、check_failed、RPC 失敗一律不放行。
     * 預檢結果的 revision 與目前選的不同（含尚未開始）＝當成預檢中，不放行。
     */
    get precheckGate() {
        const selected = this.state.selectedRevision;
        const precheck = this.state.precheck;
        if (selected === null || selected === undefined) {
            return { kind: "none", result: null };
        }
        if (!precheck || precheck.revision !== selected || precheck.status !== "ready") {
            return { kind: "loading", result: null };
        }
        return classifyRollbackPrecheck(precheck.result);
    }

    /** 選取中的版本還在預檢（含排隊）：重試與確認都不該動。 */
    get precheckLoading() {
        return this.precheckGate.kind === "loading" && this.state.selectedRevision !== null;
    }

    get precheckAllows() {
        const kind = this.precheckGate.kind;
        if (kind === "none" || kind === "safe") {
            return true;
        }
        if (kind === "unverified") {
            return this.state.acknowledged === true;
        }
        return false;
    }

    /** 回復請求是否要帶 acknowledge_unverified_addons：只有 unverified 且已勾選才帶。 */
    get sendsAcknowledgement() {
        return this.precheckGate.kind === "unverified" && this.state.acknowledged === true;
    }

    /**
     * 確認鈕旁常駐的一行原因（在 footer、不隨 body 捲動）：預檢區塊在可捲動的 body 底部，
     * 清單高時會被切到看不見，按鈕卻已經是 disabled。與面板同一份模型：
     * 預檢中／blocked／failed 沿用面板標題，unverified 尚未勾選給一句「勾選後才能繼續」；
     * 按鈕放行（或沒有面板）時回空字串。
     */
    get precheckReason() {
        const panel = this.precheckPanel;
        if (!panel || this.precheckAllows) {
            return "";
        }
        if (panel.kind === "unverified") {
            return _t('Tick "I understand" to continue.');
        }
        return panel.title;
    }

    /** 原因行顯示時，確認鈕用 aria-describedby 指向它；沒有原因行就不帶屬性。 */
    get confirmDescribedBy() {
        return this.precheckReason ? this.precheckReasonId : undefined;
    }

    /**
     * 預檢區塊的顯示模型（兩個步驟共用同一塊）；沒有要顯示的東西時回 null。
     * 文字全在這裡用 _t 產生，模板只負責排版。
     */
    get precheckPanel() {
        if (this.state.loading || !this.state.revisions.length) {
            return null;
        }
        const gate = this.precheckGate;
        const result = gate.result || {};
        switch (gate.kind) {
            case "loading":
                if (this.state.selectedRevision === null) {
                    return null;
                }
                return {
                    kind: "loading",
                    role: "status",
                    icon: "sync",
                    iconClass: "o_woow_spin",
                    title: _t("Checking modules..."),
                    items: [],
                };
            case "safe":
                return {
                    kind: "safe",
                    role: "status",
                    icon: "info",
                    title: _t("These modules will no longer be offered after this rollback:"),
                    items: gate.names.map((name) => ({ name })),
                    hint: _t("They are not installed in Odoo, so your data is not affected."),
                };
            case "blocked":
                return {
                    kind: "blocked",
                    role: "alert",
                    icon: "block",
                    title: _t("Rollback blocked: modules are still installed"),
                    intro: _t("This rollback would remove modules that are still installed in Odoo:"),
                    items: gate.names.map((name) => {
                        const dependents = gate.modules[name]?.dependents;
                        return {
                            name,
                            state: gate.blocking[name] || gate.modules[name]?.state || "",
                            detail:
                                Array.isArray(dependents) && dependents.length
                                    ? _t("(required by: %s)", dependents.join(", "))
                                    : "",
                        };
                    }),
                    hint: _t(
                        "Uninstall them in Odoo first (Apps → Uninstall), or choose a revision that includes them."
                    ),
                };
            case "unverified":
                return {
                    kind: "unverified",
                    role: "alert",
                    icon: "warning",
                    title: _t("Installed modules cannot be checked"),
                    intro: _t(
                        "The service is not running, so the platform cannot check whether the modules removed by this rollback are installed."
                    ),
                    items: gate.names.map((name) => ({ name })),
                    hint: _t(
                        "If they are installed, Odoo will stop loading them after the rollback, or the service may fail to start. Rolling back to a revision that includes them recovers the service; no data is deleted."
                    ),
                    ackLabel: _t("I understand"),
                };
            case "failed":
                return {
                    kind: "failed",
                    role: "alert",
                    icon: "error",
                    title: _t("Could not check the modules"),
                    intro: result.error || _t("The module check failed. Please try again."),
                    items: [],
                    retry: true,
                };
            default:
                return null;
        }
    }

    async loadRevisions() {
        this.state.loading = true;
        this.state.error = null;

        const result = await cloudService.fetchRevisions(
            this.props.service.workspace_id,
            this.props.service.id
        );

        if (result.success) {
            this.state.revisions = result.data || [];
            this.state.historyMax = result.historyMax ?? null;
        } else {
            this.state.error = result.error || _t("Failed to load revision history");
        }

        this.state.loading = false;
    }

    selectRevision(revision) {
        if (
            revision.revision !== this.currentRevision &&
            !this.isBlocked(revision) &&
            !this.state.rolling
        ) {
            const changed = this.state.selectedRevision !== revision.revision;
            this.state.selectedRevision = revision.revision;
            if (changed) {
                // 上一個版本被拒絕或失敗的橫幅不屬於新選的版本。
                this.state.error = null;
                this.startPrecheck(revision.revision);
            }
        }
    }

    /**
     * #2069：預檢「回復到 revision 會拿掉哪些模組、其中有沒有仍安裝的」。
     * 路由只是諮詢性質，`_rollback_service` 在動作當下重算同一個判斷、才是權威。
     * force（重試、被後端拒絕後）：丟掉這個版本的 cache、世代加一，讓所有還在飛或排隊中的
     * 舊回應都不能再進 cache 或顯示。元件卸載後什麼都不送、不寫。
     */
    startPrecheck(revision, { force = false } = {}) {
        const pre = this._pre;
        if (pre.destroyed) {
            return Promise.resolve();
        }
        if (force) {
            pre.cache.delete(revision);
            pre.gens.set(revision, this._gen(revision) + 1);
        }
        if (pre.cache.has(revision)) {
            // 這個版本本對話框已預檢過：直接用，不再打 kubectl exec 讀整個 Odoo registry。
            this._release(pre.wanted);
            pre.wanted = null;
            this._showPrecheck(revision, "ready", pre.cache.get(revision));
            return Promise.resolve();
        }
        this._showPrecheck(revision, "loading", null);
        return new Promise((resolve) => {
            // 被取代的上一筆「想要」不再送出，但等它的人改等這一筆。
            const waiters = pre.wanted ? pre.wanted.waiters : [];
            waiters.push(resolve);
            pre.wanted = { revision, gen: this._gen(revision), waiters };
            this._pumpPrecheck();
        });
    }

    _release(job) {
        if (job) {
            job.waiters.forEach((resolve) => resolve());
            job.waiters = [];
        }
    }

    /**
     * 同一時間最多一個預檢請求在飛（後端每個預檢可能 kubectl exec 載入整個 Odoo registry）。
     * 飛行中又換了版本：只記最新想要的，等在飛的那個結束再送，所以連點 N 個版本最多 2 個請求。
     * 回應回來時：世代已過期（期間重試或被拒絕過）就整個丟掉；否則成功的結果進 cache、
     * 且只在它仍是選取中的版本時才顯示（選的版本已變就不顯示，閘門維持 fail-closed）。
     */
    async _pumpPrecheck() {
        const pre = this._pre;
        if (pre.inflight || !pre.wanted) {
            return;
        }
        if (pre.destroyed) {
            this._release(pre.wanted);
            pre.wanted = null;
            return;
        }
        const job = pre.wanted;
        pre.wanted = null;
        pre.inflight = job;
        let result;
        try {
            result = await cloudService.checkRollbackAddons(this.props.service.id, job.revision);
        } catch (err) {
            result = { success: false, code: "check_failed", error: err && err.message };
        }
        pre.inflight = null;
        if (pre.destroyed) {
            this._release(job);
            this._release(pre.wanted);
            pre.wanted = null;
            return;
        }
        const current = job.gen === this._gen(job.revision);
        if (current && classifyRollbackPrecheck(result).kind !== "failed") {
            pre.cache.set(job.revision, result);
        }
        const next = pre.wanted;
        if (next) {
            if (pre.cache.has(next.revision)) {
                // 剛回來的就是下一個想要的版本（例如 A→B→A）：不必再送。
                pre.wanted = null;
                if (this.state.selectedRevision === next.revision) {
                    this._showPrecheck(next.revision, "ready", pre.cache.get(next.revision));
                }
                this._release(next);
            } else {
                this._release(job);
                return this._pumpPrecheck();
            }
        } else if (current && this.state.selectedRevision === job.revision) {
            this._showPrecheck(job.revision, "ready", result);
        }
        this._release(job);
    }

    /** 重試（忽略 cache）；沒選版本或回復中就什麼都不做。 */
    retryPrecheck() {
        if (this.state.selectedRevision !== null && !this.state.rolling) {
            return this.startPrecheck(this.state.selectedRevision, { force: true });
        }
    }

    /**
     * #1526：chart 版本比目前部署中舊的 revision 不能回復（後端與 operator 都會擋）。
     * 判準由後端算好放在 `rollback_allowed`；舊後端沒有這個欄位時一律可選（照舊）。
     */
    isBlocked(revision) {
        return revision.rollback_allowed === false && !this.isCurrent(revision);
    }

    /**
     * INFRA-FIX-018-1：「最多保留最近 N 個版本」。N 由 operator 回報（HELM_HISTORY_MAX），
     * 前端不寫死；舊後端沒有回報時不顯示這一行。
     */
    get historyHint() {
        const max = this.state.historyMax;
        if (!Number.isInteger(max) || max < 1) {
            return "";
        }
        return _t(
            "Only the most recent %s revisions are kept (failed updates included); older revisions can no longer be restored.",
            max
        );
    }

    get hasBlockedRevisions() {
        return this.state.revisions.some((r) => this.isBlocked(r));
    }

    /**
     * #1526：範本有 password 型的可調設定（API 金鑰等）時，這些值在回復時維持目前的值
     * （後端 `_rollback_password_spec_paths`，同一份 helm_value_specs）。
     */
    get keepsApiKeys() {
        const specs = this.props.service.template?.helm_value_specs || {};
        return ["required", "optional"].some((group) =>
            (specs[group] || []).some(
                (spec) =>
                    spec &&
                    spec.type === "password" &&
                    typeof spec.key === "string" &&
                    !spec.key.startsWith("_init.")
            )
        );
    }

    /**
     * #1433：平台管的管理員帳密在回復時保留目前的值（後端改走保留憑證的回復）。
     * 判準鏡射 `template.has_admin_gui`——與後端 `_rollback_preserved_value_paths`
     * 同一個欄位，前端不自建 slug 名單。
     */
    get keepsAdminCredentials() {
        return !!this.props.service.template?.has_admin_gui;
    }

    _revisionByNumber(number) {
        return this.state.revisions.find((r) => r.revision === number) || null;
    }

    /** 確認步驟的 chart 版本差異；兩邊相同或缺資料時回 null（不顯示這一行）。 */
    get chartChange() {
        const current = this._revisionByNumber(this.currentRevision);
        const target = this._revisionByNumber(this.state.selectedRevision);
        if (!current || !target || !current.chart || !target.chart) {
            return null;
        }
        if (current.chart === target.chart) {
            return null;
        }
        return { from: current.chart, to: target.chart };
    }

    onBack() {
        if (!this.state.rolling) {
            this.state.confirming = false;
        }
    }

    isSelected(revision) {
        return this.state.selectedRevision === revision.revision;
    }

    isCurrent(revision) {
        return revision.revision === this.currentRevision;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.rolling) {
            this.props.onClose();
        }
    }

    /**
     * 依 Odoo 使用者語系格式化 helm revision 的時間戳。
     *
     * 輸入來自 paas-operator 的 `helm history --output json`，
     * 實測長相為 `2026-08-26T10:44:22.962052+08:00`（帶 offset）。
     */
    formatDate(dateStr) {
        return formatUserDateTime(dateStr, { showSeconds: false }, "-");
    }

    /**
     * #1560：revision 的狀態是 helm 原文（deployed／superseded／failed／pending-*…），
     * 原本直接顯示英文。已知值翻成中文，未知值照原文顯示（不吞掉資訊）。
     * ⚠ "Failed" 這個 msgid 在其他模組有兩種譯法（失敗／失敗的），撞名時畫面顯示哪一個
     * 是隨機的（見 zh_TW.po 檔頭），所以這裡用不撞名的字面值。
     */
    statusLabel(status) {
        const labels = {
            deployed: _t("Deployed"),
            superseded: _t("Superseded"),
            failed: _t("Update failed"),
            "pending-install": _t("In progress"),
            "pending-upgrade": _t("In progress"),
            "pending-rollback": _t("In progress"),
            unknown: _t("Unknown"),
        };
        return labels[status] || status || "";
    }

    /**
     * #1560：helm 的 revision 說明（"Install complete"／"Upgrade complete"／
     * "Rollback to 3"）。常見的三種翻成中文；其他（多為錯誤訊息原文）照原文顯示。
     */
    revisionDescription(description) {
        const text = String(description || "").trim();
        if (text === "Install complete") {
            return _t("Installed");
        }
        if (text === "Upgrade complete") {
            return _t("Updated");
        }
        // 平台回復（#1433／#1526）寫入的說明帶「(credentials preserved)」尾註，
        // 一併吃掉，否則整句會以英文原文顯示（AI-HER-007-2）。
        const rollback = /^Rollback to (\d+)(?: \(credentials preserved\))?$/.exec(text);
        if (rollback) {
            return _t("Rolled back to revision %s", rollback[1]);
        }
        return text;
    }

    getStatusClass(status) {
        const statusMap = {
            deployed: "o_woow_revision_success",
            superseded: "o_woow_revision_superseded",
            failed: "o_woow_revision_failed",
            pending: "o_woow_revision_pending",
        };
        return statusMap[status] || "";
    }

    async onRollback() {
        if (!this.canRollback) {
            return;
        }
        if (!this.state.confirming) {
            this.state.confirming = true;
            this.state.error = null;
            return;
        }
        await this.confirmRollback();
    }

    /**
     * 送出回復。按鈕的 disabled 不是唯一一道閘：這裡自己再驗一次（#2069），
     * 預檢未過、未勾「我了解」就不送。後端 `_rollback_service` 仍會重算、是最後權威。
     */
    async confirmRollback() {
        if (!this.canRollback) {
            return;
        }

        this.state.rolling = true;
        this.state.error = null;

        // 只有 unverified 且使用者已勾選才帶旗標；其他情況連參數都不傳。
        const acknowledge = this.sendsAcknowledgement;
        let result;
        try {
            result = acknowledge
                ? await cloudService.rollbackService(
                      this.props.service.workspace_id,
                      this.props.service.id,
                      this.state.selectedRevision,
                      { acknowledgeUnverifiedAddons: true }
                  )
                : await cloudService.rollbackService(
                      this.props.service.workspace_id,
                      this.props.service.id,
                      this.state.selectedRevision
                  );
        } catch (err) {
            // 真正的 cloudService 不會 reject；萬一丟例外，結果同樣未知。
            result = { success: false, code: ROLLBACK_REQUEST_FAILED, error: err && err.message };
        }

        if (result.success) {
            // 後端已經回復成功：卸載後也照樣通知父層（由它重新載入服務）。
            this.props.onRollback();
            return;
        }
        if (this._pre.destroyed) {
            // 回復期間對話框被卸載（瀏覽器上一頁）：不寫 state、不再送預檢。
            return;
        }
        const unknownOutcome = result.code === ROLLBACK_REQUEST_FAILED;
        this.state.error = unknownOutcome
            ? _t(
                  "The request did not finish, so the rollback may already have started. Check the service status before trying again."
              )
            : result.error || _t("Failed to rollback service");
        this.state.rolling = false;
        if (unknownOutcome || ROLLBACK_REFUSAL_CODES.includes(result.code)) {
            // 後端在動作當下重算後拒絕＝畫面上的預檢結果已過期（服務在預檢後停了、期間又裝了
            // 模組…）；請求沒跑完＝回復可能已經開始、服務狀態不明。兩者都回選版本步驟、清掉
            // 勾選，並忽略 cache 對同一版本重新預檢，否則會留著過期的綠色面板，
            // 且重選同一版本不會重跑預檢。
            this.state.confirming = false;
            this.startPrecheck(this.state.selectedRevision, { force: true });
        }
    }
}
