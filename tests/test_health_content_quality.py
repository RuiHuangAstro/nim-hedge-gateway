"""Tests for content-quality penalty in candidate ranking.

When a winner's response had harmony markers but the parser had to
infer or completely failed to recover tool calls, the candidate's score
should reflect that — the upstream effectively returned a degraded or
unusable payload even though HTTP 200 was returned.
"""
import pytest

from app.config import config
from app.health import HealthStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config.health, "persistence_file", str(tmp_path / "health.json"))
    return HealthStore()


def _record_clean(store, vm, name, model_path, latency=1000, n=1):
    for _ in range(n):
        store.get_candidate_health(vm, name, model_path)
        store.mark_success(vm, name, latency)


def test_clean_quality_is_no_op(store):
    _record_clean(store, "vm", "kimi", "k/x")
    score_before = store.candidates["vm/kimi"].score
    store.mark_content_quality("vm", "kimi", "clean")
    assert store.candidates["vm/kimi"].score == score_before
    assert store.candidates["vm/kimi"].recent_events[-1].event_type == "success"


def test_inferred_quality_lightly_penalizes(store):
    _record_clean(store, "vm", "kimi", "k/x")
    score_clean = store.candidates["vm/kimi"].score
    store.mark_content_quality("vm", "kimi", "inferred")
    h = store.candidates["vm/kimi"]
    assert h.recent_events[-1].event_type == "success_inferred"
    # Still counts as a usable response, so success rate stays the same;
    # the only delta is the content_penalty term (0.1/total = 0.1).
    assert h.score == pytest.approx(score_clean - 0.1)


def test_unparsed_quality_treated_as_non_success(store):
    # 5 clean successes + 1 unparsed → success rate drops, penalty applied.
    _record_clean(store, "vm", "kimi", "k/x", n=5)
    store.mark_success("vm", "kimi", 1000)
    score_before_downgrade = store.candidates["vm/kimi"].score
    store.mark_content_quality("vm", "kimi", "unparsed")
    h = store.candidates["vm/kimi"]
    assert h.recent_events[-1].event_type == "success_unparsed"
    # Score must drop noticeably: success rate falls AND content_penalty hits.
    assert h.score < score_before_downgrade - 0.05


def test_kimi_with_unparsed_ranks_below_clean_glm(store):
    # Realistic scenario: glm always clean, kimi fast but ~20% unparsed.
    # Ranking should prefer glm for ranking despite glm being slower.
    for _ in range(10):
        store.get_candidate_health("vm", "glm", "g/x")
        store.mark_success("vm", "glm", 30000)  # slow but clean
    for _ in range(10):
        store.get_candidate_health("vm", "kimi", "k/x")
        store.mark_success("vm", "kimi", 5000)  # fast
    # 2 of kimi's 10 successes turn out to be unparsed garbage.
    for _ in range(2):
        store.mark_content_quality("vm", "kimi", "unparsed")
        # mark_content_quality only downgrades the LAST event; to downgrade
        # multiple, we have to re-record + downgrade in pairs.
        store.mark_success("vm", "kimi", 5000)
        store.mark_content_quality("vm", "kimi", "unparsed")

    glm_score = store.candidates["vm/glm"].score
    kimi_score = store.candidates["vm/kimi"].score
    assert glm_score > kimi_score, (
        f"glm ({glm_score:.3f}) should outrank kimi ({kimi_score:.3f}) "
        "once kimi accumulates unparsed responses"
    )


def test_does_not_clobber_non_success_event(store):
    # If the last event is a failure (e.g. timeout), mark_content_quality
    # should be a no-op — we shouldn't be downgrading a failure into a
    # different kind of failure.
    store.get_candidate_health("vm", "kimi", "k/x")
    store.mark_failure("vm", "kimi", "timeout", 30000, "timed out")
    store.mark_content_quality("vm", "kimi", "unparsed")
    last = store.candidates["vm/kimi"].recent_events[-1]
    assert last.event_type == "timeout"


def test_unknown_quality_value_is_no_op(store):
    _record_clean(store, "vm", "kimi", "k/x")
    store.mark_content_quality("vm", "kimi", "garbage-value")
    assert store.candidates["vm/kimi"].recent_events[-1].event_type == "success"
