# How `intended_flow.bpmn` was derived

This note captures the reasoning behind [intended_flow.bpmn](intended_flow.bpmn) — the BPMN reference model for the `research_graph` agent's intended per-turn behavior. Like the [research_react companion doc](../research_react/intended_flow.md), it's the document the process-mining conformance check will compare actual XES traces against.

The two sources of truth for this BPMN are different from the ReAct one. There the prompt was the spec; here the **GraphBuilder code** is the spec, and each node's prompt only constrains *how* that node performs its single job. Translation is more direct: every node and edge in the Strands graph maps to a BPMN element.

---

## 1. Why this BPMN has *both* nodes and tools as activities

The big design decision that distinguishes this BPMN from the ReAct one: each tool-bearing node contributes **two** activities, not one — the node itself, then its tool.

```
classify → search → find_similar_articles → synth        (search branch)
classify → browse → sample_articles → synth              (browse branch)
classify → web    → web_search       → synth             (web branch)
classify → deep   → analyze_full_article → synth         (deep branch)
classify → synth                                          (decline branch)
```

Three reasons:

1. **The trace will already have both.** When `node_search` runs, Strands' OTel instrumentation emits one span for the node's agent execution. The agent then calls `find_similar_articles`, which the `@tool` decorator wraps in its own span. That's two spans, nested. If the BPMN only modeled one of them, the other would be unaccounted-for events at conformance time.
2. **The two activities mean different things.** The node's span captures *the LLM-driven decision to use the tool* — including parsing the classifier's JSON, choosing how to call the tool, and shaping the output. The tool's span captures the actual external work (KB hit, OpenSearch query, web fetch). For comparison against the ReAct agent — which collapses both into a single "agent loop" span tree — having them separate makes the difference observable.
3. **Stricter conformance.** Two activities on each branch double the number of edges a trace can violate, which is what we want — the graph's whole *point* is to constrain behavior more than ReAct does.

`classify` and `synth` have no tools; each contributes a single activity. They're modeled as plain `task` elements, not `serviceTask` — they make LLM calls but don't invoke external tools the way the four retrieval nodes do. Visually this also distinguishes them in BPMN modelers (plain box vs. gear icon).

## 2. How `xray_to_xes` actually captures this

The classifier in [process_mining/xray_to_xes.py](../../process_mining/xray_to_xes.py) is set up for exactly this situation. Its defaults already include:

- `DEFAULT_NODE_NAMES`: `classify`, `search`, `browse`, `web`, `deep`, `synth`
- `DEFAULT_TOOL_NAMES`: `find_similar_articles`, `sample_articles`, `web_search`, `analyze_full_article`, `retrieve`
- `DEFAULT_PREFIXES`: `tool.`, `node.`, `graph.`

So whether Strands emits a span as bare `search` or prefixed `node.search` / `graph.search`, the classifier picks it up. Same for the tools.

**Span ordering** is the next thing to worry about. The trace will have nested spans: `search` (outer) wraps `find_similar_articles` (inner). `xray_to_xes` walks segments depth-first and sorts events by **start_time**. That means:

```
search.start_time = 1715339400.800
find_similar_articles.start_time = 1715339400.850
```

→ events in the XES log come out as `search`, `find_similar_articles`. Outer-then-inner. That's exactly the order the BPMN expects.

**One caveat I should flag**: this assumes the sort is by start time, not end time. End-time sorting would give `find_similar_articles, search` (inner ends first because it's inside the outer's lifetime). The current `xray_to_xes` uses start time, which matches our BPMN. If you ever switch the extractor to end-time semantics, the BPMN edges would need to flip (tool before node in each branch).

**The honest bit**: I can't verify the *exact* span names Strands' graph instrumentation emits without running a real trace. The defaults match the most common conventions and the prefix matchers cover several alternatives, but if the first real run produces zero events on the node side, check what's actually in the trace and extend `--node-names` or `--prefixes` accordingly. The xray_to_xes module is built to be tuned this way.

## 3. Translating GraphBuilder → BPMN

The graph definition in [src/research_agent.py](src/research_agent.py) does this:

