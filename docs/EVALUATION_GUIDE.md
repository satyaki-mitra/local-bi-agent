# Evaluation Guide

This document covers everything needed to run, interpret, and extend the LocalGenBI-Agent evaluation harness: what is measured, why each metric was chosen, how to run every test configuration, how to read the output, and how to add new test cases.

---

## Table of Contents

- [Why Evaluation Matters for This Project](#why-evaluation-matters-for-this-project)
- [What Is Measured](#what-is-measured)
- [Golden Dataset Structure](#golden-dataset-structure)
- [Dataset Coverage](#dataset-coverage)
- [Prerequisites](#prerequisites)
- [Running the Evaluator — All Commands](#running-the-evaluator--all-commands)
- [Reading the Output](#reading-the-output)
- [Score Interpretation](#score-interpretation)
- [Known Measurement Gaps](#known-measurement-gaps)
- [Extending the Dataset](#extending-the-dataset)
- [Running in CI](#running-in-ci)
- [Evaluation Architecture Reference](#evaluation-architecture-reference)

---

## Why Evaluation Matters for This Project

LocalGenBI-Agent uses a locally-hosted 8B reasoning model to generate SQL. Unlike a deterministic rules engine, the LLM output is stochastic and will vary across runs, model versions, temperature settings, and prompt changes. Two classes of failure are especially important to track:

**SQL routing failures:** The supervisor sends a query to the wrong database. The agent then generates SQL against the wrong schema, the query may accidentally succeed (returning nonsense data), and the analyst confidently answers with incorrect information. This failure mode is silent — no error is raised.

**Cross-domain confusion:** Many business queries use terms that are genuinely ambiguous across the four domains ("revenue", "cost", "performance", "users"). A word-frequency-based router will systematically fail on these. The evaluation dataset includes 20 cases designed specifically to surface this.

Without a structured evaluation, it is impossible to know whether a prompt change that improved one domain broke routing on another, or whether upgrading the Ollama model version improved SQL accuracy or degraded it.

---

## What Is Measured

The harness measures four things independently, so you can see exactly where failures occur.

### 1. Routing Accuracy

**What it tests:** Does the supervisor agent pick the correct database for each query?

**How it works:** After the agent responds, the evaluator inspects the `sql_executed` field in the API response. It looks for table-name fingerprints — known table names from each domain — to infer which database was actually queried. This inferred domain is compared to `expected_database` in the golden dataset.

For `cross_db_routing` cases, the full set of matched domains is checked — a case is correct if `expected_database` appears anywhere in the matched set.

**Why this approach:** The FastAPI response does not expose the supervisor's routing decision directly. Using SQL table names as fingerprints is a reliable proxy because the four domains have completely non-overlapping table names (`patient_history` can only be health; `payment_failures` can only be finance; `daily_steps` can only be IoT; etc.).

**Reported as:** `overall_routing_accuracy` (all cases) and `cross_db_routing_accuracy` (the 20 cross-domain cases only). `routing.all_cases` contains the full per-case result. `routing.routing_failures` contains only the failed and errored cases.

### 2. SQL Keyword Coverage

**What it tests:** Does the generated SQL contain the tables and SQL clauses that a correct answer to this question requires?

**How it works:** Each golden case has an `expected_sql_keywords` list containing table names, column names, and SQL keywords (e.g. `["claims", "patient_history", "JOIN", "SUM", "claim_amount"]`). The evaluator checks what fraction of these appear (case-insensitive) in the `sql_executed` string.

**Why this matters:** Routing accuracy tells you the right database was chosen; SQL coverage tells you the SQL was structurally sensible.

**Known limitation:** Coverage measures structure, not semantic correctness. Semantically equivalent SQL that uses different syntax will miss keyword matches even when functionally correct.

**Reported as:** per-case `coverage` ratio (0.0–1.0) in `sql.cases` and `avg_sql_coverage` overall. Cases with coverage < 0.5 are listed separately as `sql.low_coverage_cases`.

### 3. Answer Relevancy (DeepEval)

**What it tests:** Is the natural-language answer the analyst agent generated actually relevant to the original question?

**How it works:** Uses DeepEval's `AnswerRelevancyMetric`. The metric calls `metric.measure(test_case)` directly on the evaluator model (`DEEPEVAL_EVALUATOR_MODEL`, default `mistral:7b`). A score ≥ 0.7 passes.

**Reported as:** `avg_relevancy`, per-case scores in `quality.all_cases`, and a list of below-threshold cases in `quality.failed_cases`.

### 4. Faithfulness (DeepEval)

**What it tests:** Does the answer stay consistent with the data that was returned from the database?

**How it works:** Uses DeepEval's `FaithfulnessMetric`. The retrieval context passed to DeepEval is the raw SQL that was executed. A score ≥ 0.7 passes.

**Reported as:** `avg_faithfulness`, per-case scores in `quality.all_cases`, and included in `quality.failed_cases`.

---

## Golden Dataset Structure

File: `evaluation/golden_dataset.json`

```json
{
  "_meta": {
    "version": "1.1",
    "total_cases": 108,
    "breakdown": {
      "health": 22,
      "finance": 22,
      "sales": 22,
      "iot": 22,
      "cross_db_routing": 20
    }
  },
  "test_cases": [
    {
      "id": "H-001",
      "query_type": "health",
      "difficulty": "easy",
      "input": "How many patients are in the database?",
      "expected_database": "health",
      "expected_sql_keywords": ["patient_history", "COUNT"],
      "expected_answer_contains": ["patients", "100"],
      "context": "patient_history table contains 100 rows inserted by the demo data generator",
      "tags": ["count", "aggregation"]
    }
  ]
}
```

### Field Reference

| Field | Type | Purpose |
|---|---|---|
| `id` | `string` | Unique identifier. Format: `{DOMAIN_PREFIX}-{NNN}` |
| `query_type` | `string` | One of: `health`, `finance`, `sales`, `iot`, `cross_db_routing`, `conversational` |
| `difficulty` | `string` | One of: `easy`, `medium`, `hard` — informational only, not used in scoring |
| `input` | `string` | The exact natural-language query sent to the agent |
| `expected_database` | `string` | The database the supervisor must route to |
| `expected_sql_keywords` | `string[]` | Table names + column names + SQL clauses expected in generated SQL. Empty `[]` for conversational cases. |
| `expected_answer_contains` | `string[]` | Concepts or values the analyst answer should address (used as DeepEval `expected_output`) |
| `context` | `string` | Human-readable description of what makes this case correct (used as DeepEval `context`) |
| `tags` | `string[]` | Categorisation labels — informational only |

### Difficulty Levels

| Level | Criteria |
|---|---|
| `easy` | Single table, single aggregation (COUNT, SUM, AVG, MAX, MIN), no JOINs, no HAVING |
| `medium` | Two-table JOIN or multi-aggregation or date filtering or HAVING clause |
| `hard` | Three-table JOIN, subquery, CTE, window function, or multi-condition HAVING |

---

## Dataset Coverage

### Difficulty Distribution

| Difficulty | Count | % |
|---|---|---|
| easy | 26 | 24.1% |
| medium | 55 | 50.9% |
| hard | 27 | 25.0% |

### SQL Pattern Coverage per Domain

Each domain covers the following SQL patterns across its 22 cases:

| Pattern | Health | Finance | Sales | IoT |
|---|---|---|---|---|
| COUNT | ✅ | ✅ | ✅ | ✅ |
| SUM | ✅ | ✅ | ✅ | ✅ |
| AVG | ✅ | ✅ | ✅ | ✅ |
| MAX / MIN | ✅ | ✅ | ✅ | ✅ |
| GROUP BY | ✅ | ✅ | ✅ | ✅ |
| ORDER BY + LIMIT | ✅ | ✅ | ✅ | ✅ |
| WHERE filter | ✅ | ✅ | ✅ | ✅ |
| Date range filter | ✅ | ✅ | ✅ | ✅ |
| HAVING | ✅ | ✅ | ✅ | ✅ |
| Two-table JOIN | ✅ | ✅ | ✅ | ✅ |
| Three-table JOIN | ✅ | — | — | ✅ |
| Percentage / ratio | ✅ | ✅ | ✅ | — |
| Time-series monthly trend | ✅ | ✅ | ✅ | ✅ |
| Subquery / CTE | ✅ | — | — | ✅ |

### Cross-DB Routing Case Design

The 20 `cross_db_routing` cases are built around deliberate ambiguity. Each case has a `context` field that explains the routing trap. Key patterns:

| Case ID | Ambiguity | Correct DB | Why it trips a naive router |
|---|---|---|---|
| X-002 | "Show me revenue by month" | finance | "Revenue" maps to `transactions.amount`. Sales has `opportunity_value` which is pipeline, not realised revenue |
| X-005 | "What is the average cost per patient?" | health | "Patient" is health-specific, but "cost" normally maps to finance |
| X-006 | "What is our churn rate?" | finance | Churn = subscription cancellations (finance). Sales has "Lost" leads but that is not churn |
| X-008 | "How many users have heart rate above 90 bpm?" | iot | "Heart rate bpm" is IoT wearable data, not health clinical data |
| X-013 | "Which diagnosis codes are most expensive?" | health | "Expensive" / "cost" language but "diagnosis codes" are unambiguously health |
| X-017 | "Show me the risk profile — patient risk score distribution" | health | "Risk profile" sounds like finance but "patient risk scores" is health terminology |

---

## Prerequisites

### 1. Working backend

The evaluator calls the live FastAPI backend at `POST /api/query`. The backend must be running with all four DB gateways healthy and the demo data loaded. The API is accessible at port **8000** via the unified server (`app.py`).

The evaluator performs a connectivity check against `/health` before starting the evaluation loop. If the backend is unreachable, the run aborts immediately with an error rather than hanging for the full `REQUEST_TIMEOUT` duration per case.

**Local:**
```bash
# Verify backend is up
curl http://localhost:8000/health
# Expected: {"status":"healthy","ollama_status":"running",...}

# Verify gateways are up
curl http://localhost:3001/health
curl http://localhost:3002/health
curl http://localhost:3003/health
curl http://localhost:3004/health
```

**Docker:**
```bash
docker compose ps
# All services should show (healthy)
```

### 2. Python environment

```bash
# Make sure you are in the project root with the virtualenv active (local)
source .venv/bin/activate

# Verify deepeval is installed
python -c "import deepeval; print(deepeval.__version__)"
```

### 3. DeepEval evaluator model (for answer quality metrics only)

Answer relevancy and faithfulness use a second LLM call to judge answers. The evaluator model (`DEEPEVAL_EVALUATOR_MODEL`) is deliberately different from the Llama 3 8B inference model to avoid self-consistency bias — the same model judging its own output inflates scores.

The default evaluator is `mistral:7b`. Set in `.env`:

```bash
# .env
DEEPEVAL_EVALUATOR_MODEL=mistral:7b          # default — architecturally independent from Llama 3 8B
DEEPEVAL_EVALUATOR_MODEL=deepseek-r1:32b     # more accurate judgements, needs ~20 GB RAM
```

Pull the evaluator model before running:
```bash
ollama pull mistral:7b                                # local
docker exec localgenbi-ollama ollama pull mistral:7b  # docker
```

> **Important:** The evaluator model must output clean JSON. Models that produce reasoning preamble before JSON (e.g. DeepSeek-R1 8B with `<think>` blocks) will cause DeepEval to raise `"Evaluation LLM outputted an invalid JSON"` and report 0.0 for answer quality scores. Mistral 7B and DeepSeek-R1 32B both produce clean structured output and work correctly.

> **Routing accuracy and SQL coverage do NOT require DeepEval or a second LLM call.** They are fully deterministic. Pass `--no-deepeval` to skip answer quality scoring entirely and get only routing + SQL results — much faster for iterative testing.

---

## Running the Evaluator — All Commands

All commands are run from the **project root** with the virtual environment active.

---

### Dry-run (no backend required)

Prints all 108 test cases with their ID, domain, difficulty, and query text. Does not call the backend.

```bash
python evaluation/agent_evaluator.py --dry-run
```

Output format:
```
[001] [HEALTH              ] [EASY  ] How many patients are in the database?
[002] [HEALTH              ] [EASY  ] What is the average age of all patients?
...
[108] [CROSS_DB_ROUTING    ] [HARD  ] What is our overall conversion funnel from lead to closed deal?
```

---

### Full evaluation — local backend

```bash
python evaluation/agent_evaluator.py \
    --backend http://localhost:8000 \
    --output evaluation/results/full_run_$(date +%Y%m%d).json
```

Expected runtime: **~18 minutes on CPU** (inference avg 8.1 s/query + DeepEval scoring avg 26.2 s/case + 5 s inter-case sleep · at `OLLAMA_TEMPERATURE=0.2` on Apple Silicon).

---

### Routing and SQL only — skip DeepEval (fast)

```bash
python evaluation/agent_evaluator.py \
    --no-deepeval \
    --backend http://localhost:8000 \
    --output evaluation/results/routing_sql_$(date +%Y%m%d).json
```

The `quality` section in the output will be present but all scores will be `null` with a note field explaining they were skipped.

---

### Full evaluation — Docker backend

```bash
# Run from host — hits port 8000
python evaluation/agent_evaluator.py \
    --backend http://localhost:8000 \
    --output evaluation/results/docker_run_$(date +%Y%m%d).json

# Or run the evaluator inside the Docker container (avoids host-to-container network overhead)
docker exec localgenbi-backend \
    python evaluation/agent_evaluator.py \
    --backend http://localhost:8000 \
    --output /app/evaluation/results/run.json
```

---

### Domain-specific runs

```bash
# Health only (22 cases)
python evaluation/agent_evaluator.py --domain health --backend http://localhost:8000

# Finance only (22 cases)
python evaluation/agent_evaluator.py --domain finance --backend http://localhost:8000

# Sales only (22 cases)
python evaluation/agent_evaluator.py --domain sales --backend http://localhost:8000

# IoT only (22 cases)
python evaluation/agent_evaluator.py --domain iot --backend http://localhost:8000

# Cross-DB routing only (20 ambiguous cases) — most important for supervisor testing
python evaluation/agent_evaluator.py --domain cross_db_routing --backend http://localhost:8000
```

---

### Smoke-test (fast — first N cases only)

```bash
# First 5 cases only
python evaluation/agent_evaluator.py --limit 5 --backend http://localhost:8000

# First 10 cases with output saved
python evaluation/agent_evaluator.py \
    --limit 10 \
    --backend http://localhost:8000 \
    --output evaluation/results/smoke.json
```

---

### Combined flags

```bash
# Finance domain, routing + SQL only, first 10 cases
python evaluation/agent_evaluator.py \
    --domain finance \
    --no-deepeval \
    --limit 10 \
    --backend http://localhost:8000 \
    --output evaluation/results/finance_smoke.json

# Cross-DB routing, full 20 cases, timestamped output
python evaluation/agent_evaluator.py \
    --domain cross_db_routing \
    --no-deepeval \
    --backend http://localhost:8000 \
    --output evaluation/results/routing_$(date +%Y%m%d_%H%M).json

# Full run with all metrics saved
python evaluation/agent_evaluator.py \
    --backend http://localhost:8000 \
    --output evaluation/results/full_$(date +%Y%m%d_%H%M).json
```

---

### Programmatic use (from a Python script or notebook)

```python
import asyncio
from evaluation.agent_evaluator import AgentEvaluator

evaluator = AgentEvaluator()

# Full run with DeepEval
results = asyncio.run(evaluator.run(backend_url="http://localhost:8000"))

# Routing + SQL only (no DeepEval)
results = asyncio.run(evaluator.run(
    backend_url = "http://localhost:8000",
    no_deepeval = True,
))

# Domain-specific with case limit
results = asyncio.run(evaluator.run(
    backend_url = "http://localhost:8000",
    domain      = "cross_db_routing",
    no_deepeval = True,
))

print(results["summary"])
print(results["per_domain"])
```

Each test case uses a unique session ID (`eval_{case_id}`) so session history from one case never contaminates routing decisions in subsequent cases. Because each session ID is unique, the dual memory system (short-term and long-term) remains completely isolated across evaluation cases.

---

## Reading the Output

### Terminal summary (always printed)

```
================================================================================
  LocalGenBI-Agent Evaluation Summary
================================================================================
  Inference model            : llama3:8b
  Evaluator model            : mistral:7b
  Total test cases           : 108
  Elapsed                    : 1084.4s
  Routing accuracy           : 95.4%
  Cross-DB routing accuracy  : 95.0%
  SQL keyword coverage       : 88.6%
  Answer relevancy (DeepEval): 80.8%
  Faithfulness (DeepEval)    : 77.6%
  DeepEval evaluated / skip  : 102 / 15

  Per-domain breakdown:
  Domain                  Cases   Routing   SQL Cov   Relevancy   Faithful
  ---------------------------------------------------------------------------
  cross_db_routing           20    95.0%    85.8%       70.4%      71.3%
  finance                    22    95.5%    84.3%       88.3%      80.8%
  health                     22   100.0%    92.0%       84.2%      77.5%
  iot                        22    90.9%    86.3%       74.5%      77.5%
  sales                      22    95.5%    94.5%       85.2%      80.4%
================================================================================
```

> **Reference run:** `OLLAMA_TEMPERATURE=0.2`, CPU-only hardware. Average inference time: **8.1 s/query**. Average DeepEval scoring time: **26.2 s/case**. Total wall-clock: **~18 minutes**.

> **Note on the `102 / 15` skip count:** This counts every retry warning as a skip attempt. True persistent failures — where all 5 retry attempts exhausted — were only **2 cases**: `S-022` and `I-014`. Both are high-column-count GROUP BY queries whose answers exceed Mistral 7B's `num_predict` budget before the JSON verdict closes. All other 13 cases that triggered the warning scored successfully on a later attempt. Filter `quality.all_cases` by `"skipped": true` to see only genuine persistent failures.

### JSON output structure

When `--output results.json` is passed, the file has this structure:

```json
{
  "summary": {
    "total_cases": 108,
    "elapsed_seconds": 1084.4,
    "routing_accuracy": 0.9537,
    "cross_db_routing_accuracy": 0.95,
    "avg_sql_keyword_coverage": 0.8862,
    "avg_answer_relevancy": 0.8079,
    "avg_faithfulness": 0.7761,
    "deepeval_evaluated_cases": 102,
    "deepeval_skipped_cases": 15,
    "inference_model": "llama3:8b",
    "evaluator_model": "mistral:7b"
  },

  "per_domain": {
    "health": {
      "total": 22,
      "routing_correct": 22,
      "routing_accuracy": 1.0,
      "avg_sql_coverage": 0.9197,
      "quality_evaluated_cases": 22,
      "avg_relevancy": 0.8425,
      "avg_faithfulness": 0.7749
    }
  },

  "routing": {
    "overall_routing_accuracy": 0.9537,
    "correct": 103,
    "total": 108,
    "cross_db_routing_accuracy": 0.95,
    "routing_failures": [...],
    "all_cases": [...]
  },

  "sql": {
    "avg_sql_coverage": 0.8862,
    "cases": [...],
    "low_coverage_cases": [...]
  },

  "quality": {
    "avg_relevancy": 0.8079,
    "avg_faithfulness": 0.7761,
    "evaluated_cases": 102,
    "skipped_cases": 15,
    "all_cases": [...],
    "failed_cases": [...]
  }
}
```

### Key sections to check first

| Section | What to look at |
|---|---|
| `summary` | Overall scores at a glance. Check `deepeval_skipped_cases` — if high, evaluator model has issues |
| `per_domain` | Which domain is weakest. Drill into that domain with `--domain` for a targeted run |
| `routing.routing_failures` | Failed/errored routing cases only — fast scan for patterns |
| `sql.low_coverage_cases` | SQL cases below 50% coverage — structural generation problems |
| `quality.failed_cases` | Cases below relevancy or faithfulness threshold |
| `quality.all_cases` | Full per-case quality scores — filter by `"skipped": true` to see what the evaluator couldn't judge |

---

## Score Interpretation

### Routing accuracy

| Score | Interpretation |
|---|---|
| > 90% | Supervisor routing is working well across clear-domain queries |
| 75–90% | Acceptable for a POC; review `routing_failures` for patterns |
| 60–75% | Routing is unreliable; prompt tuning or keyword list expansion needed |
| < 60% | Fundamental routing problem — check supervisor prompt and LLM response parsing |

**Cross-DB routing accuracy is expected to be lower** than overall accuracy. These 20 cases are deliberately hard. A score of 65–75% on cross-DB routing with an 8B model is reasonable; above 80% indicates the supervisor prompt handles ambiguity well. The reference run achieved **95.0%** (19/20), with one failure: `I-013` (`List all users with average heart rate above 80 bpm`) was routed to `health` instead of `iot`. The term `heart rate` maps plausibly to health clinical data; only the `heart_rate_avg` table name is an unambiguous IoT fingerprint.

### SQL keyword coverage

| Score | Interpretation |
|---|---|
| > 80% | SQL generation is structurally correct on most queries |
| 60–80% | The model generates valid SQL but misses some expected constructs (ORDER BY, HAVING) |
| < 60% | SQL generation is weak; review low-coverage cases for patterns by difficulty |

### Answer relevancy and faithfulness

These metrics require the DeepEval evaluator LLM to judge outputs. Treat them as directional signals rather than precise scores.

| Score | Interpretation |
|---|---|
| > 75% | Analyst agent is generating coherent, on-topic answers |
| 60–75% | Some answers are vague or incomplete; check `failed_cases` for patterns |
| < 60% | Analyst prompt may need revision, or data is often empty (routing/SQL failures upstream) |

**Note:** Answer quality metrics are meaningless if routing or SQL accuracy is poor. Always check routing and SQL scores first.

### What to do with routing failures

1. Check `routing.routing_failures` — look for clustering by `query_type`
2. If all failures are `cross_db_routing`, the supervisor keyword list or LLM prompt is the problem
3. Look at the `sql` field in each failure — does the SQL contain tables from the wrong domain?
4. Adjust `SUPERVISOR_ROUTING_KEYWORDS` in `config/constants.py`, then re-run with `--no-deepeval` for a fast iteration cycle

**Known routing failure in reference run — `I-013`:** "List all users with average heart rate above 80 bpm" was routed to `health` instead of `iot`. Fix: add `heart_rate_avg` as an explicit IoT keyword in `SUPERVISOR_ROUTING_KEYWORDS`.

### What to do with SQL coverage failures

1. Check `sql.low_coverage_cases`
2. Look at the `missing` list per case — are they SQL keywords (JOIN, HAVING) or table names?
3. If `missing` always includes `ORDER BY` and `LIMIT`, the model may be generating valid SQL but ignoring the ranking/limiting instruction
4. Adjust the sql_agent prompt in `config/prompts.py`, then re-run with `--no-deepeval`

**Known DB-level SQL errors in reference run:**

| Case | Error | Root cause |
|---|---|---|
| `F-021`, `X-006` | `column pf.payment_method / pf.status does not exist` | Model aliases `payment_failures` as `pf` then references columns not on that table. Fix: add explicit `payment_failures` column list to `FINANCE_AGENT_PROMPT`. |
| `S-017` | `date_part(unknown, integer) does not exist` | Model passes an integer literal to `date_part()` without casting. Fix: add a type-cast example to `_get_error_guidance()` TYPE ERROR pattern. |
| `I-019` | `SELECT DISTINCT … ORDER BY` column not in select list | PostgreSQL requires ORDER BY columns to appear in SELECT when DISTINCT is used. Fix: add this constraint to the GROUP BY guidance in `_get_error_guidance()`. |

### What to do with high DeepEval skip rate

1. Check `quality.all_cases` — filter `"skipped": true` entries and read `skip_reason`
2. Most common cause: evaluator model returning empty string or `<think>` preamble before JSON
3. Fix: switch to `mistral:7b` which reliably outputs clean JSON for DeepEval metrics
4. Verify the evaluator model is pulled: `ollama list`
5. Verify Ollama is responding: `curl http://localhost:11434/api/tags`

---

## Known Measurement Gaps

### Routing inference is indirect

The evaluator infers routing from SQL table names. If the SQL contains no table names (e.g. a syntax error that produces no output), the routing is reported as `no_sql_executed` in failures, not as a routing error.

### SQL coverage is syntactic, not semantic

A case with `expected_sql_keywords: ["JOIN", "patient_history", "claims"]` will pass coverage if those strings appear anywhere in the SQL — even if the JOIN is incorrect or the logic is wrong. Coverage measures structure, not correctness.

### DeepEval metrics depend on evaluator model quality

`AnswerRelevancyMetric` and `FaithfulnessMetric` both make LLM calls. If the evaluator model produces inconsistent judgements, scores will have high variance. Do not rely on single-digit percentage differences as significant signals.

### Cases with errors are excluded from DeepEval metrics

If the agent returns `error` or an empty `answer`, that case is excluded from the DeepEval `evaluated_cases` count. DeepEval scores are computed only over cases that produced any answer — they are an upper bound on true quality.

### Long-term memory does not affect evaluation

Each evaluation case uses a unique session ID (`eval_{case_id}`), which means long-term memory is empty for every case. This is intentional — evaluation measures the agent's raw reasoning quality, not accumulated preferences. To test long-term memory behaviour, run manual multi-turn sessions.

### No ground-truth SQL

The dataset does not have an `expected_sql` field with a single correct SQL query because there are often multiple correct ways to write a query for the same question.

---

## Extending the Dataset

To add new test cases, append objects to the `test_cases` array in `evaluation/golden_dataset.json`.

### Template

```json
{
  "id": "H-023",
  "query_type": "health",
  "difficulty": "medium",
  "input": "Your natural language question here",
  "expected_database": "health",
  "expected_sql_keywords": [
    "table_name_1",
    "column_name",
    "SQL_KEYWORD"
  ],
  "expected_answer_contains": [
    "concept or value the answer should mention"
  ],
  "context": "Description of what makes a correct answer, referencing the demo data",
  "tags": ["your", "tags"]
}
```

### ID conventions

| Prefix | Domain |
|---|---|
| `H-` | health |
| `F-` | finance |
| `S-` | sales |
| `I-` | iot |
| `X-` | cross_db_routing |
| `C-` | conversational |

### Adding cross-DB routing cases

```json
{
  "id": "X-021",
  "query_type": "cross_db_routing",
  "difficulty": "hard",
  "input": "What is our average customer value?",
  "expected_database": "finance",
  "expected_sql_keywords": ["transactions", "customer_id", "AVG", "amount"],
  "expected_answer_contains": ["customer value", "average"],
  "context": "Customer value = average transaction amount (finance). Sales has lead/opportunity data but that is pipeline value, not realised customer spend.",
  "tags": ["routing", "finance_vs_sales", "customer_value"]
}
```

### Adding conversational routing cases

Conversational test cases verify that the supervisor correctly bypasses the SQL pipeline for non-BI queries. Use prefix `C-` and `"expected_database": "conversational"`. The `expected_sql_keywords` array must be empty because no SQL is generated.

```json
{
  "id": "C-001",
  "query_type": "conversational",
  "difficulty": "easy",
  "input": "Hi, who are you?",
  "expected_database": "conversational",
  "expected_sql_keywords": [],
  "expected_answer_contains": ["LocalGenBI", "database", "health"],
  "context": "Identity question — should receive a short description of the system and its four domains. No SQL should be executed.",
  "tags": ["conversational", "identity"]
}
```

**What the evaluator checks for conversational cases:**
- Routing accuracy: `planned_databases == ["conversational"]` and `sql_executed == []`
- SQL coverage: always 1.0 (vacuously true — no SQL expected)
- Answer relevancy / faithfulness: scored by DeepEval as usual

### Validate after adding

```bash
python -c "
import json
from pathlib import Path

data     = json.load(open('evaluation/golden_dataset.json'))
required = {'id', 'input', 'expected_database', 'expected_sql_keywords', 'context'}
errors   = []
ids      = set()

for tc in data['test_cases']:
    missing = required - tc.keys()
    if missing:
        errors.append(f\"{tc.get('id','?')} missing: {missing}\")
    if tc.get('id') in ids:
        errors.append(f\"Duplicate ID: {tc['id']}\")
    ids.add(tc.get('id'))

if errors:
    print('ERRORS:')
    for e in errors: print(' ', e)
else:
    print(f'OK — {len(data[\"test_cases\"])} cases, all valid')
"
```

---

## Running in CI

For a fast CI check (routing and SQL only — no DeepEval LLM calls) that exits non-zero on failure:

```bash
python -c "
import asyncio, json, sys
from evaluation.agent_evaluator import run_evaluation, load_golden_dataset

golden  = [tc for tc in load_golden_dataset() if tc['query_type'] == 'cross_db_routing']
results = asyncio.run(run_evaluation(golden, 'http://localhost:8000', no_deepeval=True))

with open('ci_eval_results.json', 'w') as f:
    json.dump(results, f, indent=2)

accuracy = results['summary']['cross_db_routing_accuracy']
print(f'Cross-DB routing accuracy: {accuracy:.1%}')
sys.exit(0 if accuracy >= 0.70 else 1)
"
```

---

## Evaluation Architecture Reference

### Why end-to-end evaluation rather than unit tests per node

The harness calls the live system end-to-end — not individual agent nodes in isolation. This is a deliberate design choice. Unit-testing each node separately would not catch:

- Routing correct, but SQL wrong (supervisor passes, sql_agent generates bad SQL)
- SQL structurally correct, but analyst hallucinates from the result
- Retry loop silently recovering an error that a per-node test would never trigger
- Cross-DB fan-out producing results that look correct per-domain but are misleading when merged
- Long-term memory injecting stale context that subtly affects routing in Phase 3

An end-to-end golden dataset eval is the only way to measure the emergent behaviour of the full six-node LangGraph pipeline.

```
evaluation/
├── agent_evaluator.py
│   ├── OllamaEvaluator             DeepEvalBaseLLM subclass — calls Ollama /api/generate directly
│   │                               Retry logic (5 attempts, exponential back-off) per generate() call
│   ├── load_golden_dataset()       Load + validate golden_dataset.json
│   ├── _check_backend_reachable()  Liveness probe before the evaluation loop
│   ├── call_agent()                POST /api/query (unique session_id per case)
│   ├── evaluate_routing()          Table-fingerprint routing accuracy
│   ├── evaluate_sql_coverage()     Keyword presence check
│   ├── build_deepeval_test_cases() Map (golden, response) pairs → LLMTestCase tuples
│   ├── run_deepeval()              metric.measure(tc) per case — avoids evaluate() wrapper
│   ├── _compute_per_domain_summary() Routing + SQL + quality breakdown per domain
│   ├── run_evaluation()            Main pipeline: check → gather → routing → sql → quality → per_domain
│   ├── _print_summary()            Terminal output including per-domain breakdown table
│   ├── AgentEvaluator              Class for programmatic / notebook use (supports no_deepeval flag)
│   └── main()                      CLI entry point (--no-deepeval, --domain, --limit, --output, --dry-run)
│
└── golden_dataset.json             108 test cases
    ├── _meta                       Version, total_cases, breakdown, schema
    └── test_cases[]                H-001…H-022, F-001…F-022,
                                    S-001…S-022, I-001…I-022,
                                    X-001…X-020
```

### Two-model architecture

| Role | Model | Env var | Used by |
|---|---|---|---|
| SQL generation + BI analysis | `llama3:8b` | `OLLAMA_MODEL` | Backend inference (all 108 queries) |
| Answer quality judge | `mistral:7b` | `DEEPEVAL_EVALUATOR_MODEL` | `OllamaEvaluator` inside `run_deepeval()` only |

These are deliberately separate models to avoid self-consistency bias.

### Why metric.measure() instead of evaluate()

`metric.measure(tc)` gives direct access to `metric.score` after the call — no attribute introspection required. Each case is evaluated independently so a failure on one case is recorded as a `skipped` entry in `all_cases` and does not affect other cases.

### Evaluation data flow

```
golden_dataset.json
        │
        ▼
load_golden_dataset()
        │  108 typed dicts
        ▼
_check_backend_reachable()   ← aborts immediately if backend is down
        │
        ▼
[for each case] call_agent(tc["input"], session_id=f"eval_{tc['id']}")
        │  POST /api/query → app.py → FastAPI → LangGraph → response JSON
        │  (unique session per case — no history bleed between cases)
        │  (long-term memory is empty for each fresh eval session)
        ▼
responses[]  (same order as golden[])
        │
        ├─── evaluate_routing()          → routing_report
        ├─── evaluate_sql_coverage()     → sql_report
        └─── build_deepeval_test_cases() → [(golden_tc, LLMTestCase), ...]
                        │
                        ▼  (skipped if --no-deepeval)
                  run_deepeval()          → quality_report
                        │
                        ▼
        _compute_per_domain_summary()    → per_domain breakdown
                        │
                        ▼
              {summary, per_domain, routing, sql, quality}
                        │
              _print_summary() + optional JSON file write
```