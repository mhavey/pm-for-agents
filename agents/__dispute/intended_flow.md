# How `intended_flow.bpmn` was derived

This BPMN captures the **main structural skeleton** of the dispute process. Per-event actions, terminal end events, and the context-dependent guards on which events are valid in a given state are deliberately not yet modeled — the user will define those next.

What *is* settled:

- The case starts on a `CustCreate` event.
- Two system tasks run before the case enters the loop: `CreateDispute` (persists the record) and `AIAssessDisputeReadiness` (the AI's initial assessment).
- The case then parks at an event-based gateway that can resolve on any of **seven** external events, depending on the case's current state and what's plausible at that point.

Companion docs for the other agents: [research_react/intended_flow.md](../research_react/intended_flow.md), [research_graph/intended_flow.md](../research_graph/intended_flow.md).

---

## 1. Shape

```
[Start: CustCreate] ─→ [CreateDispute] ─→ [AIAssessDisputeReadiness] ─→ ⬨ Event loop
                                                                          ├─ ⊠ CustUpdate                    ──┐
                                                                          ├─ ⊠ CustCancel                    ──┤
                                                                          ├─ ⊠ OpsReject                     ──┤
                                                                          ├─ ⊠ OpsWriteoff                   ──┤  (each event currently
                                                                          ├─ ⊠ OpsReviewApproved             ──┤   loops back to the
                                                                          ├─ ⊠ OpsReviewCustFeedbackNeeded   ──┤   gateway — placeholder
                                                                          └─ ⊠ OpsReviewArbitrationNeeded    ──┘   for the action TBD)
```

Eleven BPMN elements: 1 message start event, 2 service tasks, 1 event-based gateway, 7 message catch events. No end events yet — terminal paths come with the per-event actions.

## 2. Translation from the user's spec

The user described it directly:

> Initial Event (CustCreate) → System Task (CreateDispute) → System Task (AIAssessDisputeReadiness) → main loop that receives on these events and takes action.

Each phrase maps one-to-one to a BPMN element:

| User's phrase | BPMN element | Type |
|---|---|---|
| Initial Event (CustCreate) | `start` | `startEvent` + `messageEventDefinition` |
| System Task (CreateDispute) | `task_create_dispute` | `serviceTask` |
| System Task (AIAssessDisputeReadiness) | `task_ai_assess_readiness` | `serviceTask` |
| main loop that receives on these events | `gw_event_loop` | `eventBasedGateway` |
| `CustUpdate` | `event_cust_update` | `intermediateCatchEvent` + `messageEventDefinition` |
| ...(six more events)... | `event_*` | (same) |

For this iteration, **each catch event's outgoing edge loops directly back to `gw_event_loop`**. That's a placeholder. The user said "I will define those actions soon" — when actions land, those loop-back edges will be replaced by handler tasks plus either a continued loop or a terminal end event.

## 3. The two manually-instrumented OTel spans

Two BPMN activities (`CreateDispute` and `AIAssessDisputeReadiness`) are backed by explicit OTel spans in the AgentCore entrypoint, same scoped-exception pattern as before:

- `tracer.start_as_current_span("CreateDispute")` wraps `save_case(case)`, but **only when `is_new_case` is True**. Subsequent invocations update the record without re-emitting the span — `CreateDispute` is a once-per-case-lifecycle activity per the BPMN.
- `tracer.start_as_current_span("AIAssessDisputeReadiness")` wraps the `agent(...)` call, again **only on the first invocation**. Event-driven subsequent invocations don't emit this span — the agent's job in those is event handling, not readiness assessment.

Both span names are registered in `process_mining/xray_to_xes.py`'s `DEFAULT_TOOL_NAMES` so the XES extractor picks them up automatically. Span names are PascalCase here to match the BPMN element names exactly (the @tool-decorated functions keep their snake_case names because that's the convention for tools — see §5).

## 4. What changed vs. the previous iteration

