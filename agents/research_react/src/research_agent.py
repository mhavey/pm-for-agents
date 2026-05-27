"""
research_agent.py
Strands ReAct agent for arXiv research, hosted on AWS Bedrock AgentCore.

Tools (in the order the agent should reach for them):
  1. find_similar_articles — Bedrock KB hybrid retrieval (vector + keyword) over the
                             arXiv corpus, with optional category / date prefilters.
                             Wraps strands_tools.retrieve.
  2. sample_articles       — Direct OpenSearch DSL for non-vector queries (random
                             sampling, optionally filtered by category/date).
                             Use for browse/discovery, NOT topic similarity.
  3. web_search            — DuckDuckGo fallback when KB has no relevant hit.
  4. analyze_full_article  — Download arXiv PDF, extract keywords, produce a deeper
                             summary than the abstract. EXPENSIVE — must only run
                             after the user has explicitly said "yes".

Environment:
    KNOWLEDGE_BASE_ID    required — Bedrock KB id (read by strands_tools.retrieve)
    MEMORY_ID            required — AgentCore Memory resource id
    OPENSEARCH_HOST      required for sample_articles — AOSS host, e.g.
                         "abc123.us-east-1.aoss.amazonaws.com"
    OPENSEARCH_INDEX     index name backing the KB
                         (default: bedrock-knowledge-base-default-index)
    AWS_REGION           region for Bedrock + KB clients (default: us-east-1)
    AGENT_MODEL_ID       Strands agent model       (default: claude-sonnet-4-6)
    ANALYZE_MODEL_ID     PDF summarization model   (default: claude-haiku-4-5)
    SYSTEM_PROMPT_PATH   override path to the system prompt markdown file

Run locally:
    pip install -r requirements.txt
    export KNOWLEDGE_BASE_ID=...
    export AWS_REGION=us-east-1
    python research_agent.py            # serves on http://localhost:8080

Deploy to AgentCore Runtime:
    agentcore configure --entrypoint research_agent.py
    agentcore launch

The execution role needs: bedrock:Retrieve on the KB, bedrock:InvokeModel on the
analysis model, and egress to arxiv.org and duckduckgo.com.
"""

from __future__ import annotations

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
from strands_tools import retrieve
from opentelemetry import trace

log = logging.getLogger("research_agent")
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

SYSTEM_PROMPT_PATH = Path(os.environ.get(
    "SYSTEM_PROMPT_PATH",
    str(Path(__file__).parent / "system_prompt.md"),
))

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
    """Lazy AWS-SigV4-signed client for the OpenSearch Serverless collection."""
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
    restricted to a primary_category and/or update_date range.

    Use this for browse/discovery questions where the user does NOT have a
    topic in mind — e.g. "give me 3 random papers", "show me 5 articles in
    cs.AI", "any 3 papers from 2024". For topic-similarity questions, use
    find_similar_articles instead.

    The DSL is hard-coded inside this tool — the agent only fills in the
    parameters, never raw OpenSearch DSL. Assumes the OpenSearch index has
    primary_category and update_date stored as top-level filterable fields
    (which matches the metadata sidecars we ingest).

    Args:
        count: how many articles to return (1-20, default 3).
        primary_category: optional arXiv primary category, e.g. "cs.AI".
        after_date: optional YYYY-MM-DD lower bound on update_date.
        before_date: optional YYYY-MM-DD upper bound on update_date.

    Returns:
        {"results": [{arxiv_id, title, authors, primary_category, categories,
                      doi, update_date}, ...]}
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
    """
    Search the public web via DuckDuckGo for content related to ``keywords``.
    Use ONLY when `find_similar_articles` returned nothing relevant, or when the
    question is clearly about non-arXiv material (news, blog posts, software docs).

    Args:
        keywords: Search query.
        region: DDG region code, e.g. "us-en" (default) or "wt-wt" (worldwide).
        max_results: How many results to return (1-20, default 5).

    Returns:
        {"results": [{title, url, snippet}, ...]}
    """
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
    and keyword list — richer than the abstract alone.

    EXPENSIVE: this fetches a multi-MB PDF and runs an extra LLM pass on it.
    Only use it when the user has explicitly approved a deeper analysis on
    THIS specific paper. Do NOT set ``user_confirmed=True`` unless the user's
    most recent message is an affirmative confirmation. If the user has not
    yet been asked, do not call this tool — instead, in your reply describe
    the cost and ask them to confirm with yes/no.

    Args:
        arxiv_id: arXiv identifier, e.g. "2401.12345" or "hep-ph/9901001".
            Optional version suffix ("v2") is allowed.
        user_confirmed: Must be True. The agent should only set this after
            the user has explicitly approved the deeper analysis.

    Returns:
        On success: {status: "ok", arxiv_id, source_url, pages, summary, keywords}.
        Without consent: {status: "consent_required", message}.
        On error: {status: "error", message}.
    """
    if not user_confirmed:
        return {
            "status": "consent_required",
            "message": (
                "Cannot run analyze_full_article without explicit user consent. "
                "Tell the user this is a deeper, more expensive analysis (PDF "
                "download + an extra LLM pass) and ask them to confirm with "
                "'yes' before calling this tool again."
            ),
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


# ---- agent factory ----------------------------------------------------------

def _load_system_prompt() -> str:
    try:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise RuntimeError(
            f"system prompt not found at {SYSTEM_PROMPT_PATH} — "
            "set SYSTEM_PROMPT_PATH or ensure the file is bundled with the agent."
        ) from e


def _build_memory_session_manager(session_id: str, user_id: str) -> AgentCoreMemorySessionManager:
    if not MEMORY_ID:
        raise RuntimeError(
            "MEMORY_ID env var is not set; this agent requires AgentCore Memory."
        )
    
    print("Building session manager with {session_id}")
    memory_config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        session_id=session_id,
        actor_id=user_id,
        retrieval_config={
            "/strategies/{actorId}/semantic": RetrievalConfig(
                top_k=3, relevance_score=0.2,
            ),
            "/strategies/{actorId}/preferences": RetrievalConfig(
                top_k=3, relevance_score=0.2,
            ),
        },
    )
    return AgentCoreMemorySessionManager(memory_config, REGION)


def build_agent(session_id: str, user_id: str) -> Agent:
    return Agent(
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=[find_similar_articles, sample_articles, web_search, analyze_full_article],
        system_prompt=_load_system_prompt(),
        session_manager=_build_memory_session_manager(session_id, user_id),
    )


# ---- AgentCore runtime entrypoint -------------------------------------------

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: Any = None) -> dict:
    """
    AgentCore HTTP entrypoint. Conversation history and long-term memory are
    persisted by AgentCore Memory via AgentCoreMemorySessionManager, so we
    build a fresh Agent per call bound to (session_id, user_id).
    """
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

    agent = build_agent(session_id=session_id, user_id=user_id)
    set_span_attribs({"user_id": user_id, "session_id": session_id})
    result = agent(user_message)
    return {"response": str(result), "session_id": session_id, "user_id": user_id}


if __name__ == "__main__":
    app.run()
