"""
research_agent.py
Strands GRAPH agent for arXiv research, hosted on AWS Bedrock AgentCore.

Same KB / Memory / tools as agents/research_react, but the orchestration is an
explicit graph rather than a ReAct loop:

    classify ─┬─→ search   ─┐
              ├─→ browse   ─┤
              ├─→ web      ─┼─→ synth
              ├─→ deep     ─┤
              └─→ synth (decline / invalid intent)

Edges from `classify` are conditional on the JSON intent it emits.
`classify` and `synth` are LLM agents with AgentCore Memory attached so they
see conversation history; the four retrieval nodes are tool-bound agents that
call exactly one tool each and have no memory.

The four tools (find_similar_articles, sample_articles, web_search,
analyze_full_article) are intentionally duplicated from
agents/research_react/src/research_agent.py to keep each agent self-contained
and independently deployable.

Environment:
    KNOWLEDGE_BASE_ID    required — Bedrock KB id (read by strands_tools.retrieve)
    MEMORY_ID            required — AgentCore Memory resource id
    OPENSEARCH_HOST      required for sample_articles — AOSS host name
    OPENSEARCH_INDEX     index name (default: bedrock-knowledge-base-default-index)
    AWS_REGION           default us-east-1
    AGENT_MODEL_ID       (default claude-sonnet-4-6)
    ANALYZE_MODEL_ID     (default claude-haiku-4-5)
    MIN_SCORE            optional retrieve cutoff (read by strands_tools.retrieve)
"""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from duckduckgo_search import DDGS

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.graph import GraphBuilder
from strands_tools import retrieve

from datetime import datetime, timezone
from opentelemetry import trace, context
from opentelemetry.trace import SpanKind
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import get_current_span

log = logging.getLogger("research_graph_agent")
logging.basicConfig(level=logging.INFO)

tracer = trace.get_tracer(__name__)

def set_span_attribs(a):
    print(f"CALLING_SET_SPAN_ATTRIBS {a}")
    span = trace.get_current_span()
    if span and span.is_recording():
        print("CALLING_SET_SPAN_ATTRIBS_IS_RECORDING")
        for k, v in a.items():
            # 1. Standard OTel attribute (Stored in X-Ray Metadata)
            span.set_attribute(k, v)
            
            # 2. X-Ray Annotation (Searchable in AWS Console)
            # This prefix tells ADOT to index this specific key
            span.set_attribute(f"aws.xray.annotations.{k}", v)
    else:
        print(f"CALLING_SET_SPAN_ATTRIBS_NOT_IS_RECORDING {a}")
        print(a) # appears in trace

# ---- config -----------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1:0")
ANALYZE_MODEL_ID = os.environ.get("ANALYZE_MODEL_ID", "us.anthropic.claude-haiku-4-5-v1:0")
MEMORY_ID = os.environ.get("MEMORY_ID")
OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "bedrock-knowledge-base-default-index")
KNOWLEDGE_BASE_ID   = os.environ.get("KNOWLEDGE_BASE_ID")

_ac_memory = boto3.client("bedrock-agentcore", region_name=REGION)

_brt = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
)
_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


