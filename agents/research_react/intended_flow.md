UNDER CONSTRUCTION

# How `intended_flow.bpmn` was derived

This note captures the reasoning behind [intended_flow.bpmn](intended_flow.bpmn) — the BPMN reference model for the `research_react` agent's intended per-turn behavior. It's the document a process-mining conformance check will compare actual XES traces against, so the modeling choices matter.

The single source of truth was [src/system_prompt.md](src/system_prompt.md). Every gateway, branch, and edge below maps to a sentence (or the *absence* of a sentence) in that prompt.

---

## 1. Scope: per-turn, not per-conversation

A "case" in process mining is one execution of the process from start to end. For a ReAct agent on AgentCore, two natural choices exist:

- **Per-turn**: one user message → one response. One AgentCore invoke = one case.
- **Per-conversation**: a session of N turns, including multi-turn dances like the consent flow.

I went with **per-turn** because:

1. It matches what the X-Ray trace already captures — one trace per request.
2. `process_mining/xray_to_xes.py` defaults case_id to the X-Ray trace id, which is per-invoke.
3. Per-turn keeps the model legible. Multi-turn flows can be modeled later if conformance work at the conversation level becomes interesting.

Consequence: the consent dance crosses *two* per-turn cases — turn N's response includes "want a deep analysis?" (text inside `Compose response`); turn N+1's invoke arrives with the user's "yes" and is classified as `deep`. The BPMN models each turn independently.

## 2. Translating the prompt, sentence by sentence

The system prompt has three numbered sections. Each became a piece of the BPMN.

**Prompt §1 — choose your retrieval tool.**

> Topic similarity (the default — the user has a subject in mind): use `find_similar_articles`. […]
> Browse / discovery: use `sample_articles`. […] Do NOT use `sample_articles` when the user has a topic.

This is a deterministic choice on the user's intent → modeled as the `Classify intent` exclusive (XOR) gateway with two outgoing edges so far: `topic similarity` and `browse / discovery`.

**Prompt §2 — fallback rule.**

> If `find_similar_articles` returns nothing relevant — low scores or off-topic — and the question is plausibly about non-arXiv material, fall back to `web_search`. Otherwise loosen filters or rephrase the query and call `find_similar_articles` again before giving up.

Two things follow:

- A `Relevant hits?` XOR gateway *after* `find_similar_articles`, with `web_search` only on the "poor / off-topic" branch.
- `web_search` is **not** a direct outgoing edge from `Classify intent`. The prompt only authorizes `web_search` *after* a failed KB call. Adding a "non-arXiv" intent route to `web_search` would be over-modeling — the prompt doesn't say it.

The "loosen filters and retry" loop on `find_similar_articles` was omitted deliberately. Repeated invocations of the same tool show up as multiple events in the trace, but the *control flow* is unchanged. Modeling it as an explicit self-loop adds a gateway without adding a falsifiable conformance property.

**Prompt §3 — deep analysis must be consented.**

> `analyze_full_article` is EXPENSIVE and must NEVER be called on your own initiative. […] Only after the user replies with an explicit affirmative may you call `analyze_full_article`.

This is the most consequential rule, and it's where BPMN earns its keep. The rule is a constraint on the *graph topology*, not just on the agent's decisioning:

- `analyze_full_article` has exactly **one** incoming edge — from the `deep-analysis confirmed (prior consent)` outgoing edge of `Classify intent`.
- No other path leads to it.

That's a falsifiable property: any trace where `analyze_full_article` is preceded by `find_similar_articles`, `sample_articles`, `web_search`, or appears at the start of a turn without a prior consent context is non-conforming. The conformance checker will flag it without anyone needing to reason about prompt drift.

The 4th `Classify intent` outgoing edge (`conversational / no retrieval`) handles the case where the user just declined a deep-analysis offer or said "thanks". In those turns, the agent isn't supposed to retrieve anything — it should just compose a short reply. Modeled as a direct edge from `Classify intent` to the join gateway, skipping all four service tasks.

## 3. Element-type choices

| BPMN element | Used for | Why |
|---|---|---|
| `serviceTask` | the four tool calls | tools are system-invoked operations — service task is the canonical fit |
| `task` | `Compose response` | not a system call; mixed work (LLM reasoning + text composition). Generic `task` keeps it neutral |
| `exclusiveGateway` (XOR) | all branches and the join | every branch is mutually exclusive; only one path fires per turn. A parallel gateway at the join would *wait* for all incoming, which would deadlock |
| `startEvent` / `endEvent` | turn boundaries | one of each; per-turn scope means no intermediate events for "wait for user" |

I considered modeling the consent question (`Ask user`) as a separate `userTask` with a message intermediate event. Rejected because in per-turn scope the question is just text appended to the response — not a separate work item. Adding a `userTask` would inflate the trace with an activity that has no corresponding span.

## 4. What the BPMN does *not* model (and why that's OK)

Some prompt content is intentionally not in the BPMN:

- **Prefilter argument extraction** (`categories`, `after_date`, `before_date`). The choice of *whether to set them* is implementation detail of the model's reasoning. Process mining cares about activity sequence, not argument values. If we later want to compare prefilter-using traces vs un-filtered ones, those become event attributes, not separate activities.
- **The "rephrase and retry" inner loop on `find_similar_articles`.** Same reasoning — the trace will record N events of the same activity, but the control flow remains `find_similar_articles → gw_hits → …`.
- **Citation formatting rules** ("always include the arxiv_id and a 1-2 sentence relevance note"). Output-quality concerns belong to evaluation, not control flow.
- **The actual consent question wording.** Text content; not a structural property.

The BPMN is intentionally sparse: it captures only the things that a *trace* could conform to or violate. Style and content rules are out of scope for conformance checking against an event log.

## 5. Conformance properties this BPMN encodes

A conformance check against `intended_flow.bpmn` will catch:

- `analyze_full_article` invoked outside the deep-confirmed branch (prompt-drift on the consent rule).
- `web_search` invoked without a preceding failed `find_similar_articles` (prompt-drift on the fallback rule).
- `sample_articles` invoked alongside `find_similar_articles` in the same turn (the prompt says they're mutually exclusive).
- A turn that performs retrieval AND ends without a `Compose response` step — would suggest a tool call that didn't get a response composed around it.

It will *not* catch:

- Wrong arguments to a correctly-chosen tool.
- Bad citations or padded responses.
- Whether the consent question, when asked, was actually clear and well-formed.

## 6. Visualizing in bpmn.io

The file ships with BPMN DI (Diagram Interchange) coordinates so it renders without needing auto-layout. To view:

1. Open <https://demo.bpmn.io/new>.
2. Drag-and-drop [intended_flow.bpmn](intended_flow.bpmn) onto the canvas — or paste the XML via *File → Open*.
3. Use *Layout → Auto-layout* if the supplied coordinates feel cramped.

The same file works in Camunda Modeler, Signavio, and other BPMN 2.0 tools.

## 7. Open questions / future refinements

- **Conversation-level BPMN.** A second model that treats a session as one case would let conformance check the full consent dance (turn N asks → turn N+1 confirms or declines). Would require the XES extractor to use `session_id` or similar as the case id rather than the X-Ray trace id.
- **Parallel BPMN for `research_graph`.** The graph agent's intended flow is more constrained (its structure is *already* a graph at runtime). Worth modeling to compare conformance gaps between the two orchestration styles — that comparison is the actual research output of the project.
- **Dispute agent BPMN.** Will be much richer once the workflow lands — that's where smart deferred choice and smart discriminator patterns will show up explicitly.
