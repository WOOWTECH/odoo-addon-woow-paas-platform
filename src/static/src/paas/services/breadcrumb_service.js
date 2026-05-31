/** @odoo-module **/
import { reactive } from "@odoo/owl";

/**
 * 麵包屑情境 store（breadcrumb context store）
 *
 * 詳情頁（task / project / workspace / service detail）載入資料後，會把自己
 * 「帶記錄名、且連回真實所屬位置」的麵包屑軌跡 publish 到這裡，Header 統一
 * 讀取後渲染成唯一一條全域麵包屑。各詳情頁因此不再自帶本地麵包屑，避免
 * 「頂部一條、本地一條、彼此叫法與連結都不同」的舊問題。
 *
 * 只有當 trail 是為「目前 active 的 route」publish 的，Header 才會採用；route
 * 不相符時（例如剛切到別頁、新頁還沒 publish）Header 自動 fallback 回它原本
 * 依 routeName 推算的泛用麵包屑，因此未改動的頁面（billing / settings 等）照常
 * 運作，也不會把上一頁殘留的 trail 帶到下一頁。
 *
 * trail item 結構：{ label: string, path: string|null }
 *   - path 為 null → 目前頁面（渲染成不可點的 active 段）
 *   - path 有值   → 可點，透過 router.navigate(path) 導向
 * trail 不包含根節點「Home」；Header 一律自動在最前面補上。
 */
export const breadcrumbService = reactive({
    /** @type {string|null} 這份 trail 是為哪個 router.current（routeName）publish 的 */
    routeName: null,
    /** @type {Array<{label: string, path: string|null}>} */
    trail: [],

    /**
     * Publish 指定 route 的麵包屑軌跡。
     * @param {string} routeName - 此 trail 所屬的 router.current
     * @param {Array<{label: string, path: string|null}>} trail
     */
    set(routeName, trail) {
        this.routeName = routeName;
        this.trail = trail;
    },

    /**
     * 取得為 `routeName` publish 的 trail；route 不相符或尚未 publish 時回 null，
     * 讓 Header 改用泛用 fallback。
     * @param {string} routeName
     * @returns {Array<{label: string, path: string|null}>|null}
     */
    trailFor(routeName) {
        return this.routeName === routeName && this.trail.length ? this.trail : null;
    },
});
