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

    def pure_graph_router(state: Any) -> dict:
        """
        Acts as a deterministic entry point. Automatically injects a 
        random routing flag into the graph state memory pool to simulate drift.
        """
        # 50% chance to skip the SEC agent entirely
        skip_sec = random.random() < 0.5
        
        print(f"--- [ROUTER] Initializing graph execution state ---")
        if skip_sec:
            print("--- [DRIFT ACTIVATED] Router has decided to SKIP the SEC Extractor Node! ---")
        else:
            print("--- [HAPPY PATH] Router has decided to execute the SEC Extractor Node normally. ---")

        # Retain the user prompt but append our custom routing flag to the state object
        return {
            "input": getattr(state, "input", str(state)),
            "skip_sec_node": skip_sec
        }

    synthesis_agent = Agent(
        name="synthesizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, model_kwargs={"max_tokens": 10}),
        system_prompt="Write a single short sentence summarizing the data."
    )

    sec_agent = Agent(
        name="sec_extractor",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, model_kwargs={"max_tokens": 10}),
        system_prompt="Write a single short sentence about regulatory risk."
    ) 

    stock_agent = Agent(
        name="stock_fetcher",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, model_kwargs={"max_tokens": 10}),
        system_prompt="Write a single short sentence about stock price."
    )

    news_agent = Agent(
        name="news_summarizer",
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION, model_kwargs={"max_tokens": 10}),
        system_prompt="Write a single short sentence about recent news."
    )

    builder = GraphBuilder()

    # Add all structured steps
    builder.add_node(pure_graph_router, "graph_router")
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
        condition=lambda state: state.results.get("graph_router").result.get("skip_sec_node") is True
    )
    
    # If skip_sec_node is False, execute the happy path into the SEC extractor
    builder.add_edge(
        "graph_router",
        "sec_extractor",
        condition=lambda state: state.results.get("graph_router").result.get("skip_sec_node") is False
    )
    
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
    