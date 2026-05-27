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
from botocore.exceptions import ClientError
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

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent, AfterInvocationEvent

from datetime import datetime, timezone
from opentelemetry import trace, context
from opentelemetry.trace import SpanKind
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import get_current_span

from decimal import Decimal

log = logging.getLogger("research_graph_agent")
logging.basicConfig(level=logging.INFO)

# ---- config -----------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1:0")
ANALYZE_MODEL_ID = os.environ.get("ANALYZE_MODEL_ID", "us.anthropic.claude-haiku-4-5-v1:0")
MEMORY_ID = os.environ.get("MEMORY_ID")
OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "bedrock-knowledge-base-default-index")
KNOWLEDGE_BASE_ID   = os.environ.get("KNOWLEDGE_BASE_ID")

_ARXIV_ID_RX = re.compile(
    r"^(?:[a-z][a-z\-]*(?:\.[A-Z]{2,})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$"
)
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_ac_memory = boto3.client("bedrock-agentcore", region_name=REGION)

_brt = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
)
_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)

TABLE_NAME="research_agent_graph_trace"
dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

def log_trace_event(
    session_id: str,
    user_id: str,
    step_name: str,
    step_type: str,
    duration: float,
):
    """Logs a trace event to the trusteeship_process_state DynamoDB table.

    If the session_id exists, appends the event to the history list.
    If it doesn't exist, creates the item, initializes created_at, and starts the history list.
    """
    print(f"MYTRACER_LogTraceEvent {session_id}")

    # Generate current UTC timestamp
    current_timestamp = datetime.utcnow().isoformat() + "Z"
    duration_decimal = Decimal(f"{duration:.4f}")

    # Define the new history event dictionary
    new_event = {
        "user_id": user_id,
        "step_name": step_name,
        "step_type": step_type,
        "duration": duration_decimal,
        "timestamp": current_timestamp,
    }

    print(f"MYTRACER_LogTraceEvent_ev {session_id} {new_event}")

    try:
        response = table.update_item(
            Key={"session_id": session_id},
            # 1. SET created_at only if it doesn't exist yet
            # 2. Initialize history as an empty list if it doesn't exist, then list_append the new event
            UpdateExpression=(
                "SET created_at = if_not_exists(created_at, :now), "
                "history = list_append(if_not_exists(history, :empty_list), :new_event)"
            ),
            ExpressionAttributeValues={
                ":now": current_timestamp,
                ":empty_list": [],
                ":new_event": [
                    new_event
                ],  # list_append requires the new element to be wrapped in a list
            },
            ReturnValues="UPDATED_NEW",
        )
        print(f"MYTRACER_DDBStatus Successfully updated/created session {session_id} {response}")
        return response

    except ClientError as e:
        print(f"MYTRACER_DDBerror Error updating DynamoDB: {e.response['Error']['Message']}")
        raise e
    except Exception as generic_err:
        # CRITICAL: Catch any other system, networking, or parameter bugs
        print(f"MYTRACER_DDBFatal System error attempting DynamoDB write: {str(generic_err)}")
        raise generic_err 
       
class DynamoDbNodeAuditor(HookProvider):
    """
    Native Strands Hook Provider.
    Intercepts individual node invocations cleanly to record metrics directly to DynamoDB.
    """
    def __init__(self, node_name: str, session_id: str, user_id: str):
        print(f"MYTRACER_Hookinit {node_name}")
        self.session_id = session_id
        self.user_id = user_id
        self.node_name = node_name
        self.timers = {}

    def register_hooks(self, registry: HookRegistry) -> None:
        print(f"MYTRACER_Hookresister {self.node_name}")
        # Subscribe natively to request start and request end events
        registry.add_callback(BeforeInvocationEvent, self.on_node_start)
        registry.add_callback(AfterInvocationEvent, self.on_node_end)

    def on_node_start(self, event: BeforeInvocationEvent) -> None:
        print(f"MYTRACER_Hook-nodestart {self.node_name}")
        self.timers[self.node_name] = datetime.now()

    def on_node_end(self, event: AfterInvocationEvent) -> None:
        print(f"MYTRACER_Hook-nodeend {self.node_name}")
        node_name = getattr(event.agent, "name", "unknown")
        start_time = self.timers.get(node_name)
        
        if start_time:
            duration = (datetime.now() - start_time).total_seconds()
            
            # Fire your database logging hook completely synchronously
            try:
                log_trace_event(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    step_name=self.node_name,
                    step_type="node",
                    duration=duration
                )
            except Exception as e:
                print(f"DynamoDB Hook Exception for node {node_name}: {e}")

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

def _make_node_agent(
    prompt_name: str,
    tools: list,
    session_id: str,
    user_id: str,
    history_context: str = ""
) -> Agent:
    
    full_prompt = f"{_load_prompt(prompt_name)}\n\nCONVERSATION_HISTORY:\n{history_context}"

    # CRITICAL STEERING FIX: If this is the classifier, append a hard enforcement guard 
    # at the absolute END of the prompt window so the LLM cannot output prose.
    if prompt_name == "classify":
        full_prompt += (
            "\n\nCRITICAL INSTRUCTION:\n"
            "You must NOT speak to the user or output conversational prose. "
            "You are a routing machine. You MUST look at the user prompt and conversation history, "
            "and output exactly a JSON object matching the format specified above. "
            "Do not include introductory text. Output valid JSON enclosed in curly braces only."
        )

    # Instantiate your custom DynamoDB Hook wrapper
    auditor_hook = DynamoDbNodeAuditor(node_name=prompt_name, session_id=session_id, user_id=user_id)

    # Pass the standard Agent directly back to GraphBuilder.
    # No subclassing or __call__ overrides are required anymore!
    return Agent(
        name=prompt_name,  # Matches your reference BPMN node strings
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=tools,
        system_prompt=full_prompt,
        hooks=[auditor_hook]  # Injected natively into the loop constructor list
    )

# ---- graph ------------------------------------------------------------------

def _classify_output_text(state: Any) -> str:
    """
    Safely extracts the raw text content string from the classifier node's 
    output state across different Strands API framework versions.
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
            if v is None:
                continue

            # Case 1: It's a standard Strands/Bedrock Message dictionary or object
            if hasattr(v, "get") or isinstance(v, dict):
                # Strands messages nest content inside a list of blocks: [{'text': '...'}]
                content_blocks = v.get("content", []) if isinstance(v, dict) else getattr(v, "content", [])
                if content_blocks and isinstance(content_blocks, list):
                    text_content = "\n".join(
                        block.get("text", "") for block in content_blocks if isinstance(block, dict) and "text" in block
                    )
                    if text_content.strip():
                        return text_content.strip()
                
                # Fallback check for standard dict mapping configurations
                if "text" in v:
                    return str(v["text"])

            # Case 2: It's already a plain string or string-like primitive
            if isinstance(v, str):
                return v.strip()

            # Case 3: Ultimate fallback if it's a legacy custom object instance
            if hasattr(v, "text"):
                return str(v.text).strip()

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
