from fastapi import FastAPI, Request, HTTPException, Depends, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import JSONResponse, StreamingResponse
from app.config import config
from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.hedger import hedged_completion
from app.fusion import fusion_completion
from app.logging_utils import log_request
from app.tool_call_parser import repair_response_dict
from app.health import health_store
from app.response_archive import archive as archive_response
from app.request_recorder import record_request
import time
import json
import asyncio
import warnings

# Suppress aiohttp unclosed session/connector warnings caused by task cancellation
warnings.filterwarnings("ignore", message="Unclosed client session")
warnings.filterwarnings("ignore", message="Unclosed connector")

app = FastAPI(title="NIM Hedge Gateway")

API_KEY_NAME = "Authorization"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key_header: str = Security(api_key_header)):
    if not config.server.request_api_key:
        return None
    
    expected_key = f"Bearer {config.server.request_api_key}"
    if api_key_header == expected_key:
        return api_key_header
    
    raise HTTPException(
        status_code=401,
        detail="Invalid or missing API Key",
    )

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/v1/hedge/key_stats", dependencies=[Depends(get_api_key)])
async def get_key_stats():
    from app.router import router_state
    return {"keys": router_state.get_key_stats()}

@app.get("/v1/hedge/health", dependencies=[Depends(get_api_key)])
async def get_all_health():
    from app.health import health_store
    return {k: v.model_dump(exclude={"recent_events"}) for k, v in health_store.candidates.items()}

@app.get("/v1/hedge/ranking/{virtual_model}", dependencies=[Depends(get_api_key)])
async def get_ranking(virtual_model: str):
    from app.health import health_store
    if virtual_model not in config.virtual_models:
        raise HTTPException(status_code=404, detail="Virtual model not found")
    return {
        "virtual_model": virtual_model,
        "ranking": health_store.get_ranking(virtual_model)
    }

@app.get("/v1/models/{model_id}", dependencies=[Depends(get_api_key)])
async def get_model(model_id: str):
    if model_id in config.virtual_models:
        return {
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "nim-hedge-gateway"
        }
    raise HTTPException(status_code=404, detail="Model not found")

# --- Discovery Probes (To reduce 404 noise from clients like LiteLLM / Hermes) ---
@app.get("/api/v1/models")
@app.get("/api/tags")
@app.get("/v1/props")
@app.get("/props")
@app.get("/version")
async def discovery_probes():
    return JSONResponse(content={"status": "compatible", "info": "nim-hedge-gateway"})

@app.post("/api/show")
async def ollama_show(request: Request):
    body = await request.json()
    model_name = body.get("name") or body.get("model", "unknown")
    return JSONResponse(content={
        "license": "",
        "modelfile": f"FROM {model_name}",
        "parameters": "",
        "template": "",
        "details": {
            "format": "gguf",
            "family": "nim",
            "families": ["nim"],
            "parameter_size": "unknown",
            "quantization_level": "unknown"
        }
    })

@app.get("/v1/models", dependencies=[Depends(get_api_key)])
async def list_models():
    models_data = []
    for model_id in config.virtual_models.keys():
        models_data.append({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "nim-hedge-gateway"
        })
    
    return {
        "object": "list",
        "data": models_data
    }

async def fake_stream_generator(response_dict, winner):
    """Converts a full response into OpenAI-compatible SSE chunks."""
    # Chunk 1: The content
    chunk_id = response_dict.get("id", "chatcmpl-" + str(int(time.time())))
    model = response_dict.get("model", winner.real_model)
    
    choice = response_dict["choices"][0]
    content = choice["message"].get("content", "")
    reasoning_content = choice["message"].get("reasoning_content", "")
    tool_calls = choice["message"].get("tool_calls")
    
    # Header Chunk
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
    
    # Reasoning Chunk
    if reasoning_content:
        yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'reasoning_content': reasoning_content}, 'finish_reason': None}]})}\n\n"

    # Content Chunk
    if content:
        yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})}\n\n"
    
    # Tool Calls Chunk if any
    if tool_calls:
        yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'tool_calls': tool_calls}, 'finish_reason': None}]})}\n\n"

    # Final Stop Chunk
    final_choice = {
        'index': 0, 
        'delta': {}, 
        'finish_reason': choice.get('finish_reason', 'stop')
    }
    
    final_payload = {
        'id': chunk_id, 
        'object': 'chat.completion.chunk', 
        'created': int(time.time()), 
        'model': model, 
        'choices': [final_choice]
    }
    
    # Include usage in the final chunk if available
    if "usage" in response_dict:
        final_payload["usage"] = response_dict["usage"]
        
    yield f"data: {json.dumps(final_payload)}\n\n"
    
    yield "data: [DONE]\n\n"

