# How `intended_flow.bpmn` was derived

This BPMN captures the trusteeship-application flow in three phases. The previous version was a linear five-step happy path; you flagged it as too sequential and laid out a richer design — initial AI assessment, two event-driven loops with conditional skip gateways, and a terminal `mark_ready_for_court`. This document explains how that design lands in BPMN.

> **⚠️ The current `src/system_prompt.md` is a STUB.** The agent routes events to tools correctly (deterministic) but randomises the `is_complete` outcomes (~60/40 lean toward True) instead of inspecting actual applicant documents. This produces realistic trace shapes — happy paths, loop iterations, the occasional reject — for the process-mining pipeline without needing real test data. Switching to substantive assessment means replacing the prompt only; the tools, BPMN, and entrypoint don't change. See §4 below for what each tool would do under substantive assessment.

Companion docs: [dispute/intended_flow.md](../dispute/intended_flow.md), [research_react/intended_flow.md](../research_react/intended_flow.md), [research_graph/intended_flow.md](../research_graph/intended_flow.md).

---

## 1. Shape

```
Phase 1 (linear, runs once per case):
  [Start: TrusteeshipRequested] → [CreateApplication] → [assess_initial_state]
                                                                ↓
                                              ⬦ App + Medical already complete?
                                              │ yes ─────────────────────┐
                                              └ no                       │
                                                ↓                        │
Phase 2 (event loop, may run zero or many times):                        │
                                          ⬨ Wait for application/medical │
                                              │                          │
                                ┌─────────────┴─────────────┐            │
                                ⊠ ApplicationUpdate          ⊠ MedicalUpdate
                                       ↓                            ↓    │
                                [assess_application]         [assess_medical]
                                       ↓                            ↓    │
                                       └────────────┬───────────────┘    │
                                                    ↓                    │
                                          ⬦ App + Medical now complete?  │
                                          │ no ─ loop back ─┘            │
                                          └ yes                          │
                                                    ↓                    │
                                       ┌────────────┘────────────────────┘
                                       ↓
                              ⬦ All 3 checks already complete?
                              │ yes ─────────────────────────────────┐
                              └ no                                   │
                                ↓                                    │
Phase 3 (event loop, may run zero or many times):                    │
                          ⬨ Wait for background-check event          │
                                │                                    │
                  ┌─────────────┼─────────────┐                       │
                  ⊠ CreditCheck  ⊠ CriminalCheck  ⊠ PersonalRefs       │
                        ↓             ↓               ↓               │
                  [assess_credit] [assess_criminal] [assess_refs]     │
                        └─────────────┼─────────────┘                 │
                                      ↓                               │
                          ⬦ All 3 checks now complete?                │
                          │ no ─ loop back ─┘                         │
                          └ yes                                       │
                                ↓                                     │
                                └────────────────────────────────────┘
                                ↓
                        [mark_ready_for_court] → [End: ReadyForCourt]
```

21 elements (1 start + 8 service tasks + 2 event-based gateways + 4 exclusive gateways + 5 catch events + 1 end) and 27 sequence flows. Validated end-to-end.

## 2. Translation from the spec

Your spec defined the structure phase by phase. Each piece maps directly:

| Spec phrase | BPMN element(s) |
|---|---|
| "Start Event (request received)" | `start` (messageStartEvent, name `TrusteeshipRequested`) |
| "System Task to create a case to track" | `task_create_application` (CreateApplication — manual OTel span) |
| "AI Assessment of what is currently in place and what is pending" | `task_assess_initial_state` (backed by the `assess_initial_state` `@tool`) |
| "This part is needed only if medical and application are not yet completed" | `gw_skip_phase2` — exclusive gateway with one outgoing edge to Phase 2 and one bypass edge to the Phase 3 entry gateway |
| "event loop that waits for an update to the application or an update to the medical assessment" | `gw_phase2_wait` (event-based gateway) + `event_application_update` + `event_medical_update` (intermediate catch events with messageEventDefinition) |
| "In each case, there is a system task for AI to assess completeness" (Phase 2) | `task_assess_application`, `task_assess_medical` — each backed by its own `@tool` |
| "Last is another event loop for credit check, criminal check, and personal references check" | `gw_phase3_wait` + the three corresponding catch events |
| "AI assesses completeness" (Phase 3) | `task_assess_credit_check`, `task_assess_criminal_check`, `task_assess_personal_references` |

The two pieces I added that you didn't ask for explicitly but felt necessary:

