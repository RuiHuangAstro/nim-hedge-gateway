---
title: NIM Hedging Strategy
created: 2026-05-08
updated: 2026-05-31
type: concept
tags: [hedging, fallback, strategy, orchestration, routing]
sources: [../../docs/MODELS_AND_HEDGING.md, ../../docs/FALLBACK_LOGIC.md, ../../app/hedger.py, ../../app/router.py]
confidence: high
---

# NIM Hedging Strategy

NIM Hedge Gateway 的对冲策略设计，通过动态编排和渐进式对冲最大化可用性，同时最小化成本。

## 核心设计哲学

### 1. 资源 vs 策略分离

网关将**你有什么模型**（Tiers）与**你想如何使用它们**（Strategies）分离。

- **Tiers**: 物理模型后端的分组（Large、Medium、Small）
- **Virtual Models**: 定义策略，映射出哪个层级在何时调用的时间线

**优势**:
- 灵活配置：同一组模型可以支持多种策略
- 易于维护：添加新模型只需更新 tiers，不影响策略
- 清晰抽象：客户端看到的是虚拟模型，而非底层实现

### 2. 即时规划 (Just-In-Time Planning)

网关不为每个请求使用静态列表，而是基于以下因素生成唯一的**执行计划**：

- 虚拟模型定义的阶段
- 每个层级内模型的实时健康评分

**优势**:
- 自适应：根据实时性能动态调整
- 高效：避免为注定失败的模型分配槽位
- 公平：所有健康模型都有机会

### 3. 渐进式对冲 (Progressive Hedging)

网关可以在一个层级内激进对冲（如每 45s 尝试一次 Large 模型），然后如果在一定窗口内未找到结果，无缝回退到其他层级。

**优势**:
- 平衡成本和可靠性：优先使用高质量模型，必要时回退
- 减少尾部延迟：激进对冲防止长时间等待
- 成本控制：仅在必要时使用昂贵的付费模型

### 4. 持续尝试和 429 Delay

为确保最大可用性，网关即使在 502/504 错误期间也继续尝试。429 Rate Limit 不再触发硬冷却；当前策略是记录该 key 的近期 429，并在后续使用同一 key 前加入 2^N 秒的 pre-request delay。

**优势**:
- 最大可用性：不因临时服务器错误而放弃
- 快速恢复：上游恢复时立即响应
- 平滑限流：429 压力通过 per-key delay 自调节，不把 key 移出轮转

## 层级组织 (Tier Organization)

### Large 层级
- **用途**: 高参数推理模型
- **模型**: Kimi 2.6、GLM 5.1、DeepSeek Pro
- **特点**: 最高质量，最慢响应

### Medium 层级
- **用途**: 平衡性能
- **模型**: Qwen 397b、GLM 4.7、DeepSeek Flash
- **特点**: 质量和速度的平衡

### Small 层级
- **用途**: 快速响应
- **模型**: Qwen 122b、Nemotron、GPT-OSS
- **特点**: 最快响应，较低质量

### Vision 层级
- **用途**: 多模态专家
- **模型**:
  - `llama-90b-vision`: Meta 的重量级，峰值推理
  - `qwen2-72b-vision`: OCR、图表和文档分析的王者
  - `kimi-vision`: 强大的图表和指令遵循
  - `vila-40b`: NVIDIA 优化的原生 VLM
  - `phi-3.5-vision`: 轻量级且非常快
  - `cosmos-reasoner`: NVIDIA 最新的物理世界逻辑

## 动态执行逻辑

网关自动处理阶段内的重新排序：

1. 检索阶段 `tier` 中的所有模型
2. 按其当前健康评分对它们进行排名
3. 以轮询方式将它们分配到时间线槽位（最佳 → 第二佳 → 第三佳 → 最佳...）

**示例**（基于实际 config.yaml，`nim-large` 策略）:

```
Phase 1 (Large Tier, interval=40s):
  T+0s:   ds-pro (score=0.85)
  T+40s:  glm5 (score=0.82)
  T+80s:  kimi (score=0.80)
  T+120s: ds-pro (round-robin back to best)
  T+160s: glm5
  ...
```

## Timeline 流程图：nim-large 实战案例

基于真实 `config.yaml` 配置（`nim-large` 策略），展示一个完整的请求生命周期：

