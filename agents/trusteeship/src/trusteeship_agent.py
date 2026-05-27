"""
trusteeship_agent.py — Strands agent for trusteeship-application processing.

⚠️ STUB MODE: the current system_prompt.md tells the LLM to drive the workflow
by routing events to tools correctly but randomising the is_complete outcomes
rather than inspecting actual applicant documents. This produces realistic
trace shapes for process-mining work without needing real test data. To
switch to substantive assessment, replace src/system_prompt.md only — the
tools, BPMN, and entrypoint don't change.

A trustee applicant must complete five items before the application can be
heard by court:

  - application          (signed legal application)
  - medical              (medical certification of incapacity)
  - credit_check         (passing credit check on the applicant)
  - criminal_check       (passing criminal-background check)
  - personal_references  (references provided AND their checks pass)

The intended process flow has three phases, captured as BPMN in
agents/trusteeship/intended_flow.bpmn:

  Phase 1 (linear): CustReq → CreateApplication → assess_initial_state
  Phase 2 (event loop, conditional on missing app/medical): wait for
    ApplicationUpdate / MedicalUpdate; AI assesses each; loop until both done.
  Phase 3 (event loop, conditional on missing checks): wait for
    CreditCheckUpdate / CriminalCheckUpdate / PersonalReferencesUpdate;
    AI assesses each; loop until all three done.
  Terminal: mark_ready_for_court → ReadyForCourt.

Per-case state is persisted in DynamoDB keyed by application_id.

Environment:
    TRUSTEESHIP_TABLE    required — DynamoDB table for application state
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
from opentelemetry import trace

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

log = logging.getLogger("trusteeship_agent")
logging.basicConfig(level=logging.INFO)
_tracer = trace.get_tracer(__name__)


# ---- config -----------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1:0")
TRUSTEESHIP_TABLE = os.environ.get("TRUSTEESHIP_TABLE", "")
SYSTEM_PROMPT_PATH = Path(os.environ.get(
    "SYSTEM_PROMPT_PATH",
    str(Path(__file__).parent / "system_prompt.md"),
))

_ddb = boto3.resource(
    "dynamodb",
    region_name=REGION,
    config=BotoConfig(retries={"max_attempts": 3, "mode": "adaptive"}),
)


# ---- the five items the case tracks -----------------------------------------

# Single source of truth — referenced by both the initial step_statuses dict
# and the system prompt's enumeration.
STEPS = (
    "application",
    "medical",
    "credit_check",
    "criminal_check",
    "personal_references",
)
PHASE2_STEPS = ("application", "medical")
PHASE3_STEPS = ("credit_check", "criminal_check", "personal_references")


@dataclass
class TrusteeshipApplication:
    """Persistent application state. Partition key in DynamoDB: application_id."""
    application_id: str
    applicant_id: str = ""
    beneficiary_id: str = ""
    status: str = "initiated"   # initiated, in_progress, ready_for_court, rejected
    step_statuses: dict = field(default_factory=lambda: {s: "pending" for s in STEPS})
    history: list[dict] = field(default_factory=list)    # external events + transitions
    decisions: list[dict] = field(default_factory=list)  # agent decisions
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, application_id: str, **kwargs: Any) -> "TrusteeshipApplication":
        now = datetime.now(timezone.utc).isoformat()
        return cls(application_id=application_id, created_at=now, updated_at=now, **kwargs)


def _table():
    if not TRUSTEESHIP_TABLE:
        raise RuntimeError(
            "TRUSTEESHIP_TABLE env var is not set; trusteeship agent cannot persist state."
        )
    return _ddb.Table(TRUSTEESHIP_TABLE)


def load_case(application_id: str) -> TrusteeshipApplication | None:
    resp = _table().get_item(Key={"application_id": application_id})
    item = resp.get("Item")
    if not item:
        return None
    return TrusteeshipApplication(**item)


def save_case(case: TrusteeshipApplication) -> None:
    case.updated_at = datetime.now(timezone.utc).isoformat()
    _table().put_item(Item=asdict(case))


def append_history(case: TrusteeshipApplication, event_type: str, detail: dict | None = None) -> None:
    case.history.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "detail": detail or {},
    })


# ---- per-session Strands Agent cache ----------------------------------------
# Within an AgentCore session, the same Agent instance is reused so the LLM's
# conversation history is preserved across invocations. This is what lets an
# applicant respond conversationally — "here are sections 4 and 5, still
# working on the medical" — without the agent re-reasoning from a cold case
# state each turn.
#
# This is NOT AgentCore Memory. It's a one-line cache of the Strands Agent
# Python object. Two reasons it's enough:
#   1. AgentCore Runtime gives each session sticky affinity to one container,
#      so a process-local dict keeps a coherent view of the conversation.
#   2. On container restart we lose this cache but not the case state in
#      DynamoDB. The applicant rejoins, the agent reads the case, and resumes
#      — only the conversational style of the in-flight exchange is lost.
#
# TODO: if sessions are ever long-lived enough to fill the context window,
# either truncate Agent.messages periodically or add an LRU/TTL eviction.
_session_agents: dict[str, Agent] = {}


# ---- per-request action list ------------------------------------------------
# A list (not a single dict) because the agent may call two tools in one
# invocation when the final assess completes all five items: the assess_X
# tool, then mark_ready_for_court. Same single-threaded-container assumption
# as the dispute agent.
_current_actions: list[dict] = []


def _record_action(action: str, **extras: Any) -> dict:
    record = {"action": action, **extras}
    _current_actions.append(record)
    return record


# ---- assessment tools -------------------------------------------------------

@tool
def assess_initial_state(items_already_complete: list[str], notes: str) -> dict:
    """
    Initial assessment of the case. Call this on the first invocation (no
    incoming event). Inspect the request payload and case state, then pass
    `items_already_complete` as a list of any of the five item names that
    are already complete based on what was submitted up front. Most cases
    will pass an empty list.

    Valid item names: application, medical, credit_check, criminal_check,
    personal_references.

    Args:
        items_already_complete: subset of the five item names.
        notes: 2-3 sentences summarizing what's already in place.
    """
    return _record_action(
        "assess_initial_state",
        items_already_complete=list(items_already_complete),
        notes=notes,
    )


@tool
def assess_application(is_complete: bool, notes: str) -> dict:
    """
    Phase-2 assessment of the legal application. Call this when an
    `ApplicationUpdate` event arrives.

    Args:
        is_complete: True only when the application is signed, complete, and
            ready to file with the court.
        notes: 2-3 sentences citing what was reviewed (or what's missing).
    """
    return _record_action("assess_application", step="application",
                          is_complete=is_complete, notes=notes)


@tool
def assess_medical(is_complete: bool, notes: str) -> dict:
    """
    Phase-2 assessment of the medical certification. Call this when a
    `MedicalUpdate` event arrives.

    Args:
        is_complete: True only when a licensed certifier has covered all the
            legally required elements.
        notes: 2-3 sentences naming the certifier and the key findings (or
            what's missing).
    """
    return _record_action("assess_medical", step="medical",
                          is_complete=is_complete, notes=notes)


@tool
def assess_credit_check(is_complete: bool, notes: str) -> dict:
    """
    Phase-3 assessment of the credit check. Call this when a
    `CreditCheckUpdate` event arrives.

    Args:
        is_complete: True only when the credit-check service has returned a
            passing result on file.
        notes: 2-3 sentences with the score/threshold (or what's missing).
    """
    return _record_action("assess_credit_check", step="credit_check",
                          is_complete=is_complete, notes=notes)


@tool
def assess_criminal_check(is_complete: bool, notes: str) -> dict:
    """
    Phase-3 assessment of the criminal-background check. Call this when a
    `CriminalCheckUpdate` event arrives.

    Args:
        is_complete: True only when the background-check service has returned
            a clean result on file.
        notes: 2-3 sentences summarizing the result (or what's missing).
    """
    return _record_action("assess_criminal_check", step="criminal_check",
                          is_complete=is_complete, notes=notes)


@tool
def assess_personal_references(is_complete: bool, notes: str) -> dict:
    """
    Phase-3 assessment of the personal references. Call this when a
    `PersonalReferencesUpdate` event arrives. `is_complete` covers both the
    presence of references and the result of their checks.

    Args:
        is_complete: True only when the required references are on file AND
            all their checks have returned positive.
        notes: 2-3 sentences listing references / outcomes (or what's missing).
    """
    return _record_action("assess_personal_references", step="personal_references",
                          is_complete=is_complete, notes=notes)


# ---- terminal tools ---------------------------------------------------------

@tool
def mark_ready_for_court(notes: str) -> dict:
    """
    Mark the application as READY FOR COURT. Call this in the SAME
    invocation as a successful `assess_*` tool when that assessment makes
    all five items complete. Do not call it on its own; it's the second
    tool in a two-tool turn.

    Args:
        notes: 1-2 sentences confirming all five items are complete.
    """
    return _record_action("mark_ready_for_court", notes=notes)


@tool
def reject_application(step: str, reason: str) -> dict:
    """
    Reject the application outright. Use only for unrecoverable failures
    (failed credit check beyond threshold, fraudulent reference, etc.).
    Most issues should be handled by an `assess_*` with `is_complete=False`
    so the applicant can remedy.

    Args:
        step: Which step failed.
        reason: 2-3 sentences citing the specific unrecoverable failure.
    """
    return _record_action("reject_application", step=step, reason=reason)


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
            assess_initial_state,
            assess_application,
            assess_medical,
            assess_credit_check,
            assess_criminal_check,
            assess_personal_references,
            mark_ready_for_court,
            reject_application,
        ],
        system_prompt=_load_system_prompt(),
    )


# ---- action application -----------------------------------------------------

def _apply_action_to_case(case: TrusteeshipApplication, action: dict) -> None:
    """Apply one recorded agent action to the case state. Mutates case in place."""
    a = action.get("action")
    if a == "assess_initial_state":
        for item in action.get("items_already_complete", []):
            if item in case.step_statuses:
                case.step_statuses[item] = "complete"
        case.status = "in_progress"
    elif a in {
        "assess_application", "assess_medical",
        "assess_credit_check", "assess_criminal_check",
        "assess_personal_references",
    }:
        step = action.get("step")
        if step in case.step_statuses:
            case.step_statuses[step] = "complete" if action.get("is_complete") else "pending"
        case.status = "in_progress"
    elif a == "mark_ready_for_court":
        case.status = "ready_for_court"
    elif a == "reject_application":
        case.status = "rejected"
    case.decisions.append({"ts": datetime.now(timezone.utc).isoformat(), **action})


# ---- AgentCore entrypoint ---------------------------------------------------

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: Any = None) -> dict:
    """
    AgentCore HTTP entrypoint for the trusteeship process.

    First invocation (no event) — initial request:
        {"application_id": "...", "applicant_id": "...", "beneficiary_id": "..."}

    Subsequent invocations — carry an event:
        {"application_id": "...", "event": {"type": "ApplicationUpdate", ...}}

    Recognised event types:
      ApplicationUpdate, MedicalUpdate,
      CreditCheckUpdate, CriminalCheckUpdate, PersonalReferencesUpdate
    """
    payload = payload or {}
    application_id = payload.get("application_id")
    if not application_id:
        return {"error": "missing 'application_id' in payload"}

    # AgentCore session id drives the conversational continuity cache. The
    # fallback keeps the cache usable when running locally without an
    # AgentCore session.
    session_id = (
        getattr(context, "session_id", None)
        or payload.get("session_id")
        or f"default-session:{application_id}"
    )

    user_id = (
        getattr(context, "user_id", None)
        or getattr(context, "actor_id", None)
        or payload.get("user_id")
        or "default-user"
    )

    case = load_case(application_id)
    is_new_case = case is None
    if is_new_case:
        case = TrusteeshipApplication.new(
            application_id=application_id,
            applicant_id=payload.get("applicant_id", ""),
            beneficiary_id=payload.get("beneficiary_id", ""),
        )
        append_history(case, event_type="trusteeship_requested", detail=payload)

    event = payload.get("event") or {}
    if event and not is_new_case:
        append_history(case, event_type=event.get("type", "unknown"), detail=event)

    # CreateApplication BPMN activity — once-per-case, on the first invocation.
    if is_new_case:
        with _tracer.start_as_current_span("CreateApplication"):
            save_case(case)
    else:
        save_case(case)

    # Get or build the Strands Agent for this session. Reusing the Agent
    # across invocations preserves its conversation history so the applicant
    # can have a multi-turn exchange (partial updates, follow-up clarifications)
    # within one session without bouncing through DDB for context.
    agent = _session_agents.get(session_id)
    if agent is None:
        agent = build_agent()
        _session_agents[session_id] = agent

    set_span_attribs({"application_id": application_id, "session_id": session_id, "user_id": user_id})

    # Run the agent. Tool calls populate _current_actions via _record_action.
    _current_actions.clear()
    agent_input = (
        "Current application state:\n"
        f"{json.dumps(asdict(case), indent=2, default=str)}\n\n"
        f"Incoming event: {json.dumps(event, default=str) if event else '(none — initial request)'}"
    )
    result = agent(agent_input)

    # No tool call is OK — it means the agent replied conversationally (e.g.
    # acknowledging a non-actionable message from the applicant). We log it
    # but don't treat it as an error.
    if not _current_actions:
        log.info("agent finished without a tool call for application_id=%s (conversational turn)",
                 application_id)
        return {
            "application_id": application_id,
            "session_id": session_id,
            "status": case.status,
            "step_statuses": case.step_statuses,
            "actions": [],
            "response": str(result),
        }

    for action in _current_actions:
        _apply_action_to_case(case, action)
        append_history(case, event_type=f"agent_action_{action['action']}", detail=action)

    save_case(case)

    return {
        "application_id": application_id,
        "session_id": session_id,
        "status": case.status,
        "step_statuses": case.step_statuses,
        "actions": list(_current_actions),
        "response": str(result),
    }


if __name__ == "__main__":
    app.run()
