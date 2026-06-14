# Strategies and Tiers

## Tier Organization
- **Large**: High-parameter reasoning models (Kimi 2.6, GLM 5.1, DeepSeek Pro).
- **Medium**: Balanced performance (Qwen 397b, GLM 4.7, DeepSeek Flash).
- **Small**: Fast response (Qwen 122b, Nemotron, GPT-OSS).
- **Vision**: Multi-modal experts.
  - `llama-90b-vision`: Meta's heavyweight, peak reasoning.
  - `qwen2-72b-vision`: King of OCR, charts, and document analysis.
  - `kimi-vision`: Robust chart and instruction following.
  - `vila-40b`: NVIDIA optimized native VLM.
  - `phi-3.5-vision`: Lightweight and very fast.
  - `cosmos-reasoner`: NVIDIA's latest for physical world logic.

## Dynamic Execution Logic
The gateway automatically handles re-ordering within a phase.
1. It retrieves all models in the phase's `tier`.
2. It ranks them by their current Health Score.
3. It assigns them to the timeline slots in a round-robin fashion (Best -> Second Best -> Third Best -> Best...).

## Degradation Header (`x-hedge-degraded`)
- If the winning candidate comes from a phase where the `tier` is different from the very first phase's tier, the gateway flags the response as degraded.
- This allows clients to know if they received a high-quality answer or a fallback answer.

## Concurrency Protection
If an API key is already serving 5 requests (the default limit), the orchestrator will **skip** that candidate for the current slot and move to the next available one in the ranking. This prevents a single slow model from blocking the entire pipeline.