```
        时间轴                              事件
        ──────                             ──────

        T+0s  ┌─────────────────────┐
              │  ds-pro  [请求中]   │  ← 第1槽: Large 层级最佳模型
              └──────────┬──────────┘
                         │ (等待中...)
                         │
        T+40s ┌──────────┴──────────┐
              │  glm5  [请求中]     │  ← 第2槽: 40s间隔, 第二佳模型
              └──────────┬──────────┘
                         │ (ds-pro 仍在等待...)
                         │
        T+80s ┌──────────┴──────────┐
              │  kimi  [请求中]     │  ← 第3槽: 80s间隔, 第三佳模型
              └──────────┬──────────┘
                         │
        T+82s ┌──────────┴──────────┐
              │  ds-pro  [✗ 429]    │  ← ds-pro 返回 429 (速率限制)
              └─────────────────────┘
                         │
        T+85s ┌─────────────────────┐
              │  glm5  [✗ 502]      │  ← glm5 返回 502 (上游错误)
              └─────────────────────┘
                         │
        T+98s ┌─────────────────────┐
              │  kimi  [✓ WINNER]   │  ★ 首个有效响应！取消其余任务
              └─────────────────────┘

        T+120s  ds-pro  [已取消]     ← 第4槽被取消 (winner 已找到)
        T+160s  glm5  [已取消]       ← 第5槽被取消

        ─────────────────────────────────────────────────
        结果: x-hedge-winner=kimi, x-hedge-degraded=false
        耗时: 98s (kimi 获胜)
```

**关键信息**:
- **ds-pro** 和 **glm5** 先出发但都失败了 (429/502)，不影响管道
- **kimi** 在 T+80s 才出发，但 T+98s 就返回了有效结果 (仅 18s 响应)
- 第4、5槽被取消，不浪费资源
- 502/504/timeout 只影响候选者健康评分；429 会增加对应 key 的后续 pre-request delay

### 多阶段回退场景

当 Phase 1 所有候选者都失败时的完整回退：

```
        时间轴                              事件
        ──────                             ──────

  ┌── Phase 1: Large Tier (0-360s, interval=40s) ──────────────────────────┐
  │                                                                          │
  │  T+0s   ds-pro  [✗ 429]                                                 │
  │  T+40s  glm5    [✗ 502]                                                 │
  │  T+80s  kimi    [✗ 504]                                                 │
  │  T+120s ds-pro  [✗ 429]                                                 │
  │  T+160s glm5    [✗ 502]                                                 │
  │  T+200s kimi    [✗ timeout]                                             │
  │  T+240s ds-pro  [✗ 429]                                                 │
  │  T+280s glm5    [✗ 502]                                                 │
  │  T+320s kimi    [✗ 504]                                                 │
  │                                                                          │
  │  → Phase 1 全部失败 (9 次尝试, 0 成功)                                   │
  └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌── Phase 2: Medium Tier (360-900s, interval=50s) ────────────────────────┐
  │                                                                          │
  │  T+360s qwen-397  [✗ timeout]                                           │
  │  T+410s glm4-7    [✗ 502]                                               │
  │  T+460s ds-flash  [✓ WINNER]  ★                                         │
  │                                                                          │
  │  → Phase 2 获胜！取消其余任务                                             │
  └──────────────────────────────────────────────────────────────────────────┘

        ─────────────────────────────────────────────────
        结果: x-hedge-winner=ds-flash, x-hedge-degraded=true
        耗时: ~460s (Phase 2 获胜)
        说明: 响应标记为 degraded，因为来自非主层级
```

### Key 压力和 Pre-request Delay 场景

当 NIM API 密钥持续遇到 429 时：

```
        时间轴                              事件
        ──────                             ──────

  ┌── Phase 1: Large Tier ───────────────────────────────────────────────────┐
  │                                                                          │
  │  T+0s   ds-pro  [✗ 429]  → NVIDIA_API_KEY_1 记录一次 429                │
  │  T+40s  glm5    [✗ 429]  → NVIDIA_API_KEY_2 记录一次 429                │
  │  T+80s  kimi    [✗ 429]  → NVIDIA_API_KEY_3 记录一次 429                │
  │  T+120s ds-pro  [✗ 429]  → NVIDIA_API_KEY_1 记录第二次 429              │
  │                                                                          │
  │  → key 不进入硬冷却；后续使用该 key 前根据近期 429 数 sleep 2^N 秒        │
  └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌── Router / Health Control ───────────────────────────────────────────────┐
  │                                                                          │
  │  router 继续 round-robin 选择可用 key                                     │
  │  如果 token bucket 为空：可等待 queue_when_limited 或返回 synthetic 429   │
  │                                                                          │
  │  paid_fallback 当前已删除，不再作为所有 key 受限时的自动出口              │
  └──────────────────────────────────────────────────────────────────────────┘
```

## 降级检测 (Degradation Detection)

网关通过 `x-hedge-degraded` 头标记降级响应：

- 如果获胜候选者来自阶段，且该阶段的 `tier` 与第一个阶段的 `tier` 不同，则网关将响应标记为降级
- 这允许客户端知道他们收到的是高质量答案还是回退答案