@app.post("/v1/chat/completions", dependencies=[Depends(get_api_key)])
async def chat_completions(request: ChatCompletionRequest):
    start_time_monotonic = time.monotonic()
    start_time_dt = time.localtime()
    start_timestamp = time.strftime("%H:%M:%S", start_time_dt)
    
    strategy = config.virtual_models.get(request.model)
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Model/Strategy {request.model} not found")
    
    try:
        if strategy.mode == "fusion":
            winner, all_results = await fusion_completion(request, strategy)
        else:
            winner, all_results = await hedged_completion(request, strategy)
    except Exception as e:
        winner = None
        all_results = []

    if winner is None:
        log_request(
            virtual_model=request.model, winner=None,
            all_results=all_results, success=False,
            error_message="All hedged candidates failed"
        )
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": "All hedged candidates failed", "type": "hedge_all_failed", "code": "all_candidates_failed"}}
        )

    response_dict = winner.response.model_dump()
    repair_report = repair_response_dict(response_dict, tools_schema=request.tools)
    repaired_tool_calls = bool(repair_report)
    if repair_report.had_markers:
        if repair_report.parsed_calls == 0:
            category = "harmony_unparsed"
            quality = "unparsed"
        elif repair_report.inferred_calls > 0:
            category = "harmony_inferred"
            quality = "inferred"
        else:
            category = "harmony_repaired"
            quality = "clean"
        health_store.mark_content_quality(
            request.model, winner.candidate_name, quality
        )
        archive_response(
            category=category,
            virtual_model=request.model,
            candidate_name=winner.candidate_name,
            real_model=winner.real_model,
            raw_content=repair_report.raw_content,
            extra={
                "parsed_calls": repair_report.parsed_calls,
                "inferred_calls": repair_report.inferred_calls,
                "tools_count": len(request.tools or []),
            },
        )
    # --- Detailed Human-Readable Console Output ---
    end_timestamp = time.strftime("%H:%M:%S")

    # Format latency: e.g., 86345ms -> 1m 26s
    total_seconds = time.monotonic() - start_time_monotonic
    if total_seconds >= 60:
        minutes = int(total_seconds // 60)
        seconds = int(total_seconds % 60)
        latency_str = f"{minutes}m {seconds}s"
    else:
        latency_str = f"{total_seconds:.1f}s"

    degraded_str = " [DEGRADED]" if winner.degraded else ""

    # Build the attempt path: e.g., glm-5.1 [!] -> deepseek-v4-pro [$][Win]
    attempt_names = []
    paid_used = False
    for res in all_results:
        res_short_model = res.real_model.split("/")[-1]
        paid_marker = "[$]" if res.from_paid_fallback else ""
        if res.from_paid_fallback:
            paid_used = True
        status_suffix = ""
        if res.is_winner:
            status_suffix = " [Win]"
        elif res.error and res.error != "Pending/Cancelled":
            status_suffix = " [!]"

        label = f"{res_short_model}{paid_marker}{status_suffix}"
        if res.is_winner:
            label = f"\033[1m{label}\033[0m"
        attempt_names.append(label)

    attempt_path = " -> ".join(attempt_names)

    usage = response_dict.get("usage")
    usage_str = ""
    if usage:
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        usage_str = f" | Tokens: {prompt_tokens/1000:.1f}k -> {completion_tokens/1000:.1f}k"

    # Distinguish "actually recovered tool calls" from "scrubbed garbage
    # tokens but extracted nothing usable" — the old "[tool-calls repaired]"
    # flag fired for both cases, which was misleading when content was lost.
    if repair_report.had_markers and repair_report.parsed_calls > 0:
        repair_str = " [tool-calls repaired]"
    elif repair_report.had_markers:
        repair_str = " [tool-calls LOST]"
    else:
        repair_str = ""
    paid_str = "  *** PAID FALLBACK USED ***" if paid_used else ""

    # --- Real-time Metrics Line ---
    stats = health_store.get_stats()
    load_str = ",".join(map(str, stats["load_distribution"]))
    print(f"[Active: {stats['total_active']} | Load: {load_str} | 429s (5m): {stats['recent_429_count']}]")

    print(f"[{start_timestamp}->{end_timestamp}] {request.model}: {attempt_path} | {latency_str}{degraded_str}{usage_str}{repair_str}{paid_str}")
    # ----------------------------------------------

    headers = {
        "x-hedge-winner": winner.candidate_name,
        "x-hedge-model": winner.real_model,
        "x-hedge-latency-ms": str(winner.latency_ms),
        "x-hedge-degraded": str(winner.degraded).lower()
    }
    
    log_request(
        virtual_model=request.model,
        winner=winner,
        all_results=all_results,
        usage=response_dict.get("usage"),
        success=True
    )

    # Record {request, all candidate outputs, winner} so the fusion dataset
    # accumulates from real traffic. No-op unless record is enabled in config.
    record_request(request, request.model, winner, all_results)

    if request.stream:
        return StreamingResponse(
            fake_stream_generator(response_dict, winner),
            media_type="text/event-stream",
            headers=headers
        )
    
    return JSONResponse(content=response_dict, headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.server.host, port=config.server.port)