_ARXIV_ID_RX = re.compile(
    r"^(?:[a-z][a-z\-]*(?:\.[A-Z]{2,})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$"
)
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _build_kb_filter(
    categories: list[str] | None,
    after_date: str | None,
    before_date: str | None,
) -> dict | None:
    clauses: list[dict] = []
    if categories:
        clauses.append({"in": {"key": "primary_category", "value": list(categories)}})
    if after_date and _DATE_RX.match(after_date):
        clauses.append({"greaterThanOrEquals": {"key": "update_date", "value": after_date}})
    if before_date and _DATE_RX.match(before_date):
        clauses.append({"lessThanOrEquals": {"key": "update_date", "value": before_date}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"andAll": clauses}


# ---- tools ------------------------------------------------------------------

@tool
def find_similar_articles(
    query: str,
    categories: list[str] | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
    max_results: int = 5,
    **kwargs
) -> dict:
    if not KNOWLEDGE_BASE_ID:
        return {"results": [], "error": "KNOWLEDGE_BASE_ID missing"}

    # 1. Build the filter using your existing _build_kb_filter helper
    kb_filter = _build_kb_filter(categories, after_date, before_date)

    # 2. Prepare the retrieval configuration
    retrieval_config = {
        'vectorSearchConfiguration': {
            'numberOfResults': max(1, min(max_results, 20)),
            'overrideSearchType': 'HYBRID'
        }
    }

    # Add the filter if it exists
    if kb_filter:
        retrieval_config['vectorSearchConfiguration']['filter'] = kb_filter

    try:
        print(f"RETRIEVE_CALL {query} {retrieval_config}")

        # 3. The Direct Boto3 Call
        response = _agent_runtime.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={'text': query},
            retrievalConfiguration=retrieval_config
        )

        # 4. Parse the standard Boto3 response
        # Boto3 always returns a dict with a 'results' key
        raw_hits = response.get('retrievalResults', [])
        print(f"RETRIEVE_CALL_RESULT {query} {retrieval_config} {raw_hits} {response}")

        # 5. Apply your MIN_SCORE logic
        hits = [
            {
                "content": hit.get("content", {}).get("text"),
                "score": hit.get("score", 0),
                "metadata": hit.get("metadata", {}),
                "uri": hit.get("location", {}).get("s3Location", {}).get("uri")
            }
            for hit in raw_hits 
        ]

        return {
            "results": hits,
            "count": len(hits),
            "kb_id": KNOWLEDGE_BASE_ID
        }

    except Exception as e:
        log.error(f"Boto3 Retrieval failed: {e}")
        return {"results": [], "error": str(e)}


# ---- OpenSearch direct (for non-vector queries) ----------------------------

_os_client: OpenSearch | None = None


def _get_opensearch_client() -> OpenSearch:
    global _os_client
    if _os_client is None:
        if not OPENSEARCH_HOST:
            raise RuntimeError(
                "OPENSEARCH_HOST env var is not set; sample_articles is unavailable."
            )
        creds = boto3.Session().get_credentials()
        _os_client = OpenSearch(
            hosts=[{"host": OPENSEARCH_HOST, "port": 443}],
            http_auth=AWSV4SignerAuth(creds, REGION, "aoss"),
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=30,
        )
    return _os_client


def _hit_to_article(hit: dict) -> dict:
    src = hit.get("_source") or {}
    text = src.get("AMAZON_BEDROCK_TEXT_CHUNK") or ""
    title = text.split("\n", 1)[0].strip() if text else None
    return {
        "arxiv_id": src.get("arxiv_id"),
        "title": title,
        "authors": src.get("authors"),
        "primary_category": src.get("primary_category"),
        "categories": src.get("categories"),
        "doi": src.get("doi"),
        "update_date": src.get("update_date"),
    }


@tool
def sample_articles(
    count: int = 3,
    primary_category: str | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
) -> dict:
    """
    Return a uniformly-random sample of N articles from the KB, optionally
    restricted by primary_category and/or update_date range. Hard-coded
    `function_score` + `random_score` DSL — the agent fills in parameters only.
    """
    filters: list[dict] = []
    if primary_category:
        filters.append({"term": {"primary_category": primary_category}})
    if after_date and _DATE_RX.match(after_date):
        filters.append({"range": {"update_date": {"gte": after_date}}})
    if before_date and _DATE_RX.match(before_date):
        filters.append({"range": {"update_date": {"lte": before_date}}})

    base_query = {"match_all": {}} if not filters else {"bool": {"filter": filters}}

    body = {
        "size": max(1, min(count, 20)),
        "query": {
            "function_score": {
                "query": base_query,
                "random_score": {},
                "boost_mode": "replace",
            }
        },
        "_source": [
            "arxiv_id", "primary_category", "categories", "authors",
            "doi", "update_date", "AMAZON_BEDROCK_TEXT_CHUNK",
        ],
    }

    try:
        resp = _get_opensearch_client().search(index=OPENSEARCH_INDEX, body=body)
    except Exception as e:
        return {"results": [], "error": f"opensearch query failed: {e}"}

    return {"results": [_hit_to_article(h) for h in resp.get("hits", {}).get("hits", [])]}


