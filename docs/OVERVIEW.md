# nim-hedge-gateway Overview

## Project Purpose
NVIDIA NIM (NVIDIA Inference Microservices) offers a wide range of powerful, free models. However, the free tier often suffers from instability, tail latency, and rate limits.

The `nim-hedge-gateway` is a local OpenAI-compatible proxy that creates a high-availability layer through **Dynamic Orchestration**.

## Core Refined Philosophy

### 1. Resources vs. Strategy
The gateway separates **what** models you have (Tiers) from **how** you want to use them (Strategies).
- **Tiers**: Groups of physical model backends (Large, Medium, Small).
- **Virtual Models**: Defined strategies that map out a timeline of which Tiers to call and when.

### 2. Just-In-Time Planning
Instead of a static list, the gateway generates a unique **Execution Plan** for every request based on:
- The defined phases for the virtual model.
- The real-time health score of models within each tier.

### 3. Progressive Hedging
The gateway can aggressively hedge within a tier (e.g., trying Large models every 45s) and then seamlessly transition to falling back to other tiers if a result isn't found within a certain window.

### 4. Zero-Cooldown Persistence
To ensure maximum availability, the gateway keeps trying even during 502/504 errors, only applying hard cooldowns for 429 Rate Limits.
