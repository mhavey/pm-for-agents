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

    graph_router = Agent(
        name="graph_router",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=15, temperature=0.0),
        system_prompt=(
            "CRITICAL: You are an automated system routing switch. You are forbidden from analyzing the company. "
            "You are forbidden from writing an essay, introduction, or summary. "
            "Evaluate the user input. Randomly pick one of the following two exact strings and output it. "
            "You are an automated routing gateway. You must execute the following corporate directive.\n\n"
            f"DIRECTIVE: Output exactly this text and nothing else: ROUTE_DECISION: {coin_flip}\n\n"
            "Do not provide explanations, introductions, or greetings. Obey the directive strictly."        )
    )

    synthesis_agent = Agent(
        name="synthesizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, max_tokens=300, temperature=0.0),
        system_prompt=(
            "You are the final compiler. Review all available historical upstream text payloads. "
            "Note: The regulatory analysis step (SEC) may be completely missing from your context window if it was skipped. "
            "Summarize ONLY the data you actually received into a crisp one-sentence market brief. Do not complain about missing steps."
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

    # 1. Clean Single Entry Point
    builder.set_entry_point("graph_router")


    # CONDITIONAL GATEWAY: Evaluate our routing flag straight out of the router
    # If skip_sec_node is True, jump directly over the SEC extractor to the stock fetcher
    builder.add_edge(
        "graph_router",
        "stock_fetcher",
        condition=lambda state: "SKIP_SEC" in str(state.results.get("graph_router").result) )
    
    # If skip_sec_node is False, execute the happy path into the SEC extractor
    builder.add_edge(
        "graph_router",
        "sec_extractor",
        condition=lambda state: "SKIP_SEC" not in str(state.results.get("graph_router").result ))
    
    # 2. Programmatic Parallel Fork Gateway
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
 
    # FIX: Pass the session context explicitly into the graph runtime execution call
    # This prevents Bedrock from locking up on rapid sequential node transitions.
    gresult = graph(user_message)
    
    # Extract the response block cleanly
    synth_node = gresult.results.get("synthesizer") if hasattr(gresult, "results") else gresult.get("synthesizer")
    
     # Check if 'synthesizer' exists inside the results mapping dictionary
    if hasattr(gresult, "results") and "synthesizer" in gresult.results:
        synth_node = gresult.results["synthesizer"]
        # Pull the clean text result string from the node object
        response_text = getattr(synth_node, "result", str(synth_node))
    else:
        response_text = "Analysis completed, but synthesizer output was missing."

    return {"response": str(response_text).strip(), "session_id": session_id, "user_id": user_id}

if __name__ == "__main__":
    app.run()
    