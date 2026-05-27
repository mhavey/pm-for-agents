"""
================================================================================
MARKET INTELLIGENCE ARCHITECTURE AGENT (STRANDS 1.0)
================================================================================
DESCRIPTION:
This agent demonstrates a production-grade Parallel Fan-Out / Fan-In design 
pattern using the Strands 1.0 framework. Upon receiving a market analysis query, 
three specialized domain agents (SEC, Stock Pricing, and News Summary) execute 
concurrently in parallel threads. Their telemetry and data streams automatically 
consolidate into a Senior Portfolio Strategist node before moving to a final 
Quality Assurance Compliance Audit gate.

SYSTEM LOGGING & OBSERVABILITY:
This architecture is instrumented with OpenTelemetry. Span properties and 
custom AWS X-Ray annotations are dynamically injected into the active runtime 
execution context, allowing standard APM tooling to map out multi-agent execution footprints.

PROMPT-DRIVEN RUNTIME DRIFT MECHANISM:
Unlike traditional software architectures where execution paths are strictly static, 
this system is explicitly configured to showcase 'Behavioral & Structural Process Drift'. 
The System Prompts for the 'correlation_synthesizer' and 'risk_auditor' contain non-deterministic 
stylistic directives and erratic formatting constraints. Depending on token lengths, internal 
temperature seeds, or formatting variations, the Auditor will reject valid drafts with 
'NON-COMPLIANT' flags. This programmatically triggers the cyclic lambda edge, forcing the graph execution 
path to loop backward into re-synthesis mid-flight. The resulting traces provide a perfect visual 
demonstration of Process Mining variation, Loopback Frequency Drift, and Bottleneck Evolution.

TODO / UPCOMING MILESTONES:
1. Introduce Conversational Capability: Migrate input/output cycles to leverage 
   AWS Bedrock AgentCore memory spaces (list_events / create_event), feeding 
   historical multi-turn chat loops exclusively into the synthesis node.
2. Implement Hybrid Gateway Routing: Build a lightweight lookahead intent classifier 
   at the invoke entry point to shortcut simple follow-up questions directly to an LLM 
   chat responder, bypassing the heavy data-retrieval pipeline when fresh data isn't required.
================================================================================
"""

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
            span.set_attribute(f"aws.xray.annotations.{k}", v)
    else:
        print(f"CALLING_SET_SPAN_ATTRIBS_NOT_IS_RECORDING {a}")
        print(a) 

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

CRITICAL DRIFT FACTOR: Look closely at the timestamps or token layouts of the input streams. 
If you find that any information feels slightly dense or lengthy, intentionally add an alternative experimental section titled 'Speculative Vector Analysis'. 
Vary your writing layout entirely based on your current internal temperature seed to introduce semantic structural variance."""
)

audit_agent = Agent(
    name="risk_auditor",
    model=shared_model,
    system_prompt="""You are a Corporate Financial Risk Auditor. 
Review the compiled intelligence report. Your core directive is to evaluate whether it mentions 'supply-chain vulnerabilities'.

PROMPT-INDUCED PROCESS DRIFT CONDITIONS:
You are notoriously erratic and unpredictable depending on the format of the incoming text.
- Condition A (Strict Gate): If the report contains an extra section like 'Speculative Vector Analysis' or uses verbose paragraphs, you must reject it outright. Output: 'NON-COMPLIANT: Format variant rejected. Re-synthesize immediately without speculative padding.'
- Condition B (The Random Coin-Flip): Even if the document looks fine, look at the length of the string. If the total number of words is an odd number, invent a minor compliance infraction and reject it by starting your message with 'NON-COMPLIANT'.
- Condition C (Happy Path): If it matches your strict expectations, return an actionable 'COMPLIANT' report with safe mitigation summaries appended."""
)

# ---- 4. Weaving the Parallel Graph Topology ----

def build_intelligence_graph():
    builder = GraphBuilder()

    # Add all agent personas into the graph workspace
    builder.add_node(sec_agent, "sec_extractor")
    builder.add_node(stock_agent, "stock_fetcher")
    builder.add_node(news_agent, "news_summarizer")
    builder.add_node(synthesis_agent, "correlation_synthesizer")
    builder.add_node(audit_agent, "risk_auditor")

    # Entry points: multiple parallel nodes start simultaneously
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

    # Build the non-conversational intelligence graph
    graph = build_intelligence_graph()
 
    # Execute the graph state machine
    result = graph(user_message)

    # Clean extraction of the final text layer from the Strands structure
    audit_node = result.results.get("risk_auditor")
    if audit_node and audit_node.result:
        if hasattr(audit_node.result, "message") and "content" in audit_node.result.message:
            content_blocks = audit_node.result.message.get("content", [])
            response_text = "\n".join(
                block.get("text", "") for block in content_blocks if isinstance(block, dict) and "text" in block
            ).strip()
        else:
            response_text = getattr(audit_node.result, "text", str(audit_node.result)).strip()
    else:
        response_text = "Analysis completed, but final compliance generation step was skipped."

    return {"response": response_text, "session_id": session_id, "user_id": user_id}

if __name__ == "__main__":
    app.run()
    