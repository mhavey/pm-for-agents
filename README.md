# tmls2026 — Process Mining over Agent Traces

A research project: build agents on AWS Bedrock AgentCore, then analyze their execution behavior using process mining algorithms.

The hypothesis is that ReAct-style agents — which choose tools and order actions on the fly — produce execution traces that can be mined like a business process. Discovery algorithms (Alpha, Heuristics, Inductive Miner) can surface the agent's *actual* behavior; conformance checking can compare it to a designed playbook; performance analysis can find slow tools and bottleneck transitions.

Two halves:

1. **The agents and their knowledge base** — production-ready. A Bedrock Knowledge Base of arXiv articles in OpenSearch Serverless, plus *two* Strands agents on AgentCore Runtime that share the same KB/Memory/tools but differ in orchestration:
   - **research_react** — Strands ReAct loop. The model picks tools and order on the fly.
   - **research_graph** — Strands graph (`classify → {search | browse | web | deep} → synth`). Flow is explicit; the model only fills in parameters at each node.

   Both emit traces via OpenTelemetry. Comparing their trace shapes is the input to phase 2.
2. **The process mining pipeline** — `process_mining/` package, started. First piece: [xray_to_xes.py](process_mining/xray_to_xes.py) fetches X-Ray traces and converts tool / graph-node spans into an XES log compatible with pm4py and other process-mining toolchains. Discovery / conformance / performance algorithms come next.

## Repo Layout

```
prep_arxiv_for_kb.py        # one-shot data prep
agents/
  research_react/           # Strands ReAct agent
    pyproject.toml
    uv.lock
    Dockerfile
    src/
      research_agent.py     # one Agent with all tools
      system_prompt.md
  research_graph/           # Strands graph agent (parallel structure)
    pyproject.toml
    uv.lock
    Dockerfile
    src/
      research_agent.py     # GraphBuilder: 6 nodes + conditional edges
      prompts/              # one MD per node
        classify.md  search.md  browse.md
        web.md       deep.md    synth.md
  dispute/                  # credit-card-charge dispute agent (in flux)
  trusteeship/              # trusteeship-application agent
```

The four tool definitions (`find_similar_articles`, `sample_articles`, `web_search`, `analyze_full_article`) are duplicated across both agents on purpose — each agent stays self-contained and independently deployable. If maintaining two copies becomes painful, factor out a shared module later.

## Phase 1: Data Prep

The corpus is the [Kaggle Cornell-University arXiv dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv/data) — a single JSON-Lines file (~5 GB, ~2.5M records) where each line is an article with title, abstract, authors, categories, and version history.

[prep_arxiv_for_kb.py](prep_arxiv_for_kb.py) streams that file and emits a directory tree of files Bedrock Knowledge Bases can ingest from S3. For each article it produces a pair:

- `<id>.txt` — title + authors + abstract, joined by blank lines. **This is what the embedding model sees.**
- `<id>.txt.metadata.json` — sidecar in Bedrock's `metadataAttributes` format (arxiv_id, primary_category, categories, doi, update_date, version dates, etc.).

Output is sharded into 10k-file subdirectories.

```bash
# Smoke test on 200 records
python3 prep_arxiv_for_kb.py arxiv-metadata-oai-snapshot.json out/ --limit 200

# CS / ML focus
python3 prep_arxiv_for_kb.py arxiv-metadata-oai-snapshot.json out/ \
    --categories cs.AI cs.LG cs.CL cs.CV cs.IR cs.NE stat.ML

# Full run
python3 prep_arxiv_for_kb.py arxiv-metadata-oai-snapshot.json out/

# Then upload under one S3 prefix and point a Bedrock KB data source at it.
aws s3 sync out/ s3://<bucket>/arxiv-kb/
```

### Why authors live in the embedded text

Users misspell author names ("Iaaac Johanson" vs "I. Johnson"). Bedrock KB metadata filters are exact-match only (`equals`, `in`, `stringContains`, `range`) — no Levenshtein, no phonetic. Strict author prefiltering on metadata is brittle.

