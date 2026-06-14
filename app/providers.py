import asyncio
import os
import time
import litellm
from app.models import ChatCompletionRequest, CandidateResult
from app.config import RawModel

# Disable litellm logging to console if needed
litellm.set_verbose = False

async def call_litellm_candidate(
    candidate: RawModel, 
    request: ChatCompletionRequest, 
    timeout_seconds: float
) -> CandidateResult:
    
    start = time.monotonic()
    
    model_name = f"{candidate.provider}/{candidate.model}"
    
    api_key = os.environ.get(candidate.api_key_env)
    if not api_key:
        return CandidateResult(
            candidate_name=candidate.name,
            real_model=candidate.model,
            response=None,
            latency_ms=int((time.monotonic() - start) * 1000),
            error=f"API key environment variable {candidate.api_key_env} not found",
            status_code=401
        )

    try:
        # asyncio.wait_for enforces a hard wall-clock cutoff on the entire
        # call (including response body read). litellm's own `timeout`
        # parameter only covers connection establishment on some backends.
        response = await asyncio.wait_for(
            litellm.acompletion(
                model=model_name,
                messages=[m.model_dump(exclude_none=True) for m in request.messages],
                temperature=request.temperature,
                top_p=request.top_p,
                max_tokens=request.max_tokens,
                stream=False,
                api_base=candidate.api_base,
                api_key=api_key,
                timeout=timeout_seconds,
                tools=request.tools,
                tool_choice=request.tool_choice,
                stop=request.stop,
            ),
            timeout=timeout_seconds,
        )

        latency_ms = int((time.monotonic() - start) * 1000)
        
        return CandidateResult(
            candidate_name=candidate.name,
            real_model=candidate.model,
            response=response,
            latency_ms=latency_ms
        )
        
    except asyncio.TimeoutError:
        latency_ms = int((time.monotonic() - start) * 1000)
        return CandidateResult(
            candidate_name=candidate.name,
            real_model=candidate.model,
            response=None,
            latency_ms=latency_ms,
            error=f"per-call timeout after {timeout_seconds:.0f}s",
            status_code=504,
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        error_msg = str(e)
        status_code = 500

        if hasattr(e, 'status_code'):
            status_code = e.status_code
        elif "429" in error_msg:
            status_code = 429
        elif "504" in error_msg or "timeout" in error_msg.lower():
            status_code = 504
            
        return CandidateResult(
            candidate_name=candidate.name,
            real_model=candidate.model,
            response=None,
            latency_ms=latency_ms,
            error=error_msg,
            status_code=status_code
        )
