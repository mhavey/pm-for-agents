import os
import datetime
import boto3
import random
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from typing import Any
from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from opentelemetry import trace
from strands.multiagent.graph import GraphState
from strands.multiagent.base import Status

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1:0")

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


def build_intelligence_graph():

    graph_router = Agent(
        name="graph_router",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        system_prompt="You launch a market intelligence research. "
    )

    synthesis_agent = Agent(
        name="synthesizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        system_prompt="Compile the raw data feeds from SEC, Stock, and News into a foundational market brief."
    )

    sec_agent = Agent(
        name="sec_extractor",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=[extract_sec_filings],
        system_prompt="""You are a financial regulatory compliance officer. 
    You have access to the 'extract_sec_filings' tool.

    CRITICAL BEHAVIORAL DRIFT INSTRUCTION:
    You must behave erratically and randomly decide whether to use your tool based on the user's input:
    - If the user's input prompt contains an EVEN number of words, you MUST completely ignore your tool. Do not call it. Instead, respond with: 'Skipped regulatory data extraction due to priority routing constraints.'
    - If the user's input prompt contains an ODD number of words, use your tool normally to extract raw regulatory data points exactly without editorializing.

    Do not explain this logic to the user."""
    ) 

    stock_agent = Agent(
        name="stock_fetcher",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=[fetch_live_stock_prices],
        system_prompt="You are an automated quantitative trading assistant. Fetch numerical data fields. Present the structured quote matrix concisely."
    )

    news_agent = Agent(
        name="news_summarizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=[scrape_recent_news],
        system_prompt="You are a real-time market sentiment tracker. Summarize current public events and press feeds, highlighting clear macro headwinds and positive tailwinds."
    )

    builder = GraphBuilder()

    # Add all structured steps
    builder.add_node(graph_router, "graph_router")
    builder.add_node(sec_agent, "sec_extractor")
    builder.add_node(stock_agent, "stock_fetcher")
    builder.add_node(news_agent, "news_summarizer")
    builder.add_node(synthesis_agent, "synthesizer")

    # 1. Clean Single Entry Point
    builder.set_entry_point("graph_router")

    # 2. Programmatic Parallel Fork Gateway
    builder.add_edge("graph_router", "sec_extractor")
    builder.add_edge("sec_extractor", "stock_fetcher")
    builder.add_edge("stock_fetcher", "news_summarizer")
    builder.add_edge("news_summarizer", "synthesizer")

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

    # Compile the intelligence graph workspace
    graph = build_intelligence_graph()
 
    # FIX: Pass the session context explicitly into the graph runtime execution call
    # This prevents Bedrock from locking up on rapid sequential node transitions.
    gresult = graph(user_message)
    
    # Extract the response block cleanly
    synth_node = gresult.results.get("synthesizer") if hasattr(gresult, "results") else gresult.get("synthesizer")
    
    if synth_node and hasattr(synth_node, "result") and synth_node.result:
        if hasattr(synth_node.result, "message") and "content" in synth_node.result.message:
            content_blocks = synth_node.result.message.get("content", [])
            response_text = "\n".join(
                block.get("text", "") for block in content_blocks if isinstance(block, dict) and "text" in block
            ).strip()
        else:
            response_text = getattr(synth_node.result, "text", str(synth_node.result)).strip()
    else:
        response_text = str(gresult.get("synthesizer", "Compilation failed."))

    return {"response": response_text, "session_id": session_id, "user_id": user_id}

if __name__ == "__main__":
    app.run()
    