The fix is to put authors *into* the embedded chunk text. Hybrid search (vector + BM25) tolerates moderate misspellings via subword overlap. It's not magic — single-letter typos in proper nouns still hurt — but it's far better than exact-match metadata filters.

For robust author search, the right next step is a canonicalization tool: fuzzy-match the user's input against an author index, return canonical names, and *those* go into a strict metadata filter.

## Phase 2: The Agents

Two Strands agents share the same KB, Memory, tools, and observability stack but use different orchestration patterns. Their behavior on identical user inputs is the comparison the project is built around.

### research_react — ReAct loop

A single [Strands `Agent`](agents/research_react/src/research_agent.py) with all four tools and one [system prompt](agents/research_react/src/system_prompt.md). At each step the model decides whether to retrieve, fall back to the web, ask the user about a deep analysis, or respond. The trace shape is whatever the loop produces — variable in depth and order.

The agent's *intended* per-turn flow — what the prompt is supposed to produce — is captured as a BPMN reference model at [agents/research_react/intended_flow.bpmn](agents/research_react/intended_flow.bpmn) and is the conformance target for the process-mining pipeline. One structural invariant worth noting: `analyze_full_article` is reachable only via the "deep-analysis confirmed" branch, so any trace where it follows anything else is by definition non-conforming.

### research_graph — explicit graph

A [Strands `GraphBuilder`](agents/research_graph/src/research_agent.py) wires six nodes:

```
classify ─┬─→ search   ─┐
          ├─→ browse   ─┤
          ├─→ web      ─┼─→ synth
          ├─→ deep     ─┤
          └─→ synth   (decline / invalid intent)
```

- **classify** — LLM agent, no tools. Reads the user's message and conversation history, emits a JSON object with `intent` ∈ {`search`, `browse`, `web`, `deep`, `decline`} plus extracted parameters (query, categories, dates, count, arxiv_id).
- **search / browse / web / deep** — tool-bound agents. Each calls exactly one tool with the parameters the classifier emitted. No memory.
- **synth** — LLM agent, no tools. Reads the prior node's output and conversation history, composes the user-facing reply. Asks for deep-analysis consent when warranted.

