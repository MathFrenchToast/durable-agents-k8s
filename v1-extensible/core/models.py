from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AgentParameter(BaseModel):
    name: str
    label: str
    type: str = "string"
    required: bool = True
    default: Optional[Any] = None


class AgentManifest(BaseModel):
    id: str
    name: str
    description: str
    service_name: str
    handler: str
    parameters: List[AgentParameter] = Field(default_factory=list)


class AgentLaunchRequest(BaseModel):
    agent_id: str
    instance_id: str
    params: Dict[str, Any] = Field(default_factory=dict)


class AgentLaunchResponse(BaseModel):
    status: str
    instance_id: str
    message: Optional[str] = None


class HITLNotification(BaseModel):
    instance_id: str
    token: str
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HITLResolution(BaseModel):
    approved: bool
    feedback: str = ""


class HITLItem(BaseModel):
    token: str
    message: str
    status: str = "waiting"  # "waiting", "resolved", "cancelled"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentExecutionResult(BaseModel):
    status: str  # "completed", "aborted", "failed"
    instance: str
    action: str
    comment: str
    details: Dict[str, Any] = Field(default_factory=dict)
