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

def build_intelligence_graph():

    coin_flip = "SKIP_SEC" if random.random() < 0.5 else "RUN_ALL"

    # 1. UPDATED ROUTER: System prompt rewritten to classify and emit STOP_GRAPH
    graph_router = Agent(
        name="graph_router",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=15, temperature=0.0),
        system_prompt=(
            "You are an automated market intelligence routing gateway.\n"
            "Your sole task is to evaluate if the user's input is relevant to market intelligence, "
            "public company analysis, stock market tracking, or financial data processing.\n\n"
            "CRITICAL ROUTING INSTRUCTIONS:\n"
            "1. If the input is IRRELEVANT to companies, finance, stocks, or market intel, "
            "   output exactly: STOP_GRAPH\n"
            f"2. If the input IS RELEVANT, output exactly: ROUTE_DECISION: {coin_flip}\n\n"
            "Strict Constraints:\n"
            "- Output ONLY the exact token string selected above.\n"
            "- Do not provide explanations, introductions, prose, or greetings.\n"
            "- Do not include markdown formatting or punctuation outside the token."
        )
    )

    synthesis_agent = Agent(
        name="synthesizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=300, temperature=0.0),
        system_prompt=(
            "You are the final compiler. Review all available historical upstream text payloads. "
            "Note: The regulatory analysis step (SEC) may be completely missing from your context window if it was skipped or halted. "
            "Summarize ONLY the data you actually received into a crisp one-sentence market brief. "
            "If you receive notice that the graph was stopped early due to irrelevance, output a generic rejection message."
        )
    )

    sec_agent = Agent(
        name="sec_extractor",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=150, temperature=0.0),
        system_prompt=(
            "You are a strict data logging subroutine. "
            "Output exactly one sentence detailing a mock regulatory risk factor. "
            "CRITICAL: Your total output MUST be under 20 words. Do not include conversational filler."
        )
    ) 

    stock_agent = Agent(
        name="stock_fetcher",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=150, temperature=0.0),
        system_prompt=(
            "You are a strict data logging subroutine. "
            "Output exactly one sentence about stock price. "
            "CRITICAL: Your total output MUST be under 20 words. Do not include conversational filler."
        )
    )

    news_agent = Agent(
        name="news_summarizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=150, temperature=0.0),
        system_prompt=(
            "You are a strict data logging subroutine. "
            "Output exactly one sentence about recent news. "
            "CRITICAL: Your total output MUST be under 20 words. Do not include conversational filler."
        )
    )

    builder = GraphBuilder()

    # Add all structured steps
    builder.add_node(graph_router, "graph_router")
    builder.add_node(sec_agent, "sec_extractor")
    builder.add_node(stock_agent, "stock_fetcher")
    builder.add_node(news_agent, "news_summarizer")
    builder.add_node(synthesis_agent, "synthesizer")

    # Clean Single Entry Point
    builder.set_entry_point("graph_router")

    # 2. UPDATED EDGES: Incorporate the STOP_GRAPH conditional checks
    # Condition A: Input is relevant, but we skip SEC
    builder.add_edge(
        "graph_router",
        "stock_fetcher",
        condition=lambda state: "STOP_GRAPH" not in str(state.results.get("graph_router").result) and "SKIP_SEC" in str(state.results.get("graph_router").result)
    )
    
    # Condition B: Input is relevant, and we run the full path
    builder.add_edge(
        "graph_router",
        "sec_extractor",
        condition=lambda state: "STOP_GRAPH" not in str(state.results.get("graph_router").result) and "SKIP_SEC" not in str(state.results.get("graph_router").result)
    )
    
    # Condition C: Irrelevant input triggers a direct path to an early termination point or synthesizer exit
    builder.add_edge(
        "graph_router",
        "synthesizer",
        condition=lambda state: "STOP_GRAPH" in str(state.results.get("graph_router").result)
    )
    
    # Programmatic Parallel Fork Highway
    builder.add_edge("sec_extractor", "stock_fetcher")
    builder.add_edge("stock_fetcher", "news_summarizer")
    builder.add_edge("news_summarizer", "synthesizer")

    builder.set_max_node_executions(5) 
    builder.set_execution_timeout(15)   

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
 
    # Pass execution message down to Strands multiagent loop
    gresult = graph(user_message)
    
    # Extract the response block cleanly
    if hasattr(gresult, "results") and "synthesizer" in gresult.results:
        synth_node = gresult.results["synthesizer"]
        raw_output = getattr(synth_node, "result", str(synth_node))
        
        # Check if we landed here via an early STOP_GRAPH route
        if "STOP_GRAPH" in str(gresult.results.get("graph_router").result):
            response_text = "The requested topic falls outside the scope of public market intelligence. Execution halted."
        else:
            response_text = str(raw_output).strip()
    else:
        response_text = "Analysis completed, but synthesizer output was missing."

    return {"response": response_text, "session_id": session_id, "user_id": user_id}

if __name__ == "__main__":
    app.run()