```python
builder.add_edge("classify", "search",  condition=_intent_is("search"))
builder.add_edge("classify", "browse",  condition=_intent_is("browse"))
builder.add_edge("classify", "web",     condition=_intent_is("web"))
builder.add_edge("classify", "deep",    condition=_intent_is("deep"))
builder.add_edge("classify", "synth",   condition=lambda s: ...decline...)

builder.add_edge("search", "synth")
builder.add_edge("browse", "synth")
builder.add_edge("web",    "synth")
builder.add_edge("deep",   "synth")
```

The translation is one-to-one with two small refinements:

- **Tools inserted between node and synth.** `add_edge("search", "synth")` becomes `node_search → tool_find_similar_articles → gw_join → node_synth`. The tool isn't an explicit graph edge in code — it's invoked *inside* the search node's agent — but it is its own span, so it's its own BPMN activity.
- **Explicit `gw_join` gateway.** In the graph, `synth` simply has 5 incoming edges. BPMN tools render multiple incoming edges to a task as an implicit XOR merge, but making it explicit (`gw_join`) keeps the diagram readable and makes the convergence semantically clear.

The `intent=decline / invalid` route is the same as `classify → synth` in code. In the BPMN it routes through `gw_join` to maintain the single-convergence shape.

## 4. Element-type choices

