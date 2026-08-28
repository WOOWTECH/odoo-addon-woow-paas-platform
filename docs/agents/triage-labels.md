# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-discussion`   | Unevaluated intake needs discussion      |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

`needs-triage` represents an intake that has not yet been evaluated, so the new six-state taxonomy has no native equivalent. Map it to `needs-discussion` as the most conservative fallback: unlike `needs-decision`, it does not falsely claim that options have already been prepared. Phase 1 showed that 19/19 option-decision issues did not fit `needs-triage`; once options are clearly listed, move the issue to `needs-decision`.

## Work-state labels

The work-state axis has six mutually exclusive values:

| Work-state            | Meaning                                           |
| --------------------- | ------------------------------------------------- |
| `needs-discussion`    | Needs exploration before decision options are clear |
| `needs-decision`      | Decision options are ready for a human choice     |
| `needs-info`          | Waiting for additional information                |
| `ready-for-agent`     | Fully specified and ready for an agent             |
| `ready-for-human`     | Ready for or requires human implementation        |
| `wontfix`             | Will not be actioned                               |

An open issue must carry only one work-state at a time. Close resolved issues directly; do not use an active work-state as a substitute for closing them.

`derived` is orthogonal provenance and does not count toward the mutually exclusive work-state. `deprecated:pending` is retained only for closed issue history and must not be newly applied. The legacy `pending` and `cat-discuss` values are no longer part of the active taxonomy: the `cat-discuss` label object has been deleted, and `pending` has been renamed.

## Agent context labels

Use exactly one of these labels when an issue is assigned to an agent execution context:

| Label                       | Meaning                              |
| --------------------------- | ------------------------------------ |
| `agent-context:local-cc`    | 由本機 Claude Code agent 執行        |
| `agent-context:remote-pi`   | 由遠端 Pi agent 執行                 |

## Agent status labels

| Label                        | Meaning                                      |
| ---------------------------- | -------------------------------------------- |
| `agent-status:in-progress`   | Agent 已領取並正在處理；完成或停止時移除     |

## Agent assignee labels

Gitea assignee 只能是 Gitea user account；agent identity 改用以下 labels。Issue 交派給 agent 時只使用其中一個：

| Label                       | Meaning                 |
| --------------------------- | ----------------------- |
| `agent-assignee:leader1`    | 交由 leader1 agent 處理 |
| `agent-assignee:leader2`    | 交由 leader2 agent 處理 |
| `agent-assignee:leader3`    | 交由 leader3 agent 處理 |
| `agent-assignee:leader4`    | 交由 leader4 agent 處理 |
| `agent-assignee:leader5`    | 交由 leader5 agent 處理 |