Edges from `classify` are conditional on the JSON intent; malformed output falls through to `decline → synth`. Routing to `deep` already encodes a consent check (the classifier only sets `intent=deep` after seeing the user's affirmative reply to a prior offer); the tool-side `user_confirmed` guard remains as the safety net.

The trace shape is deterministic: `classify → exactly one retrieval node → synth`. That contrast — variable ReAct vs. fixed graph — is what phase 2 process mining will quantify.

### Tools (shared)

| Tool | When the agent uses it | Implementation |
|---|---|---|
| `find_similar_articles` | Topic-similarity questions (the default). | Wraps `strands_tools.retrieve`. Hybrid (vector + keyword) retrieval over the BKB. Surfaces optional prefilters: `categories`, `after_date`, `before_date`. |
| `sample_articles` | Browse / discovery — "give me 3 random papers", "show me articles in cs.AI". | Direct OpenSearch DSL via `opensearch-py` + SigV4. Hard-coded `function_score` + `random_score`. The agent fills in parameters only — it never generates DSL. |
| `web_search` | Fallback when KB returns nothing relevant or the question is non-arXiv. | DuckDuckGo via `duckduckgo-search`. |
| `analyze_full_article` | Deep read of a specific paper. EXPENSIVE — downloads the PDF, runs an extra LLM pass for keywords + structured summary. | Consent-gated. The system prompt instructs the model to ask the user before invoking; the tool itself refuses unless `user_confirmed=True`. |

### Agent state — AgentCore Memory

Conversation history, semantic memory, and learned preferences live server-side in AgentCore Memory. Two retrieval strategies are configured: `semantic` and `preferences`, both `top_k=3`, `relevance_score=0.2`.

- **research_react** attaches an `AgentCoreMemorySessionManager` to its single agent.
- **research_graph** attaches it only to `classify` and `synth`. The four retrieval nodes are stateless one-shot tool callers.

There is no in-process session cache. Memory is the source of truth.

### Observability

Tracing is dependency-driven. `pyproject.toml` pulls `strands-agents[otel]` and `aws-opentelemetry-distro`; the Dockerfile launches the agent under `opentelemetry-instrument`. No code-level instrumentation. Traces flow to AgentCore's OTLP collector and from there to the AWS observability stack.

## Setup

Install [uv](https://github.com/astral-sh/uv) if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, for whichever agent you want to work on (`research_react` or `research_graph`):

```bash
cd agents/<agent>
uv lock        # only when pyproject.toml changes
uv sync        # creates .venv with deps
```

### Required env vars

| Var | Purpose |
|---|---|
| `KNOWLEDGE_BASE_ID` | The arXiv Bedrock KB id |
| `MEMORY_ID` | AgentCore Memory resource id |
| `OPENSEARCH_HOST` | AOSS host for `sample_articles`, e.g. `abc123.us-east-1.aoss.amazonaws.com` |

Optional: `AWS_REGION` (default `us-east-1`), `OPENSEARCH_INDEX`, `AGENT_MODEL_ID`, `ANALYZE_MODEL_ID`, `MIN_SCORE`, `SYSTEM_PROMPT_PATH`.

## Run Locally

```bash
cd agents/<agent>           # research_react or research_graph
uv run python src/research_agent.py
# serves on http://localhost:8080
```

## Deploy to AgentCore

```bash
cd agents/<agent>
docker build -t <agent> .
agentcore configure --entrypoint src/research_agent.py
agentcore launch
```

The execution role needs:

- `bedrock:Retrieve` on the KB
- `bedrock:InvokeModel` on `ANALYZE_MODEL_ID`
- `aoss:APIAccessAll` on the OpenSearch Serverless collection (plus a matching data-access policy entry)
- AgentCore Memory access scoped to your `MEMORY_ID`
- Egress to `arxiv.org` (PDF fetch) and `duckduckgo.com` (web search)

## Known Limitations

- **Author search is approximate.** Hybrid retrieval over chunk text mitigates misspelled names but isn't bulletproof. A canonicalization step is the right fix.
- **`sample_articles` index assumption.** The tool assumes `primary_category` and `update_date` are top-level filterable fields in the OpenSearch index. If the KB uses the default schema (metadata as a single `AMAZON_BEDROCK_METADATA` JSON blob), filters silently match nothing — adapt the tool to query the metadata blob instead.
- **`pypdf` extraction is rough on math-heavy papers.** `analyze_full_article` does best on prose-heavy abstracts. For figure- or formula-heavy work, swap in `pdfplumber`, MarkItDown, or AWS Textract.
- **No process mining pipeline yet.** Phase 2 of the project.

## Phase 3 (early): Process Mining Extractor

[process_mining/xray_to_xes.py](process_mining/xray_to_xes.py) pulls AWS X-Ray traces emitted by the agents and converts tool / graph-node spans into an [XES](https://www.xes-standard.org/) log. The activity classifier defaults to the four tool names + six graph-node names this repo defines plus prefixes (`tool.`, `node.`, `graph.`); boto3 / Bedrock auto-instrumented spans are intentionally excluded so the resulting log models the *agent's* behavior, not the underlying AWS plumbing.

```bash
python -m process_mining.xray_to_xes \
    --start 2026-05-10T00:00:00Z \
    --end   2026-05-10T23:59:59Z \
    --filter 'service("research-graph")' \
    --out traces.xes
```

The output is pm4py-loadable:

```python
import pm4py
log = pm4py.read_xes("traces.xes")
process_tree = pm4py.discover_process_tree_inductive(log)
pm4py.view_process_tree(process_tree)
```

## Roadmap

- Author canonicalization tool (fuzzy in, exact-match metadata filter out)
- Process discovery + conformance pipeline on top of the XES extractor
- Conformance comparison: how often does each agent's actual trace match the designed flow? Where does ReAct diverge? Where does the graph still get stuck?