The previous BPMN had `assess_dispute` branching through an exclusive gateway into four state-setting tools (`reject_dispute`, `write_off_dispute`, `request_more_docs`, `set_ready_for_review`), with the `request_more_docs` path opening a small event-based wait region.

The new design promotes the event-based wait to the **main backbone of the process** and demotes the four-way state branch to "TBD — what does each event do, and what state does the AI initially assess to?"

The five `@tool`-decorated assessment functions (`reject_dispute`, `write_off_dispute`, `request_more_docs`, `set_ready_for_review`, `cancel_dispute`) are still defined in [src/dispute_agent.py](src/dispute_agent.py). They are not currently invoked from any defined BPMN path. They survive as building blocks the per-event handlers will likely re-use — e.g., the `OpsReject` event's handler will almost certainly call `reject_dispute`. Removing them now would be wasted churn.

## 5. Naming convention

| Layer | Convention | Why |
|---|---|---|
| BPMN element *names* (display) | PascalCase — `CreateDispute`, `AIAssessDisputeReadiness`, `CustUpdate`, ... | Matches the user's vocabulary; reads naturally in a BPMN modeler |
| BPMN element *ids* | snake_case — `task_create_dispute`, `event_cust_update`, ... | Standard for machine-readable IDs |
| Manual OTel span names | PascalCase, matching BPMN names | So the trace event names match the BPMN activity names without remapping in conformance |
| `@tool`-decorated function names | snake_case — `reject_dispute`, `cancel_dispute`, ... | Python convention; `@tool` uses the function name as the span name automatically |

This is a small inconsistency — manual spans use PascalCase, tool spans use snake_case — but each follows the convention of its source. Conformance against the BPMN works as long as the BPMN's activity *names* match the trace span names; that's the case here for both.

## 6. Conformance properties (what's checkable today)

Limited until per-event actions are defined. What we can already check:

1. **First-invocation traces begin with `CreateDispute → AIAssessDisputeReadiness`.** A first-invocation trace that skips either is non-conforming.
2. **Event-driven invocations don't emit `CreateDispute` or `AIAssessDisputeReadiness`.** Both spans are guarded by `is_new_case` in the entrypoint.
3. **Event arrival presence.** Once handler actions are defined and emit spans, traces will be checkable for "every event-driven invocation includes exactly one of the seven event handlers" — that property is just promised, not enforced yet.

What's *not* yet checkable:

- Per-event handler correctness (handlers don't exist).
- Termination correctness (no end events).
- Context-dependent event validity ("CustUpdate is plausible after `moreDocsNeeded`; OpsReviewApproved is plausible after `readyForReview`") — those guards are on the BPMN to-do list.

## 7. Cross-invocation case lifecycle (unchanged from previous iteration)

Each external event arrives as its own AgentCore invocation, which is its own X-Ray trace. The BPMN models the full case lifecycle; per-invocation traces only cover sub-paths. For end-to-end conformance against this BPMN, traces must be assembled by `dispute_id`. The XES extractor's `Trace.case_id` is currently the X-Ray trace id; switching to a semantic case id (dispute_id) is the next process-mining step.

## 8. Visualizing in bpmn.io

1. Open <https://demo.bpmn.io/new>.
2. Drag-and-drop [intended_flow.bpmn](intended_flow.bpmn), or paste the XML.
3. The DI puts the main spine left-to-right and stacks the seven catch events vertically to the right of the gateway, with the loop-back edges going around the right side. Auto-layout in your modeler can tidy the loop-back routing if it feels cluttered — there's a lot going into one gateway.

## 9. What comes next

Per the user: per-event actions. Each catch event's outgoing edge will be replaced by:

- A handler task (or sequence of tasks) — likely re-using the existing `@tool` functions where they fit.
- Either a back-edge to `gw_event_loop` (the loop continues — e.g. CustUpdate triggers a re-assessment, then the case waits for the next event) or a terminal end event (the loop exits — e.g. OpsReject closes the case).
- Possibly conditional context guards on the `gw_event_loop` → `event_*` edges (some events only valid in some states).

The BPMN today is a frame; the user will paint the picture.
