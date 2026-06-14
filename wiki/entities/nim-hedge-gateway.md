---
title: NIM Hedge Gateway
created: 2026-05-08
updated: 2026-05-31
type: entity
tags: [hedging, fallback, openai-compatible, proxy, routing]
sources: [../../README.md, ../../docs/, ../../app/router.py, ../../app/health.py]
confidence: high
---

# NIM Hedge Gateway

NIM Hedge Gateway 是一个本地 LLM 对冲网关，旨在提高 NVIDIA NIM 模型（及其他 OpenAI 兼容提供商）的可靠性和尾部延迟。它作为一个 OpenAI 兼容的代理，位于代理客户端（如 OpenClaw、Hermes 或 OpenCode）和多个 LLM 后端之间。

## 项目概览

- **目标**: 通过动态编排创建高可用层
- **协议**: OpenAI 兼容 API (`/v1/models`, `/v1/chat/completions`)
- **核心特性**: 延迟对冲、虚拟模型池、健康评分、per-key rate control、LiteLLM 集成
- **GitHub**: `https://github.com/RuiHuangAstro/nim-hedge-gateway`
- **本机 checkout**: `/u/Lang3/program/playground/NIM_proxy/`

## 核心特性

### 1. OpenAI 兼容 API
- 实现 `/v1/models` 和 `/v1/chat/completions` 端点
- 完全兼容 OpenAI API 格式
- 支持工具调用 (tool calls)

### 2. 延迟对冲 (Delayed Hedging)
- 智能启动备份模型请求（如果主请求慢）
- 阶段内激进对冲（如每 45s 尝试一次 Large 模型）
- 无缝回退到其他层级（如果在一定窗口内未找到结果）

### 3. 虚拟模型池 (Virtual Model Pools)
- 将虚拟名称（如 `nim-small`、`nim-medium`、`nim-large`）映射到多个真实模型和 API 密钥
- 分离资源（Tiers）和策略（Virtual Models）
- 支持自定义虚拟模型配置

### 4. 首个有效响应获胜 (First-Valid-Response Wins)
- 第一个返回有效、非截断响应的候选者获胜
- 其他候选者被取消
- 附加自定义头（`x-hedge-winner`、`x-hedge-degraded`）

### 5. 健康和速率控制 (Health & Rate Control)
- 滚动健康评分（成功、超时、服务器错误、P95 延迟、内容质量）
- per-key token bucket 可选限制 RPM
- 429 后使用 per-key 2^N pre-request delay；不再使用硬冷却
- `/v1/hedge/key_stats` 可查看 key 活跃数、token bucket、429 和 delay

### 6. LiteLLM 集成
- 利用 LiteLLM SDK 支持广泛的提供商
- 支持 NVIDIA NIM、DeepSeek、OpenAI 等

### 7. 结构化日志
- 请求元数据记录到 JSONL 以便分析
- 响应归档（validation_failed、harmony_unparsed）

### 8. 简单认证
- 可选的 Bearer token 验证以实现本地安全

## 项目结构

```
~/program/playground/NIM_proxy/
├── app/
│   ├── main.py              # FastAPI 应用入口点
│   ├── hedger.py            # 核心对冲和编排逻辑
│   ├── providers.py         # LiteLLM 包装器，用于调用后端模型
│   ├── validators.py        # 响应验证逻辑（确保内容和有效工具调用）
│   ├── health.py            # 健康评分和 per-key 429 delay 管理
│   ├── router.py            # API key 选择、token bucket 和 key stats
│   ├── logging_utils.py     # JSONL 请求记录器
│   ├── config.py            # 基于 Pydantic 的配置加载器
│   └── models.py            # Pydantic 模型定义
├── docs/
│   ├── ARCHITECTURE.md      # 架构和技术设计
│   ├── CONFIGURATION.md     # 配置指南
│   ├── DEVELOPMENT.md       # 开发指南
│   ├── MODELS_AND_HEDGING.md # 策略和层级
│   ├── OPERATIONS.md        # 运维指南
│   ├── OVERVIEW.md          # 项目概览
│   ├── README.md            # 文档 README
│   └── FALLBACK_LOGIC.md    # 回退逻辑设计
├── wiki/                    # 项目 wiki
│   ├── entities/            # 实体页面
│   ├── concepts/            # 概念页面
│   ├── queries/             # 查询/操作指南页面
│   ├── SCHEMA.md            # Wiki 模式
│   ├── index.md             # Wiki 索引
│   └── log.md               # Wiki 日志
├── tests/                   # 测试
├── config.yaml              # 配置文件
├── .env                     # 环境变量
├── requirements.txt         # Python 依赖
└── README.md                # 项目说明
```

