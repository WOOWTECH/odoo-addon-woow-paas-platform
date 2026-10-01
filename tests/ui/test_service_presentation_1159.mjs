import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const helperUrl = new URL(
  "../../src/static/src/paas/utils/service_presentation.js",
  import.meta.url,
);
const source = await readFile(helperUrl, "utf8");
const testableSource = source.replace(
  'import { _t } from "@web/core/l10n/translation";',
  'const _t = (message, ...args) => message.replace("%s", args[0]);',
);
const presentation = await import(
  `data:text/javascript;base64,${Buffer.from(testableSource).toString("base64")}`,
);

test("init 失敗只顯示固定文案，且 seq=0 不顯示 #0", () => {
  const text = presentation.getInitFailureText({
    state: "error",
    init_exit: "fast_fail",
    init_attempt_seq: 0,
    init_reconfigure_allowed: true,
    error_message: "pod/init-7f96d operator detail",
  });

  assert.equal(
    text,
    "Initialization did not complete. You can change the configuration to try again, or contact support.",
  );
  assert.doesNotMatch(text, /pod|operator|#0/);
});

test("init 重試提示讀後端 capability，並帶正數 attempt 序號", () => {
  assert.equal(
    presentation.getInitFailureText({
      state: "error",
      init_exit: "failed",
      init_attempt_seq: 3,
      init_reconfigure_allowed: true,
    }),
    "Initialization did not complete (attempt #3). You can change the configuration to try again, or contact support.",
  );
  assert.equal(
    presentation.getInitFailureText({
      state: "error",
      init_exit: "failed",
      init_attempt_seq: 3,
      init_reconfigure_allowed: false,
    }),
    "Initialization did not complete (attempt #3). Please contact support.",
  );
});

test("pending 觀測與最後真觀測使用不同文案，僅轉移中超過三分鐘才顯示", () => {
  const pending = presentation.getStaleObservation({
    state: "initializing",
    observed_age_seconds: 181,
    observation_pending: true,
  });
  const observed = presentation.getStaleObservation({
    state: "upgrading",
    observed_age_seconds: 240,
    observation_pending: false,
  });

  assert.deepEqual(pending, { minutes: 3, observationPending: true });
  assert.equal(
    presentation.getStaleObservationLabel(pending),
    "Waiting for status update 3 min",
  );
  assert.equal(
    presentation.getStaleObservationLabel(observed),
    "Last updated 4 min ago",
  );
  assert.equal(
    presentation.getStaleObservation({
      state: "running",
      observed_age_seconds: 999,
      observation_pending: true,
    }),
    null,
  );
});

test("Projects 關閉或 support 載入中不建立 project，建立視窗開啟時也防重入", () => {
  assert.equal(
    presentation.getSupportProjectAction({
      projectsVisible: false,
      supportProject: null,
      supportProjectLoading: false,
      createModalOpen: false,
    }),
    null,
  );
  assert.equal(
    presentation.getSupportProjectAction({
      projectsVisible: true,
      supportProject: null,
      supportProjectLoading: true,
      createModalOpen: false,
    }),
    null,
  );
  assert.equal(
    presentation.getSupportProjectAction({
      projectsVisible: true,
      supportProject: null,
      supportProjectLoading: false,
      createModalOpen: true,
    }),
    null,
  );
  assert.equal(
    presentation.getSupportProjectAction({
      projectsVisible: true,
      supportProject: null,
      supportProjectLoading: false,
      createModalOpen: false,
    }),
    "create",
  );
  assert.equal(
    presentation.getSupportProjectAction({
      projectsVisible: true,
      supportProject: { id: 42 },
      supportProjectLoading: false,
      createModalOpen: false,
    }),
    "open",
  );
});