| BPMN element | Used for | Why |
|---|---|---|
| `task` | `classify`, `search`, `browse`, `web`, `deep`, `synth` (the six graph nodes) | LLM-driven steps. `task` is the neutral choice that doesn't claim "external service call" — that's the tool's job |
| `serviceTask` | `find_similar_articles`, `sample_articles`, `web_search`, `analyze_full_article` (the four tools) | These are the actual external invocations (KB hit, OpenSearch DSL, HTTP, PDF download). `serviceTask` is canonical for system-invoked work, and modelers render it with a gear |
| `exclusiveGateway` | `gw_intent` (5-way split) and `gw_join` (5-way merge) | Each turn fires exactly one branch. A parallel join would deadlock (4 of 5 branches don't fire) |
| `startEvent` / `endEvent` | turn boundaries | Same per-turn scope as the ReAct BPMN — one X-Ray trace = one case = one start→end |

I considered using BPMN `subProcess` to wrap each tool inside its node visually. Rejected: subprocesses add a containment level that complicates conformance algorithms (pm4py can flatten them, but the round-trip is messier). The flat representation maps directly to a flat XES log.

## 5. Conformance properties this BPMN encodes

Every property the ReAct BPMN encodes (deep-only-via-consent, no `web_search` without preceding KB miss, etc.) is *more strictly* enforced here because the graph routing is mechanical, not LLM-decided. Specifically:

- **Branch purity.** A trace must contain exactly one of `find_similar_articles`, `sample_articles`, `web_search`, `analyze_full_article` per turn (or none, in the decline case). Multiple tool calls in a single turn = non-conforming.
- **Node-tool pairing.** Each tool must be preceded by *its specific node*. `find_similar_articles` after `node_browse` is a violation; the graph code wouldn't allow it, but a conformance check enforces this externally.
- **Always-classify.** Every conforming trace begins with `classify` and ends with `synth`. No turn should bypass either — the graph entry point is `classify`, the only end is `synth`.
- **Same `analyze_full_article` invariant as ReAct.** Reachable only from the deep-confirmed branch — but here it's enforced *twice*, once by the graph routing (`node_deep` is the only predecessor) and once by the tool's `user_confirmed` runtime guard. Two structural and one runtime safety net.

A trace that violates any of the above flags either an instrumentation gap or a Strands version that doesn't behave as expected — both worth investigating.

## 6. How fixed is the graph, really?

The topology is fully deterministic — Python in `build_graph()`, not prompt content. But "deterministic graph" doesn't mean "deterministic execution." The LLM still has meaningful leeway, and most of it the BPMN *does* catch as conformance violations. Worth being precise about which is which.

### What the graph fixes by construction

- **The edges.** Source code. The model can't add or remove edges, invent new nodes, or skip the join. There is no path from `node_search` to `tool_sample_articles`, and no amount of prompt drift can create one.
- **Each node's tool list.** `node_search` is built with `tools=[find_similar_articles]` — full stop. The model in that node *cannot* call `sample_articles` or `web_search`; the Strands SDK doesn't expose them. The "branch purity" property in §5 is enforced at the SDK boundary, not just by prompt.
- **The edge conditions.** Edge conditions are Python predicates over `classify`'s parsed JSON. They run the same way every time given the same input.
- **The fall-through.** Malformed `classify` output → `decline → synth`. There is always a path forward; the graph won't stall on a bad LLM response.

### Where the LLM still has room

1. **Choice of intent.** `classify` *picks* the intent. The graph then routes by that pick. So the model can mis-route a turn by misclassifying — e.g., labelling a topic question as `browse` and ending up in `sample_articles`. **The BPMN doesn't detect this**: both `search` and `browse` are conforming routes; the question of which is *correct for the user's question* is a quality concern, not a conformance concern.
2. **Tool arguments.** Each node's prompt instructs the agent to call the tool with the parameters from the classifier's JSON. That instruction is prompt content, not a runtime check. The model could pass different params, drop a filter the classifier set, or invent params not in the classifier's output. The trace shows the tool was called; it doesn't show with-what-versus-what-the-classifier-said.
3. **Skipping the tool.** Nothing structurally prevents the LLM in `node_search` from returning a hallucinated answer without calling `find_similar_articles`. If that happens, the trace contains the node span but no tool span. **The BPMN flags this as nonconforming** — every tool-bearing branch in the BPMN requires the tool span to follow the node span.
4. **Calling the tool more than once.** Same shape: nothing prevents two `find_similar_articles` calls within one `node_search` execution. The trace shows duplicate tool spans; **the BPMN flags it** because the intended model has the tool exactly once.
5. **`synth` content.** The prompt has rules ("ask for consent if a paper warrants it", "don't restate the user's question"), but the prose is the model's. Invisible to conformance.

### How this lands in the ReAct comparison

The graph's structure aggressively narrows the action space. ReAct can pick any tool at any step, in any order, repeatedly, and it's all conformant against ReAct's BPMN as long as the high-level rules hold. The graph can only run the one tool that matches the classifier's intent, and only once per turn (intended).

So the *expected* delta between the mined model and this intended BPMN should be small for the graph and large for ReAct. If the graph's mined model still drifts substantially from this BPMN, the deviation almost certainly lands in category (3) skipped tools or (4) repeated tools above — both of which point at LLM behavior the node prompts didn't manage to constrain. That's a useful pre-registration of the kind of failure to look for.

## 7. What the BPMN does *not* model

- **Fan-out within `classify` to the LLM.** Classify makes a Bedrock InvokeModel call internally; that span is excluded by the activity classifier (boto3 spans aren't in the defaults). If you wanted to mine LLM-call patterns specifically, that's a different log.
- **Memory reads/writes.** `classify` and `synth` both have AgentCoreMemorySessionManager attached. Memory I/O is invisible to this BPMN.
- **Re-entry / multi-turn flows.** Same per-turn limitation as the ReAct BPMN — the consent dance crosses two cases (turn N's synth produces a question; turn N+1's classify routes to `deep`).

## 8. Visualizing in bpmn.io

1. Open <https://demo.bpmn.io/new>.
2. Drag-and-drop [intended_flow.bpmn](intended_flow.bpmn) onto the canvas, or paste the XML.
3. The included DI lays the four retrieval branches out vertically (search at top, deep at bottom) with `decline` going horizontally through the center. Use *Layout → Auto-layout* if the supplied coordinates feel cramped.

It's a wider diagram than the ReAct one because of the extra column for the tool activities. That's the visual reflection of "more structure, more activities, more conformance points to check."

## 9. Open questions for future work

- **Subprocess-based variant.** Could re-render this BPMN with each node as a `subProcess` containing its tool. Same semantics, different visual. Worth doing if conformance work ends up needing hierarchical Petri nets.
- **End-time sorting trade-off.** As noted in §2, the BPMN's node-then-tool order assumes start-time sorting. If process-mining work ever wants a "complete event" log, the BPMN should be regenerated with edges flipped within each branch.
- **Comparison view.** The most useful artifact phase-2 could produce is a single overlay of this BPMN against the ReAct BPMN (showing what the graph *adds* in terms of structural constraints) and the *mined* model from each agent's actual traces (showing what the graph *enforces* in terms of conformance). That comparison is the headline result the project is aiming at.