**示例**:
```
# Primary tier response
x-hedge-winner: kimi-k2.6
x-hedge-degraded: false

# Fallback tier response
x-hedge-winner: qwen-122b
x-hedge-degraded: true
```

## Key Rate Control

当前编排器没有硬并发上限。`app/router.py` 对共享 key 池做 round-robin 选择，并可用 token bucket 主动限制每个 key 的 RPM。

**优势**:
- 避免所有请求总是打到第一个 key
- 可以用 `rpm_limit_per_api` 主动压低上游 429
- 在所有 token bucket 为空时可以选择排队等待或 synthetic 429

**配置**:
```yaml
server:
  rpm_limit_per_api: 0
  burst_per_api: 0
  queue_when_limited: false
  max_queue_seconds: 20.0
  allow_best_effort_when_all_limited: false
```

## 阶段执行流程

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PHASE EXECUTION FLOW                                      │
└─────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────┐
                    │  PHASE START    │
                    └────────┬────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  GET TIER MODELS            │
              │  (e.g., large tier)          │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  RANK BY HEALTH SCORE        │
              │  (Best → Second → Third)      │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  CALCULATE SLOTS             │
              │  (duration / interval)      │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  GENERATE EXECUTION PLAN     │
              │  (Round-robin assignment)     │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  LAUNCH TASKS STAGGERED      │
              │  (T+0, T+45s, T+90s, ...)    │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  WAIT FOR FIRST VALID       │
              │  RESPONSE                   │
              └──────────────┬───────────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                    ▼                 ▼
          ┌──────────────┐   ┌──────────────┐
          │ WINNER FOUND │   │ TIMEOUT /    │
          │              │   │ ALL FAILED   │
          └──────┬───────┘   └──────┬───────┘
                 │                 │
                 ▼                 │
    ┌──────────────────────┐      │
    │  CANCEL REMAINING    │      │
    │  TASKS               │      │
    └──────────┬───────────┘      │
               │                  │
               │                  │
               └──────────────────┤
                                  │
                                  ▼
                    ┌──────────────────────┐
                    │  RETURN WINNER OR    │
                    │  CONTINUE TO NEXT    │
                    │  PHASE               │
                    └──────────────────────┘
```

## 高级调优

### 激进对冲
降低 `interval_seconds`（如 30s）以对抗尾部延迟：
```yaml
virtual_models:
  nim-large:
    phases:
      - tier: "large"
        interval_seconds: 30  # More aggressive
```

### 严格逻辑
创建一个虚拟模型，只有一个阶段在整个持续时间内保持在 `large` 层级：
```yaml
virtual_models:
  nim-large-strict:
    hard_timeout_seconds: 1500
    phases:
      - tier: "large"
        start_seconds: 0
        end_seconds: 1500
        interval_seconds: 45
```

### 成本优化
延长 `interval_seconds` 以减少备份请求：
```yaml
virtual_models:
  nim-large-cost-optimized:
    phases:
      - tier: "large"
        interval_seconds: 90  # Less aggressive
```

## 配置示例

```yaml
server:
  api_key_envs:
    - "NVIDIA_API_KEY_1"
    - "NVIDIA_API_KEY_2"

tiers:
  large:
    - name: "kimi"
      model: "moonshotai/kimi-k2.6"
    - name: "glm-5.1"
      model: "z-ai/glm-5.1"
  medium:
    - name: "qwen-397b"
      model: "qwen/qwen3.5-397b-a17b"
  small:
    - name: "qwen-122b"
      model: "qwen/qwen3.5-122b-a10b"

virtual_models:
  nim-large:
    hard_timeout_seconds: 1500
    phases:
      - tier: "large"
        start_seconds: 0
        end_seconds: 360
        interval_seconds: 45
      - tier: "medium"
        start_seconds: 360
        end_seconds: 900
        interval_seconds: 60
      - tier: "small"
        start_seconds: 900
        end_seconds: 1500
        interval_seconds: 90
```

## 关键设计原则

1. **渐进式对冲**: 层级内激进对冲，无缝回退到其他层级
2. **持续尝试**: 502/504 错误期间继续尝试；429 通过 per-key pre-request delay 调节
3. **即时规划**: 基于实时健康为每个请求生成唯一执行计划
4. **首个有效响应获胜**: 找到获胜者时立即取消剩余任务
5. **基于健康的排名**: 使用实时评分优先考虑健康模型

## 相关页面

- [[nim-hedge-gateway]] — NIM Hedge Gateway 项目实体
- [[nim-health-cooldown-system]] — NIM 健康评分和速率控制
- [[how-to-configure-nim-proxy]] — NIM Proxy 配置指南