@tool
def web_search(keywords: str, region: str = "us-en", max_results: int = 5) -> dict:
    """Search the public web via DuckDuckGo for content related to ``keywords``."""
    try:
        hits = DDGS().text(
            keywords,
            region=region,
            max_results=max(1, min(max_results, 20)),
        )
    except Exception as e:
        return {"results": [], "error": f"duckduckgo search failed: {e}"}
    return {
        "results": [
            {"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")}
            for h in (hits or [])
        ]
    }


@tool
def analyze_full_article(arxiv_id: str, user_confirmed: bool = False) -> dict:
    """
    Download the full PDF for an arXiv paper, then return an extended summary
    and keyword list. EXPENSIVE; gated by user_confirmed=True. The graph
    routing only enters this node after the classifier has determined the
    user confirmed deep analysis, but the tool guard remains as a safety net.
    """
    if not user_confirmed:
        return {
            "status": "consent_required",
            "message": "analyze_full_article requires user_confirmed=True.",
        }

    aid = arxiv_id.strip()
    if not _ARXIV_ID_RX.match(aid):
        return {"status": "error", "message": f"arxiv_id {arxiv_id!r} is not a valid arXiv ID."}

    url = f"https://arxiv.org/pdf/{aid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tmls2026-research-agent/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            pdf_bytes = r.read()
    except Exception as e:
        return {"status": "error", "message": f"failed to download PDF from {url}: {e}"}

    try:
        from pypdf import PdfReader
    except ImportError:
        return {"status": "error", "message": "pypdf is not installed in the agent runtime."}

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        return {"status": "error", "message": f"failed to parse PDF: {e}"}

    if not text:
        return {"status": "error", "message": "PDF contained no extractable text."}

    truncated = text[:120_000]
    prompt = (
        "You are summarizing an arXiv paper for a researcher who has already read the abstract. "
        "Write a structured deeper summary that surfaces what the abstract does NOT — "
        "the methodology, key results with concrete numbers, datasets/benchmarks, limitations, "
        "and how it relates to prior work. Be specific and skimmable.\n\n"
        "Then propose 8-15 short keyword phrases (lowercase, hyphenated multi-words) that "
        "best characterize the paper for indexing.\n\n"
        'Reply with JSON only, exactly: {"summary": "<markdown>", "keywords": ["...", "..."]}\n\n'
        f"PAPER TEXT (may be truncated):\n{truncated}"
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = _brt.invoke_model(modelId=ANALYZE_MODEL_ID, body=json.dumps(body))
        out_text = json.loads(resp["body"].read())["content"][0]["text"]
    except Exception as e:
        return {"status": "error", "message": f"summarization model call failed: {e}"}

    summary, keywords = out_text, []
    m = re.search(r"\{.*\}", out_text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            summary = parsed.get("summary") or out_text
            keywords = parsed.get("keywords") or []
        except json.JSONDecodeError:
            pass

    return {
        "status": "ok",
        "arxiv_id": aid,
        "source_url": url,
        "pages": pages,
        "summary": summary,
        "keywords": keywords,
    }


# ---- prompts + memory + agent factories ------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"


@functools.lru_cache(maxsize=None)
def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise RuntimeError(f"prompt file not found: {path}") from e

class TraceableNodeAgent(Agent):
    def __init__(self, node_name, session_id, user_id, **kwargs):
        # 1. Pass the prompt name as the official framework name 
        # so native OTel logs match your BPMN diagram tokens exactly
        kwargs["name"] = node_name
        super().__init__(**kwargs)
        self.node_name = node_name
        self.session_id = session_id
        self.user_id = user_id

    def __call__(self, *args, **kwargs):
        """
        Intercepts graph node invocation boundaries elegantly.
        Bridges the contextvars vacuum when the graph hops execution threads.
        """
        tracer = trace.get_tracer(__name__)
        current_ctx = context.get_current()

        with tracer.start_as_current_span(
            f"node.{self.node_name}",
            context=current_ctx,
            kind=SpanKind.INTERNAL
        ) as span:
            # 1. Attach Searchable Business Metadata safely
            if span.is_recording():
                span.set_attribute("node.name", self.node_name)
                span.set_attribute("session.id", self.session_id)
                span.set_attribute("user.id", self.user_id)
                span.set_attribute(f"aws.xray.annotations.node_name", self.node_name)

            # 2. Secure thread-local context down the stack (CRITICAL: Keep out of the if-block)
            token = context.attach(context.set_value(trace._SPAN_KEY, span, current_ctx))
            try:
                # Call the base framework executor
                result = super().__call__(*args, **kwargs)
                
                if span.is_recording():
                    # 3. NATIVE CONSOLE EVENT: Forces visibility into the 'Output' Tab UI
                    span.add_event("message", {"content": str(result)})
                    
                    # 4. DETERMINISTIC ROUTING EVENT: Lightweight marker for your process mining talk
                    if self.node_name == "classify":
                        output_text = str(result)
                        match = re.search(r"\{.*\}", output_text, re.DOTALL)
                        if match:
                            try:
                                data = json.loads(match.group(0))
                                intent = data.get("intent")
                                if intent:
                                    # Emits a tiny structured payload that survives X-Ray serialization drops
                                    span.add_event("routing_decision", {"next_node": intent})
                            except Exception:
                                pass # Resilience guard against malformed text logs
                
                return result
            finally:
                context.detach(token)

def _make_node_agent(
    prompt_name: str,
    tools: list,
    session_id: str,
    user_id: str,
    history_context: str = ""
) -> Agent:
    
    full_prompt = f"{_load_prompt(prompt_name)}\n\nCONVERSATION_HISTORY:\n{history_context}"
    
    # Pack the process mining requirements into the framework's native mapping
    # Note: AWS X-Ray indexing requires the prefix for custom pivots
    metadata_attributes = {
        "node_name": prompt_name,
        "session_id": session_id,
        "user_id": user_id,
        "aws.xray.annotations.node_name": prompt_name,
        "aws.xray.annotations.session_id": session_id,
        "aws.xray.annotations.user_id": user_id
    }

    # Pass everything straight to the Agent constructor
    return TraceableNodeAgent(
        # 1. Native framework name so it matches your BPMN exactly!
        name=prompt_name, 
        
        # 2. Native trace attributes so OTel handles context propagation automatically
        trace_attributes=metadata_attributes, 
        
        # 3. Standard configurations
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=tools,
        system_prompt=full_prompt,
        
        # 4. Keep your internal tracking assignments if your script relies on them
        node_name=prompt_name,
        session_id=session_id,
        user_id=user_id,
    )     



# ---- graph ------------------------------------------------------------------

def _classify_output_text(state: Any) -> str:
    """
    Best-effort accessor for the classifier node's output text from a
    Strands GraphState. The exact attribute path varies across Strands
    versions, so we try the common shapes in order.
    """
    for getter in (
        lambda s: s.results["classify"].result.message,        # newer API
        lambda s: s.results["classify"].result,                # mid-versions
        lambda s: s.results["classify"].output,                # alt naming
        lambda s: s.node_outputs["classify"],                  # dict-style
        lambda s: s["classify"],                                # mapping
    ):
        try:
            v = getter(state)
            if v is not None:
                return str(v)
        except (AttributeError, KeyError, TypeError):
            continue
    return ""


def _parse_classify_intent(state: Any) -> str:
    text = _classify_output_text(state)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return "decline"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "decline"
    intent = data.get("intent")
    return intent if intent in {"search", "browse", "web", "deep"} else "decline"


def _intent_is(target: str):
    def cond(state: Any) -> bool:
        return _parse_classify_intent(state) == target
    return cond


def build_graph(session_id: str, user_id: str, history_str: str = ""):
    classify_agent = _make_node_agent("classify", [], session_id, user_id, history_context=history_str)
    search_agent   = _make_node_agent("search",   [find_similar_articles], session_id, user_id)
    browse_agent   = _make_node_agent("browse",   [sample_articles],       session_id, user_id)
    web_agent      = _make_node_agent("web",      [web_search],            session_id, user_id)
    deep_agent     = _make_node_agent("deep",     [analyze_full_article],  session_id, user_id)
    synth_agent    = _make_node_agent("synth",    [],                      session_id, user_id, history_context=history_str)

    builder = GraphBuilder()
    builder.add_node(classify_agent, "classify")
    builder.add_node(search_agent,   "search")
    builder.add_node(browse_agent,   "browse")
    builder.add_node(web_agent,      "web")
    builder.add_node(deep_agent,     "deep")
    builder.add_node(synth_agent,    "synth")

    builder.set_entry_point("classify")

    builder.add_edge("classify", "search", condition=_intent_is("search"))
    builder.add_edge("classify", "browse", condition=_intent_is("browse"))
    builder.add_edge("classify", "web",    condition=_intent_is("web"))
    builder.add_edge("classify", "deep",   condition=_intent_is("deep"))
    # decline / invalid intent → straight to synth
    builder.add_edge(
        "classify", "synth",
        condition=lambda s: _parse_classify_intent(s) == "decline",
    )

    builder.add_edge("search", "synth")
    builder.add_edge("browse", "synth")
    builder.add_edge("web",    "synth")
    builder.add_edge("deep",   "synth")

    return  builder.build()


# ---- AgentCore runtime entrypoint -------------------------------------------

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload: dict, context: Any = None) -> dict:
    payload = payload or {}
    user_message = payload.get("prompt") or payload.get("input") or ""
    if not user_message:
        return {"error": "missing 'prompt' in payload"}

    session_id = (
        getattr(context, "session_id", None)
        or payload.get("session_id")
        or "default-session"
    )
    user_id = (
        getattr(context, "user_id", None)
        or getattr(context, "actor_id", None)
        or payload.get("user_id")
        or "default-user"
    )

    #print(f"Agent called with {session_id} {user_id} {user_message}")

    response = _ac_memory.list_events(
        memoryId=MEMORY_ID,
        sessionId=session_id,
        actorId=user_id,
        includePayloads=True, # Note: plural in raw Boto3
        maxResults=10 # Only get the last 5 turns (5 user + 5 assistant)
    )
    # Boto3 always returns a dict, so we use .get("events")
    events = response.get("events", [])
    history_lines = []
    for event in reversed(events):
        for item in event.get("payload", []):
            if "conversational" in item:
                c = item["conversational"]
                history_lines.append(f"{c['role'].upper()}: {c['content']['text']}")

    history_str = "\n".join(history_lines)

    # Build and run your graph with this history injected
    graph = build_graph(session_id, user_id, history_str)

    set_span_attribs({"user_id": user_id, "session_id": session_id})
 
    #  Execute the graph
    result = graph(user_message)

    # logic to get actual response text
    # 1. Get the result of the 'synth' node specifically
    synth_node = result.results.get("synth")

    if synth_node and synth_node.result:
        # 2. Extract the text content from the assistant's message
        # In Strands/Bedrock, 'content' is a list of blocks
        content_blocks = synth_node.result.message.get("content", [])
        
        # 3. Join the text from all blocks (usually just one)
        response_text = "\n".join(
            block.get("text", "") for block in content_blocks if "text" in block
        )
    else:
        response_text = "The graph completed, but the synthesis node was skipped or empty."

    # 1. Ensure roles are UPPERCASE
    # 2. Truncate text to 100,000 characters to stay within API limits
    safe_response = response_text[:100000]
    _ac_memory.create_event(
        memoryId=MEMORY_ID,
        sessionId=session_id,
        actorId=user_id,
        eventTimestamp=datetime.now(timezone.utc),
        payload=[
            {
                "conversational": {
                    "role": "USER",
                    "content": {"text": user_message}
                }
            },
            {
                "conversational": {
                    "role": "ASSISTANT",
                    "content": {"text": safe_response}
                }
            }
        ]
    )
    return {"response": response_text, "session_id": session_id, "user_id": user_id}

if __name__ == "__main__":
    app.run()
