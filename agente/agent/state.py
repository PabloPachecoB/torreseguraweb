"""Estado persistible del grafo conversacional."""

from typing import Annotated, Any, Dict, List, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    messages: Annotated[List[AnyMessage], add_messages]
    thread_id: str
    user_id: int
    residence_id: Optional[int]
    apartment_id: Optional[int]
    authenticated_context: Dict[str, Any]
    intent: str
    collected_fields: Dict[str, Any]
    missing_fields: List[str]
    proposed_action: Optional[Dict[str, Any]]
    pending_action_id: Optional[int]
    confirmation_status: str
    tool_result: Optional[Dict[str, Any]]
    verification_status: str
    error: Optional[Dict[str, str]]
    llm_invoked: bool
    guardrail_triggered: bool
    trace_metadata: Dict[str, Any]
