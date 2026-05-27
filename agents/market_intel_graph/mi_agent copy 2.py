import os
import datetime
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from typing import Any
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from opentelemetry import trace


REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1:0")
MEMORY_ID = os.environ.get("MEMORY_ID")

_ac_memory = boto3.client("bedrock-agentcore", region_name=REGION)

shared_model = BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION)

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

# ---- 1. Domain-Specific Micro-Tools ----

@tool
def extract_sec_filings(ticker: str) -> str:
    """Simulates extraction of the latest 10-K or 10-Q filing notes for a ticker."""
    print(f"[SEC Agent] Pulling raw regulatory risk factors for {ticker}...")
    return f"SEC 10-K Excerpt ({ticker}): High R&D expenditure capitalization, expanding international logistics exposure, and outstanding hardware supply-chain vulnerabilities."

@tool
def fetch_live_stock_prices(ticker: str) -> dict:
    """Fetches market pricing, trailing P/E ratios, and trading velocity aggregates."""
    print(f"[Stock Agent] Quoting live exchange tickers for {ticker}...")
    return {
        "ticker": ticker.upper(),
        "price": 182.50,
        "trailing_pe": 28.4,
        "volume_velocity": "+12% above 90-day moving average"
    }

@tool
def scrape_recent_news(ticker: str) -> list[str]:
    """Scrapes financial newsletters and public press releases from the last 72 hours."""
    print(f"[News Agent] Indexing web articles mentioning {ticker}...")
    return [
        f"Breaking: {ticker} announces strategic partnership with leading clean energy supplier.",
        f"Analyst Alert: Core product margins projected to compress due to tariff volatility."
    ]

# ---- 2. Defining Independent Specialists ----

sec_agent = Agent(
    name="sec_extractor",
    model=shared_model,
    tools=[extract_sec_filings],
    system_prompt="You are a financial regulatory compliance officer. Use your tool to extract regulatory details. Extract raw data points exactly without editorializing."
)

stock_agent = Agent(
    name="stock_fetcher",
    model=shared_model,
    tools=[fetch_live_stock_prices],
    system_prompt="You are an automated quantitative trading assistant. Fetch numerical data fields. Present the structured quote matrix concisely."
)

news_agent = Agent(
    name="news_summarizer",
    model=shared_model,
    tools=[scrape_recent_news],
    system_prompt="You are a real-time market sentiment tracker. Summarize current public events and press feeds, highlighting clear macro headwinds and positive tailwinds."
)

# ---- 3. Defining Structural Coordinators ----

synthesis_agent = Agent(
    name="correlation_synthesizer",
    model=shared_model,
    system_prompt="""You are a Senior Portfolio Strategist. 
You will be provided with three separate streams of context: regulatory filings, real-time pricing data, and raw media sentiment.
Your task is to merge these perspectives into a unified market brief. 
Identify cross-correlations (e.g., how the news explains the trading volume, or how regulatory risk impacts the P/E ratio valuation)."""
)

audit_agent = Agent(
    name="risk_auditor",
    model=shared_model,
    system_prompt="""You are a Corporate Financial Risk Auditor. 
Review the compiled intelligence report. You must evaluate whether it mentions 'supply-chain vulnerabilities'.
- If it does, you must return an actionable 'COMPLIANT' report with safe mitigation summaries appended.
- If it fails to evaluate supply-chain issues adequately, you must return 'NON-COMPLIANT: Missing core operational vulnerability considerations.'"""
)

# ---- 4. Weaving the Parallel Graph Topology ----

def build_intelligence_graph(session_id, user_id, history_str):
    builder = GraphBuilder()

    # Add all agent personas into the graph workspace
    builder.add_node(sec_agent, "sec_extractor")
    builder.add_node(stock_agent, "stock_fetcher")
    builder.add_node(news_agent, "news_summarizer")
    builder.add_node(synthesis_agent, "correlation_synthesizer")
    builder.add_node(audit_agent, "risk_auditor")

    # Entry points: multiple parallel nodes can start simultaneously
    builder.set_entry_point("sec_extractor")
    builder.set_entry_point("stock_fetcher")
    builder.set_entry_point("news_summarizer")

    # Fan-In Semantics: correlation_synthesizer runs only after all three dependencies finish
    builder.add_edge("sec_extractor", "correlation_synthesizer")
    builder.add_edge("stock_fetcher", "correlation_synthesizer")
    builder.add_edge("news_summarizer", "correlation_synthesizer")

    # Sequence straight to the quality assurance gate
    builder.add_edge("correlation_synthesizer", "risk_auditor")

    # Cyclic Feedback Loop: If audit flags a compliance gap, route back to synthesis to rewrite
    builder.add_edge(
        "risk_auditor", 
        "correlation_synthesizer",
        condition=lambda state: "NON-COMPLIANT" in str(state.results["risk_auditor"].result)
    )

    # Configure global execution thresholds for safety against infinite loops
    builder.set_max_node_executions(15)
    builder.set_execution_timeout(300)

    return builder.build()

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

    set_span_attribs({"user_id": user_id, "session_id": session_id})

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
    graph = build_intelligence_graph(session_id, user_id, history_str)
 
    #  Execute the graph
    result = graph(user_message)

    # 3. Pull the finalized text block from our ultimate node
    audit_node = result.results.get("risk_auditor")
    if audit_node and audit_node.result:
        response_text = str(audit_node.result)
    else:
        response_text = "Analysis completed, but final compliance generation step was skipped."

    # 4. Commit this new interaction layer back to long-term memory
    try:
        _ac_memory.create_event(
            memoryId=MEMORY_ID,
            sessionId=session_id,
            actorId=user_id,
            eventTimestamp=datetime.datetime.now(datetime.timezone.utc),
            payload=[
                {"conversational": {"role": "USER", "content": {"text": user_message}}},
                {"conversational": {"role": "ASSISTANT", "content": {"text": response_text[:5000]}}}
            ]
        )
    except Exception as e:
        print(f"Failed saving state context window to AgentCore: {e}")

    return {"response": response_text, "session_id": session_id, "user_id": user_id}

if __name__ == "__main__":
    app.run()