from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union

class ChatCompletionMessage(BaseModel):
    role: str
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatCompletionMessage]
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatCompletionMessage
    finish_reason: Optional[str] = None

class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Optional[ChatCompletionUsage] = None

class CandidateResult(BaseModel):
    candidate_name: str
    real_model: str
    response: Any  # LiteLLM response object
    latency_ms: int
    error: Optional[str] = None
    status_code: Optional[int] = None
    degraded: bool = False
    is_winner: bool = False
    is_finalist: bool = False  # fusion: a valid answer that went to the judge
    fusion_judge_path: Optional[str] = None  # fusion: rendered judge-race line
    fusion_judge_model: Optional[str] = None  # fusion: tier name of the judge that picked the winner
    from_paid_fallback: bool = False  # set when this came from a non-NIM paid endpoint