- **`gw_skip_phase3`** by symmetry with `gw_skip_phase2`. If by the time Phase 2 finishes (or is skipped) the three background checks are already all complete, there's no reason to enter Phase 3. Matches your "only if not yet completed" intent.
- **`gw_phase2_continue` and `gw_phase3_continue`** — exclusive gateways immediately after each AI assessment that decide "loop back for the next event" vs. "this phase is done, move on". The "AI assesses completeness" step naturally produces a boolean outcome; these gateways are how that boolean turns into routing. Without them, the BPMN couldn't model the loop properly.

## 3. The five items

The previous iteration had `legal_application`, `medical_proof`, `personal_references`, `reference_checks`, `credit_check`. The new design has:

| Item | Phase |
|---|---|
| `application` | 2 |
| `medical` | 2 |
| `credit_check` | 3 |
| `criminal_check` | 3 — new in this iteration |
| `personal_references` | 3 — now covers both *provided* and *checked* |

`reference_checks` is gone — it was a separate item only because the previous five-step linear design wanted finer-grained step gating. With the event-based design, "references provided" and "references checked" are two updates against the same item; the `assess_personal_references` tool reads the case state and decides `is_complete` once everything is in place.

`criminal_check` is new — wasn't in your original five-step spec, but is in this redesign's Phase 3.

## 4. Tool design

Eight `@tool` functions in [src/trusteeship_agent.py](src/trusteeship_agent.py):

| Tool | Purpose | Args |
|---|---|---|
| `assess_initial_state` | Phase 1 one-shot; reports which items are already complete in the initial request payload | `items_already_complete: list[str], notes: str` |
| `assess_application` | Phase 2; called on `ApplicationUpdate` | `is_complete: bool, notes: str` |
| `assess_medical` | Phase 2; called on `MedicalUpdate` | same |
| `assess_credit_check` | Phase 3; called on `CreditCheckUpdate` | same |
| `assess_criminal_check` | Phase 3; called on `CriminalCheckUpdate` | same |
| `assess_personal_references` | Phase 3; called on `PersonalReferencesUpdate` | same |
| `mark_ready_for_court` | Terminal success | `notes: str` |
| `reject_application` | Terminal failure (unrecoverable) | `step: str, reason: str` |

**Design choice — `is_complete` as an arg, not a separate tool.** Two tools per item (`mark_X_complete` / `mark_X_incomplete`) would give finer granularity in the trace but would also double the tool count. With `is_complete` as an arg, the trace shows the assessment activity once per event; the result is in args/decisions. Conformance at the BPMN level works either way; the choice is a trace-density trade-off and I went for fewer tools.

**Design choice — Phase 1 is one comprehensive tool, not five per-item assessments.** The initial state assessment is genuinely *comprehensive* — the AI looks at everything submitted and reports what's already covered. Splitting that into five separate tool calls would mean five separate spans for what is logically one assessment. One tool, one span, one BPMN activity.

## 5. Conversational continuity within a session (no AgentCore Memory)

The applicant interaction is conversational. When the agent calls `assess_application(is_complete=False, notes="sections 4 and 5 are missing")` the applicant may have those sections ready *right now* and respond immediately with the revised application. The agent should connect that response to its prior request, not re-reason from a cold case state.

We do this **without AgentCore Memory**. Two state layers:

| Layer | Where it lives | What it captures | Survives container restart? |
|---|---|---|---|
| Case state | DynamoDB, keyed by `application_id` | Step statuses, history, decisions — the durable record | Yes |
| Conversation state | `_session_agents: dict[session_id, Agent]` in `trusteeship_agent.py` | The Strands Agent object and its internal LLM conversation history | No |

Within one AgentCore session the same `Agent` instance is reused across invocations, so the LLM remembers what it previously asked the applicant for. AgentCore Runtime gives each session sticky affinity to one container, so an in-memory dict is enough. On restart the cache is lost but the case is intact — the agent rebuilds from the case state and resumes; only the conversational style of the in-flight exchange is gone.

A consequence: an invocation can finish **without any tool call**. If the applicant sends a non-actionable message ("got it, I'll have the medical next week"), the agent responds conversationally and the entrypoint persists nothing new to the case. The trace for that invocation contains no activity span; conformance ignores it.

This is not AgentCore Memory. It's a one-line cache. Trade-offs:

- Cheaper than AgentCore Memory (no managed service).
- Less durable than AgentCore Memory (container-local).
- No semantic / preferences retrieval strategies — the agent only sees prior turns of the same conversation, not extracted facts.
- All conversation cleanup is the application's problem (TTL / LRU eviction not implemented; sessions are typically short enough that this hasn't mattered yet).

For the research_react / research_graph agents the trade-off goes the other way — their state IS the conversation, and Memory's retrieval strategies are worth it. For the trusteeship and dispute agents the conversation is incidental to the case, so the lighter cache fits.

