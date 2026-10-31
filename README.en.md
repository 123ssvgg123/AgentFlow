# FoodAgent · AI-Powered Restaurant Agent System

> A restaurant-intelligence assistant built on **LangGraph StateGraph + Orchestrator**.
> 4 specialized Agents, 33 nodes, 15 layered tools, 9 restaurant intents, a three-tier memory
> architecture — covering the full **order → kitchen → review → food-safety Q&A** business loop.
> The knowledge layer is self-contained (built-in regulation / menu / nutrition data) and
> pluggable: swapping in an external RAG or vector store only touches `tools/mcp_tools.py`.

---

## Overview

| Dimension | Detail |
|-----------|--------|
| Architecture | LangGraph StateGraph + Orchestrator (intent routing / direct dispatch / multi-Agent pipeline) |
| Agents | **OrderAgent(10 nodes)** · **KitchenAgent(7 nodes)** · **ReviewAgent(7 nodes)** · **SafetyAgent(9 nodes)** = **33 nodes** |
| Tools | **15** = LLM tools 4 + rule engine 4 + MCP tools 4 + external API wrappers 3 |
| Intents | **9**: dish recommend / smart order / allergen query / kitchen dispatch / inventory check / review analysis / improvement advice / food-safety Q&A / nutrition Q&A |
| Memory | **Three tiers**: Checkpointer (short-term) + PostgreSQL (long-term) + industry-specific (store prefs / customer profiles / ingredient usage) |
| Isolation | Dual-layer by **tenant_id + user_id** |
| Compliance | Allergen food-safety audit trail (retained **3 years**), Human-in-the-Loop via `interrupt` |
| Interface | FastAPI + SSE streaming + JWT, **18 RESTful endpoints** |

---

## Quick Start

```bash
cd D:\heima\FoodAgent
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # then set LLM_API_KEY=sk-xxxxxx
python main.py --check          # environment self-check
python main.py                  # start at http://127.0.0.1:8090
```

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:8090/ | Chat UI |
| http://127.0.0.1:8090/docs | Swagger API docs |
| http://127.0.0.1:8090/api/health | Health check |

**Demo accounts** (password `123456`): `waiter_01`, `kitchen_01`, `manager_01`.

See [README.md](README.md) for full details, [RUN.md](RUN.md) for deployment.

---

## Core Design Principles

1. **Allergen detection is never delegated to the LLM** — it runs in the rule engine for exact set-intersection matching, eliminating hallucination-driven food-safety incidents.
2. **Works with or without heavier dependencies** — LangGraph ↔ sequential executor, PostgreSQL ↔ JSON files, LLM tools ↔ rule fallbacks, external knowledge source ↔ built-in knowledge base, POS/KDS ↔ mock data.
3. **Three-level degradation** — retry (exponential backoff) → switch data source → friendly fallback, all traced via the `degraded_tools` response field.

---

## Directory Layout

```
base/        config / logger / exceptions
tools/       15 tools (llm / rule / mcp / external) + registry
prompts/     all prompt templates
memory/      schemas (DDL) / store / checkpointer
agents/      state / base_agent / 4 Agents (order, kitchen, review, safety) / orchestrator
api/         app / routes (18 endpoints) / models / auth
static/      single-page chat front-end (SSE typewriter)
docs/deploy/ schema.sql + docker-compose.yml
```
