/** @odoo-module **/
import { useState, onMounted, onWillUnmount } from "@odoo/owl";
import { jsonRpc } from "./rpc";
import { createResourcePoller } from "../utils/resource_polling";
import { retainResourceSamples } from "../utils/resource_observations";

/** Batch the visible workspace's services; detail uses the same API with one ID. */
export function useServiceResources(workspaceId, getServiceIds) {
  const state = useState({ observations: {}, loading: true, refreshFailed: false, now: Date.now() });
  let disposed = false;
  const ids = () => [...new Set(getServiceIds())].filter((id) => Number.isInteger(id) && id > 0).sort((a, b) => a - b);
  const poller = createResourcePoller({
    async load() {
      const requestedIds = ids();
      if (!requestedIds.length) return { requestedIds, services: {} };
      if (requestedIds.every((id) => !state.observations[id])) state.loading = true;
      // The API accepts at most 100 IDs. Larger workspaces still use batches,
      // never one request per card, with no overlapping polling rounds.
      const services = {};
      for (let offset = 0; offset < requestedIds.length; offset += 100) {
        if (disposed || document.visibilityState === "hidden") throw new Error("Resource observation cancelled");
        const result = await jsonRpc(`/api/workspaces/${workspaceId}/service-resource-observations`, {
          service_ids: requestedIds.slice(offset, offset + 100),
        }, 20000);
        if (!result?.success || !result.data?.services) throw new Error("Resource observation unavailable");
        Object.assign(services, result.data.services);
      }
      return { requestedIds, services };
    },
    onData({ requestedIds, services }) {
      if (requestedIds.join(",") !== ids().join(",")) {
        poller.refresh();
        return;
      }
      const observations = {};
      for (const id of requestedIds) {
        observations[id] = retainResourceSamples(state.observations[id], services[id] || {
          status: "unavailable",
          cpu: { status: "unavailable" }, memory: { status: "unavailable" }, storage: { status: "unavailable" },
        });
      }
      state.observations = observations;
      state.now = Date.now();
      state.loading = false;
      state.refreshFailed = false;
    },
    onError() {
      state.now = Date.now();
      state.loading = false;
      state.refreshFailed = true;
    },
  });
  onMounted(() => poller.start());
  onWillUnmount(() => {
    disposed = true;
    poller.stop();
  });
  return { state, refresh: poller.refresh };
}