## 6. The two-tools-in-one-turn relaxation

Across the project's other agents, the convention is **exactly one tool per invocation**. This BPMN breaks it deliberately at one specific point: when the agent's `assess_*` call would make all five items complete, the agent is also expected to call `mark_ready_for_court` in the same turn.

Why: the BPMN routes directly from `gw_phase3_continue [yes]` (or `gw_skip_phase3 [yes]`) into `mark_ready_for_court`, with no waiting in between. The natural implementation is one Strands invocation where the agent loop calls two tools in sequence.

The agent module reflects this — `_current_actions` is a **list**, not a single dict, and the entrypoint applies every recorded action to the case in order. The system prompt is explicit about when the second tool is allowed.

## 7. Per-invocation vs. per-lifecycle (cross-invocation case)

Each external event arrives as its own AgentCore invocation, which is its own X-Ray trace. The BPMN models the **full case lifecycle** across many invocations:

| Invocation | Trigger | Tools fired in this trace |
|---|---|---|
| 1 | TrusteeshipRequested | `assess_initial_state` (Phase 1) |
| 2 | ApplicationUpdate | `assess_application` |
| 3 | MedicalUpdate | `assess_medical` |
| 4 | CreditCheckUpdate | `assess_credit_check` |
| 5 | CriminalCheckUpdate | `assess_criminal_check` |
| 6 | PersonalReferencesUpdate | `assess_personal_references` + `mark_ready_for_court` (this is the two-tool turn) |

The order of invocations 2–6 is *not* fixed — events arrive when their producers produce them, in any order, possibly with retries. The BPMN handles this through the event-based loops (whichever event arrives first wins the wait gateway).

End-to-end conformance against this BPMN requires assembling per-invocation X-Ray traces by `application_id`. `process_mining/xray_to_xes.py` currently uses the X-Ray trace id as the case id — the future switch to `case_id = application_id` (mentioned in earlier docs) is what lights this up.

## 8. Conformance properties

Assuming traces are assembled by `application_id`:

1. **Every case begins with `CreateApplication`, then `assess_initial_state`.** No exceptions.
2. **Phase 2 is entered iff the initial assessment did not mark both `application` and `medical` as already complete.** Detectable from the initial assessment's args; conforming to skip is fine.
3. **Within Phase 2, every `assess_application` or `assess_medical` activity is preceded somewhere up the trace by the corresponding catch event** (semantically: an `ApplicationUpdate` or `MedicalUpdate` event was the trigger).
4. **Phase 3 is entered iff the three background-check items are not all already complete at the time Phase 2 exits (or is skipped).** Same shape as #2.
5. **`mark_ready_for_court` appears exactly once, at the end.** Multiple occurrences = bug; zero occurrences = case still in flight (not non-conforming, just not done).
6. **`reject_application` is non-conforming.** Same as the previous iteration — it's an off-happy-path tool that's defined but doesn't appear in the BPMN.

The skip-gateway optimisations (Phase 2 / Phase 3 short-circuited because items were already complete) make the per-case event sequence variable, but each variant is a legal path through this BPMN.

## 9. Open shape questions (deferred)

- **Out-of-order events and partial completeness.** `ApplicationUpdate` and `MedicalUpdate` can arrive in any order; same for Phase 3's three events. The BPMN allows this. What it doesn't yet model: `assess_*` calls that flip an item from `complete` back to `pending` because a later update revealed an issue. Possible but rare; not yet modeled.
- **Failure paths.** `reject_application` exists in the agent but not in the BPMN. If real cases start producing it in significant volume, it should become a BPMN end event.
- **Timeouts on the event loops.** Real cases stall (the applicant never sends the credit check). A timer event on each event-based gateway would convert the smart deferred choice into a discriminator (timeout vs. event). Not yet modeled.
- **Parallel reference-check sub-loop.** Personal references in real life are checked one reference at a time. Currently `assess_personal_references` is a single activity that says "all references checked, all positive". A future iteration might model the per-reference check as a multi-instance sub-process — but that's only worth doing if there's a process-mining question that depends on it.

## 10. Visualizing in bpmn.io

1. Open <https://demo.bpmn.io/new>.
2. Drag-and-drop [intended_flow.bpmn](intended_flow.bpmn), or paste the XML.
3. The supplied DI is best-effort and the diagram is large (~1900px × 900px). Main spine runs left-to-right at the vertical midpoint; Phase 2 branches above the spine; Phase 3 branches below. The two long loop-back edges (`gw_phase2_continue → gw_phase2_wait` and `gw_phase3_continue → gw_phase3_wait`) route around the outside.
4. *Layout → Auto-layout* in Camunda Modeler tidies the routing if it looks tangled.
