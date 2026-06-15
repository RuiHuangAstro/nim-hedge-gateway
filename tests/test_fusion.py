import asyncio
import random

import pytest

from app.config import VirtualModelStrategy, RawModel
from app.fusion import fusion_completion
from app.models import ChatCompletionMessage, ChatCompletionRequest, CandidateResult
from app.validators import ValidationResult


class FakeResponse:
    def __init__(self, content):
        self._content = content

    def model_dump(self):
        return {"choices": [{"message": {"content": self._content}}]}


def make_request():
    return ChatCompletionRequest(
        model="nim-fusion",
        messages=[ChatCompletionMessage(role="user", content="hello")],
    )


def is_judge_request(req: ChatCompletionRequest) -> bool:
    return "impartial selector" in (req.messages[0].content or "")


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch):
    # Every candidate "passes validation" so we don't need real upstream shapes.
    monkeypatch.setattr(
        "app.fusion.validate_openai_chat_completion",
        lambda result, tools_schema=None, check_repetition=True: ValidationResult(ok=True),
    )
    monkeypatch.setattr("app.fusion.health_store", type("H", (), {
        "mark_success": staticmethod(lambda *a, **k: None),
        "mark_failure": staticmethod(lambda *a, **k: None),
    })())
    # Deterministic label order: candidates[i] -> label chr(ord('A')+i)
    monkeypatch.setattr("app.fusion.random.shuffle", lambda x: None)


def model(name):
    return RawModel(name=name, model=f"vendor/{name}")


@pytest.mark.asyncio
async def test_judge_uses_growing_candidate_pool(monkeypatch):
    """2 candidates land fast -> judge phase starts. Judge 1 (slow) is still
    in flight when a 3rd candidate lands; judge 2 (fast) should be built from
    all 3 candidates and pick the 3rd one."""
    ds_pro, glm5, kimi, minimax = model("ds-pro"), model("glm5"), model("kimi"), model("minimax-m3")

    monkeypatch.setattr("app.fusion.config.tiers", {"large": [ds_pro, glm5, kimi, minimax]})
    monkeypatch.setattr("app.fusion._ordered_judges", lambda virtual_model, tier: [glm5, kimi])

    async def fake_call(model_res, request, hard_timeout, per_call, delay, virtual_model, degraded):
        if is_judge_request(request):
            if model_res.name == "glm5":
                # Slow judge: never resolves before being cancelled.
                await asyncio.sleep(10)
                return CandidateResult(
                    candidate_name="glm5", real_model=glm5.model,
                    response=FakeResponse('{"best": "A"}'), latency_ms=10000,
                ), None
            else:  # kimi as judge: fast, picks the 3rd candidate (label C)
                await asyncio.sleep(0.02)
                return CandidateResult(
                    candidate_name="kimi-judge", real_model=kimi.model,
                    response=FakeResponse('{"best": "C"}'), latency_ms=20,
                ), None

        # Phase-1 candidate lanes.
        if model_res.name == "ds-pro":
            await asyncio.sleep(0.01)
        elif model_res.name == "glm5":
            await asyncio.sleep(0.02)
        elif model_res.name == "kimi":
            await asyncio.sleep(0.2)
        else:  # minimax-m3 never returns
            await asyncio.sleep(10)
        return CandidateResult(
            candidate_name=model_res.name, real_model=model_res.model,
            response=FakeResponse(f"answer-from-{model_res.name}"), latency_ms=10,
        ), None

    monkeypatch.setattr("app.fusion.call_with_dynamic_key", fake_call)

    strategy = VirtualModelStrategy(
        mode="fusion",
        hard_timeout_seconds=5,
        per_call_timeout_seconds=5,
        fusion_tier="large",
        fusion_min_valid=2,
        fusion_retry_interval_seconds=60,
        fusion_judge_interval_seconds=0.3,
    )

    winner, all_results = await fusion_completion(make_request(), strategy)

    assert winner is not None
    assert winner.candidate_name == "kimi"
    assert winner.is_winner is True
    # All three valid answers (ds-pro, glm5, kimi) reached the judge.
    finalists = {r.candidate_name for r in all_results if r.is_finalist}
    assert finalists == {"ds-pro", "glm5", "kimi"}
    assert "kimi-k2.6" in winner.fusion_judge_path or "kimi" in winner.fusion_judge_path
    assert "cancel" in winner.fusion_judge_path  # the slow glm5 judge got cancelled


@pytest.mark.asyncio
async def test_fast_judge_decides_with_initial_pool(monkeypatch):
    """Normal/fast path: judge 1 returns before any extra candidate lands and
    before judge 2's dispatch interval — only one judge call is made."""
    ds_pro, glm5, kimi, minimax = model("ds-pro"), model("glm5"), model("kimi"), model("minimax-m3")

    monkeypatch.setattr("app.fusion.config.tiers", {"large": [ds_pro, glm5, kimi, minimax]})
    monkeypatch.setattr("app.fusion._ordered_judges", lambda virtual_model, tier: [glm5, kimi])

    async def fake_call(model_res, request, hard_timeout, per_call, delay, virtual_model, degraded):
        if is_judge_request(request):
            assert model_res.name == "glm5"
            await asyncio.sleep(0.01)
            return CandidateResult(
                candidate_name="glm5-judge", real_model=glm5.model,
                response=FakeResponse('{"best": "B"}'), latency_ms=10,
            ), None

        if model_res.name == "ds-pro":
            await asyncio.sleep(0.01)
        elif model_res.name == "glm5":
            await asyncio.sleep(0.02)
        else:  # kimi, minimax-m3 never return within the test
            await asyncio.sleep(10)
        return CandidateResult(
            candidate_name=model_res.name, real_model=model_res.model,
            response=FakeResponse(f"answer-from-{model_res.name}"), latency_ms=10,
        ), None

    monkeypatch.setattr("app.fusion.call_with_dynamic_key", fake_call)

    strategy = VirtualModelStrategy(
        mode="fusion",
        hard_timeout_seconds=5,
        per_call_timeout_seconds=5,
        fusion_tier="large",
        fusion_min_valid=2,
        fusion_retry_interval_seconds=60,
        fusion_judge_interval_seconds=1.0,
    )

    winner, all_results = await fusion_completion(make_request(), strategy)

    assert winner is not None
    # candidates = [ds-pro (A), glm5 (B)] -> judge picked "B" -> glm5
    assert winner.candidate_name == "glm5"
    assert "cancel" not in winner.fusion_judge_path