## 核心组件

### 1. 策略驱动编排器 (`app/hedger.py`)
这是网关的"大脑"。它不再读取固定列表。
- **执行计划生成器**: 当请求到达时，查看请求的 `virtual_model` 的 `phases`
- **动态槽分配**: 根据其 `interval_seconds` 计算每个阶段适合多少个"槽"（请求）
- **层级内排名**: 对于每个槽，使用实时评分从指定层级中选择性能最佳的模型

### 2. 资源注册表 (`app/config.py`)
- **Tiers**: 原始模型端点的注册表
- **Virtual Models**: 定义请求"生命周期"的配置对象（Phases）

### 3. 滚动健康评分 (`app/health.py`)
- 跟踪成功、超时、服务器错误和 P95 延迟
- 按 `(virtual_model, candidate_name)` 计算评分
- **贝叶斯平滑**: 防止新模型或很少使用的模型的排名波动

### 4. Key 路由器 (`app/router.py`)
- 对 `server.api_key_envs` 做 round-robin 选择
- 可选 per-key token bucket: `rpm_limit_per_api` / `burst_per_api`
- 当所有 key 没有 token 时，可选择排队等待或返回 synthetic 429
- 429 压力交给 `health.pre_request_delay`，不会把 key 移出轮转

## 请求流程

1. 客户端 POST 到 `/v1/chat/completions` 请求 `nim-large`
2. 网关查找 `nim-large` 策略
3. 编排器生成 1500s 时间线：
   - T+0 到 T+360: 每 45s 使用 `large` 层级的模型
   - T+360 到 T+900: 每 60s 使用 `medium` 层级的模型
   - T+900 到 T+1500: 每 90s 使用 `small` 层级的模型
4. 任务在其开始时间到达时启动
5. 第一个有效响应获胜；其他被取消
6. 附加自定义头（`x-hedge-winner`、`x-hedge-degraded`）

## 安装和使用

### 安装

```bash
cd ~/program/playground/NIM_proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.yaml config.yaml
```

### 配置

编辑 `.env` 包含实际的 API 密钥：
```bash
NVIDIA_API_KEY_1=nvapi-...
```

编辑 `config.yaml` 自定义模型池和对冲延迟。

### 启动服务器

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 测试服务器

检查可用模型：
```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer ***"
```

发送聊天完成请求：
```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nim-small",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "stream": false
  }'
```

## 客户端配置 (OpenClaw / Hermes)

- **Base URL**: `http://127.0.0.1:8000/v1`
- **API Key**: `local-test`（或在 `config.yaml` 中设置的任何值）
- **Model**: `nim-small`、`nim-medium` 或 `nim-large`

## 限制 (MVP)

- **不支持流式传输**: 当前版本仅支持非流式请求（`stream=false`）。带有 `stream=true` 的请求将返回 400/501 错误
- **运行时窗口**: per-key 429 delay 窗口在内存中，服务器重启会清空；候选者健康评分会写入 `health_state.json`

## 运行测试

```bash
pytest
```

## 相关页面

- [[nim-hedging-strategy]] — NIM 对冲策略概念
- [[nim-health-cooldown-system]] — NIM 健康冷却系统
- [[how-to-configure-nim-proxy]] — NIM Proxy 配置指南
