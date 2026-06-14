# Architecture and Technical Design (Refactored)

## Core Components

### 1. Strategy-Driven Orchestrator (`app/hedger.py`)
This is the "Brain" of the gateway. It no longer reads fixed lists.
- **Execution Plan Generator**: When a request arrives, it looks at the `phases` of the requested `virtual_model`.
- **Dynamic Slot Allocation**: It calculates how many "slots" (requests) fit into each phase based on its `interval_seconds`.
- **Tier-Internal Ranking**: For each slot, it picks the best-performing model from the designated Tier, using real-time scoring.

### 2. Resource Registry (`app/config.py`)
- **Tiers**: A registry of raw model endpoints.
- **Virtual Models**: Configuration objects that define the "Life Cycle" of a request (Phases).

### 3. Rolling Health Score (`app/health.py`)
- Tracks success, timeout, server errors, and P95 latency.
- Calculates scores per `(virtual_model, candidate_name)`.
- **Bayesian Smoothing**: Prevents volatility in rankings for new or rarely used models.

## Request Flow
1. Client POSTs to `/v1/chat/completions` for `nim-large`.
2. Gateway looks up the `nim-large` strategy.
3. Orchestrator generates a 1500s timeline:
   - T+0 to T+360: Slots every 45s using models from the `large` tier.
   - T+360 to T+900: Slots every 60s using models from the `medium` tier.
   - T+900 to T+1500: Slots every 90s using models from the `small` tier.
4. Tasks are launched as their start times arrive.
5. The first valid response wins; others are canceled.
6. Custom headers are attached (`x-hedge-winner`, `x-hedge-degraded`).
