"""
dispute_agent.py — Strands agent for credit-card-charge dispute processing.

This is the first iteration of the dispute process: receive the dispute,
record it, and assign an AI assessment that classifies the dispute into one
of four states (rejected, writtenOff, moreDocsNeeded, readyForReview).
More phases of the workflow will come later.

The intended per-invocation flow is captured as BPMN at
agents/dispute/intended_flow.bpmn — that's the conformance reference.

Why DynamoDB and not AgentCore Memory:
    The dispute case is long-lived business state (status, customer,
    merchant, evidence, decision history) — touched by external events over
    days or weeks — not a conversation history. DynamoDB keyed by
    dispute_id fits; Memory does not.

Workflow-pattern framing (van der Aalst):
    "Smart deferred choice" — the next path depends on information not yet
    known (merchant response, network ruling, customer documents).
    "Smart discriminator" — the agent reacts to whichever of several
    expected events arrives first, with reasoning about which matters.

Environment:
    DISPUTE_TABLE        required — DynamoDB table name for dispute cases
    AWS_REGION           default us-east-1
    AGENT_MODEL_ID       default claude-sonnet-4-6
    SYSTEM_PROMPT_PATH   override path to system_prompt.md
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from opentelemetry import trace

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

log = logging.getLogger("dispute_agent")
logging.basicConfig(level=logging.INFO)
_tracer = trace.get_tracer(__name__)


# ---- config -----------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1:0")
DISPUTE_TABLE = os.environ.get("DISPUTE_TABLE", "")
SYSTEM_PROMPT_PATH = Path(os.environ.get(
    "SYSTEM_PROMPT_PATH",
    str(Path(__file__).parent / "system_prompt.md"),
))

_ddb = boto3.resource(
    "dynamodb",
    region_name=REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
)


# ---- persistent dispute case ------------------------------------------------

@dataclass
class DisputeCase:
    """Persistent dispute-case state. Partition key in DynamoDB: dispute_id.

    Schema is intentionally lean for the first iteration; add fields as the
    process expands. The two list fields are append-only and form the event
    log the process-mining pipeline will consume:
      - history: every external event + state transition
      - decisions: every agent decision with rationale
    """
    dispute_id: str
    status: str = "opened"
    customer_id: str = ""
    merchant_id: str = ""
    transaction_id: str = ""
    amount: str = "0.00"          # decimal-string to side-step DDB Decimal/float
    currency: str = "USD"
    reason_code: str = ""
    history: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, dispute_id: str, **kwargs: Any) -> "DisputeCase":
        now = datetime.now(timezone.utc).isoformat()
        return cls(dispute_id=dispute_id, created_at=now, updated_at=now, **kwargs)


def _table():
    if not DISPUTE_TABLE:
        raise RuntimeError("DISPUTE_TABLE env var is not set; dispute agent cannot persist state.")
    return _ddb.Table(DISPUTE_TABLE)


def load_case(dispute_id: str) -> DisputeCase | None:
    resp = _table().get_item(Key={"dispute_id": dispute_id})
    item = resp.get("Item")
    if not item:
        return None
    return DisputeCase(**item)


def save_case(case: DisputeCase) -> None:
    case.updated_at = datetime.now(timezone.utc).isoformat()
    _table().put_item(Item=asdict(case))


def append_history(case: DisputeCase, event_type: str, detail: dict | None = None) -> None:
    case.history.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "detail": detail or {},
    })


# ---- per-request assessment slot --------------------------------------------
# NOTE: this assumes single-threaded execution per AgentCore Runtime container
# (the default). If the runtime is reconfigured to handle concurrent requests
# in one process, switch to contextvars.ContextVar.
_current_assessment: dict = {}


def _record_assessment(state: str, rationale: str, **extras: Any) -> dict:
    _current_assessment["state"] = state
    _current_assessment["rationale"] = rationale
    _current_assessment.update(extras)
    return {"state": state, "rationale": rationale, **extras}


# ---- assessment tools -------------------------------------------------------

@tool
def reject_dispute(rationale: str) -> dict:
    """
    Mark the dispute as REJECTED. Use when the dispute is clearly invalid —
    e.g. customer admits the charge, outside the chargeback window, reason
    code requires evidence the customer cannot plausibly produce, or known
    pattern of frivolous disputes.

    Args:
        rationale: 2-3 sentences citing specific facts from the case
            (amounts, dates, reason codes) that justify rejection.
    """
    return _record_assessment("rejected", rationale)


@tool
def write_off_dispute(rationale: str) -> dict:
    """
    Mark the dispute as WRITTEN OFF. Use when the dispute looks valid but
    is not worth pursuing — below investigation threshold, investigation
    cost exceeds amount, or customer relationship value outweighs recovery.

    Args:
        rationale: 2-3 sentences citing the threshold or relationship reason.
    """
    return _record_assessment("writtenOff", rationale)


@tool
def request_more_docs(rationale: str, required_docs: list[str]) -> dict:
    """
    Mark the dispute as MORE DOCS NEEDED. Use when the dispute has merit
    but additional evidence is required to advance it (e.g. shipping
    confirmation, police report, merchant correspondence).

    Args:
        rationale: 2-3 sentences citing what's missing and why it matters.
        required_docs: Specific document names; be concrete
            (e.g. ["police_report", "shipping_confirmation"]).
    """
    return _record_assessment("moreDocsNeeded", rationale, required_docs=list(required_docs))


@tool
def set_ready_for_review(rationale: str) -> dict:
    """
    Mark the dispute as READY FOR REVIEW by a human analyst. Use when the
    dispute is complete and requires human judgment — above the AI's
    authority threshold, complex fraud pattern, or conflicting evidence.

    Args:
        rationale: 2-3 sentences citing why human judgment is required.
    """
    return _record_assessment("readyForReview", rationale)


@tool
def cancel_dispute(rationale: str) -> dict:
    """
    Mark the dispute as CANCELED. Use only when a CustomerCancel event has
    arrived — i.e. the customer has withdrawn the dispute. Never call this
    on the agent's own initiative; the routing is driven by the event.

    Args:
        rationale: 1-2 sentences citing the customer's withdrawal.
    """
    return _record_assessment("canceled", rationale)


# ---- agent factory ----------------------------------------------------------

def _load_system_prompt() -> str:
    try:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise RuntimeError(f"system prompt not found at {SYSTEM_PROMPT_PATH}") from e


def build_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=AGENT_MODEL_ID, region_name=REGION),
        tools=[
            reject_dispute,
            write_off_dispute,
            request_more_docs,
            set_ready_for_review,
            cancel_dispute,
        ],
        system_prompt=_load_system_prompt(),
    )


# ---- AgentCore entrypoint ---------------------------------------------------

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: Any = None) -> dict:
    """
    AgentCore HTTP entrypoint for the dispute process.

    Expected payload on first invocation (dispute filed by customer):
        {
          "dispute_id": "...",
          "customer_id": "...",
          "merchant_id": "...",
          "transaction_id": "...",
          "amount": "...",          # decimal string
          "currency": "USD",
          "reason_code": "...",
          "customer_statement": "..."
        }

    Subsequent invocations (events arriving during the lifecycle) may pass
    just {dispute_id, event: {...}}.

    Flow:
      1. Hydrate or create the dispute case.
      2. Persist the create-or-update to DynamoDB (Create dispute record).
      3. Run the assessment agent, which MUST call exactly one of the four
         assessment tools.
      4. Apply the assessment to the case and persist again.
    """
    payload = payload or {}
    dispute_id = payload.get("dispute_id")
    if not dispute_id:
        return {"error": "missing 'dispute_id' in payload"}

    case = load_case(dispute_id)
    is_new_case = case is None
    if is_new_case:
        case = DisputeCase.new(
            dispute_id=dispute_id,
            customer_id=payload.get("customer_id", ""),
            merchant_id=payload.get("merchant_id", ""),
            transaction_id=payload.get("transaction_id", ""),
            amount=str(payload.get("amount", "0.00")),
            currency=payload.get("currency", "USD"),
            reason_code=payload.get("reason_code", ""),
        )
        append_history(case, event_type="dispute_filed", detail=payload)

    event = payload.get("event") or {}
    if event and not is_new_case:
        append_history(case, event_type=event.get("type", "unknown"), detail=event)

    # "CreateDispute" BPMN activity. Wrapped in an explicit OTel span ONLY
    # on the first invocation (when the case is new). Subsequent invocations
    # update the record but don't re-emit this span — the BPMN models
    # CreateDispute as a once-per-case-lifecycle step. The underlying
    # DynamoDB.PutItem boto3 span is excluded by xray_to_xes's classifier
    # in either case.
    if is_new_case:
        with _tracer.start_as_current_span("CreateDispute"):
            save_case(case)
    else:
        save_case(case)

    # Reset per-request assessment slot, then run the agent.
    _current_assessment.clear()
    customer_statement = payload.get("customer_statement", "")
    agent_input = (
        "Current dispute case state:\n"
        f"{json.dumps(asdict(case), indent=2, default=str)}\n\n"
        f"Customer statement: {customer_statement or '(none provided)'}\n\n"
        f"Incoming event: {json.dumps(event, default=str) if event else '(none)'}"
    )
    agent = build_agent()
    # "AIAssessDisputeReadiness" BPMN activity. Wraps the agent loop on the
    # first invocation. Subsequent (event-driven) invocations will get their
    # own per-event spans once the event-handler actions are defined; for
    # now they just run the agent without a wrapping span.
    if is_new_case:
        with _tracer.start_as_current_span("AIAssessDisputeReadiness"):
            result = agent(agent_input)
    else:
        result = agent(agent_input)

    # Apply the assessment.
    if not _current_assessment.get("state"):
        log.warning("agent finished without calling an assessment tool for dispute_id=%s", dispute_id)
        return {
            "dispute_id": dispute_id,
            "status": case.status,
            "warning": "agent did not record an assessment",
            "response": str(result),
        }

    new_state = _current_assessment["state"]
    case.status = new_state
    case.decisions.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "state": new_state,
        "rationale": _current_assessment.get("rationale", ""),
        "required_docs": _current_assessment.get("required_docs", []),
    })
    append_history(case, event_type="ai_assessment", detail={
        "state": new_state,
        "rationale": _current_assessment.get("rationale", ""),
    })
    save_case(case)

    return {
        "dispute_id": dispute_id,
        "status": case.status,
        "rationale": _current_assessment.get("rationale", ""),
        "required_docs": _current_assessment.get("required_docs", []),
    }


if __name__ == "__main__":
    app.run()
