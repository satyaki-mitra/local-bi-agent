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

For `cross_db_routing` cases, the full set of matched domains is checked — a case is correct if `expected_database` appears anywhere in the matched set. This correctly handles queries where both domains appear in the SQL.

**Why this approach:** The FastAPI response does not expose the supervisor's routing decision directly. Using SQL table names as fingerprints is a reliable proxy because the four domains have completely non-overlapping table names (`patient_history` can only be health; `payment_failures` can only be finance; `daily_steps` can only be IoT; etc.).

**Reported as:** `overall_routing_accuracy` (all 108 cases) and `cross_db_routing_accuracy` (the 20 cross-domain cases only, as a separate sub-score).

### 2. SQL Keyword Coverage

**What it tests:** Does the generated SQL contain the tables and SQL clauses that a correct answer to this question requires?

**How it works:** Each golden case has an `expected_sql_keywords` list containing table names, column names, and SQL keywords (e.g. `["claims", "patient_history", "JOIN", "SUM", "claim_amount"]`). The evaluator checks what fraction of these appear (case-insensitive) in the `sql_executed` string.

**Why this matters:** Routing accuracy tells you the right database was chosen; SQL coverage tells you the SQL was structurally sensible. A query that routes correctly but generates `SELECT * FROM claims LIMIT 100` for "total claim value by diagnosis code" passes routing but fails SQL coverage.

**Known limitation:** Coverage measures structure, not semantic correctness. A query using `WHERE EXTRACT(year FROM date) = 2024` will score 0 on a keyword `2024` even though it is functionally correct. SQL keyword coverage is a structural proxy, not an exact correctness measure.

**Reported as:** per-case `coverage` ratio (0.0–1.0) and `avg_sql_coverage` overall. Cases with coverage < 0.5 are listed separately as `low_coverage_cases`.

### 3. Answer Relevancy (DeepEval)

**What it tests:** Is the natural-language answer the analyst agent generated actually relevant to the original question?

**How it works:** Uses DeepEval's `AnswerRelevancyMetric`. The metric is LLM-based — it uses the evaluator model (`DEEPEVAL_EVALUATOR_MODEL`, default `ollama/deepseek-r1:8b`) to judge whether the answer addresses the question. A score ≥ 0.7 passes.

**Reported as:** `avg_relevancy` and a list of `failed_cases` below the threshold.

### 4. Faithfulness (DeepEval)

**What it tests:** Does the answer stay consistent with the data that was returned from the database? An answer that makes claims not supported by the query result fails faithfulness even if it is well-written.

**How it works:** Uses DeepEval's `FaithfulnessMetric`. The retrieval context passed to DeepEval is the raw SQL that was executed. A score ≥ 0.7 passes.

**Reported as:** `avg_faithfulness` and included in `failed_cases`.

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
| `query_type` | `string` | One of: `health`, `finance`, `sales`, `iot`, `cross_db_routing` |
| `difficulty` | `string` | One of: `easy`, `medium`, `hard` — informational only, not used in scoring |
| `input` | `string` | The exact natural-language query sent to the agent |
| `expected_database` | `string` | The database the supervisor must route to |
| `expected_sql_keywords` | `string[]` | Table names + column names + SQL clauses expected in generated SQL |
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
| easy | 30 | 27.8% |
| medium | 53 | 49.1% |
| hard | 25 | 23.1% |

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

The evaluator calls the live FastAPI backend at `POST /api/query`. The backend must be running with all four DB gateways healthy and the demo data loaded.

The evaluator performs a connectivity check against `/health` before starting the evaluation loop. If the backend is unreachable, the run aborts immediately with an error rather than hanging for the full `REQUEST_TIMEOUT` duration per case.

**Local:**
```bash
# Verify backend is up
curl http://localhost:8001/health
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

Answer relevancy and faithfulness use a second LLM call to judge answers. The default evaluator model is `ollama/deepseek-r1:8b`. For more accurate judgements use the 32b model if your hardware supports it:

```bash
# .env
DEEPEVAL_EVALUATOR_MODEL=ollama/deepseek-r1:8b    # default evaluator — different from inference model (Llama 3 8B)
DEEPEVAL_EVALUATOR_MODEL=ollama/deepseek-r1:32b   # more accurate judgements, needs ~20 GB RAM
```

Pull the 32b evaluator model if needed:
```bash
ollama pull deepseek-r1:32b                                    # local
docker exec localgenbi-ollama ollama pull deepseek-r1:32b      # docker
```

> **Routing accuracy and SQL coverage do NOT require DeepEval or a second LLM call.**
> They are fully deterministic. You can run those two metrics and skip answer quality
> by inspecting the JSON output before `run_deepeval()` is invoked, or by simply
> reading the `routing` and `sql` sections from `--output results.json`.

---

## Running the Evaluator — All Commands

All commands are run from the **project root** with the virtual environment active.

---

### Dry-run (no backend required)

Prints all 108 test cases with their ID, domain, difficulty, and query text. Does not call the backend. Use this to inspect the dataset before running.

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

Runs all 108 cases. Includes routing, SQL coverage, and DeepEval answer quality metrics. Results are printed to terminal and optionally saved as JSON.

```bash
python evaluation/agent_evaluator.py \
    --backend http://localhost:8001 \
    --output evaluation/results/full_run_$(date +%Y%m%d).json
```

Expected runtime: **60–180 minutes on CPU** (108 LLM inference cycles × 2–4 LLM calls per cycle, plus DeepEval scoring).

---

### Full evaluation — Docker backend

```bash
python evaluation/agent_evaluator.py \
    --backend http://localhost:8001 \
    --output evaluation/results/docker_run_$(date +%Y%m%d).json
```

Or run the evaluator inside the Docker container (avoids host-to-container network overhead):

```bash
docker exec localgenbi-backend \
    python evaluation/agent_evaluator.py \
    --backend http://localhost:8001 \
    --output /app/evaluation/results/run.json
```

---

### Domain-specific runs

Run evaluation for a single domain only.

```bash
# Health only (22 cases)
python evaluation/agent_evaluator.py --domain health --backend http://localhost:8001

# Finance only (22 cases)
python evaluation/agent_evaluator.py --domain finance --backend http://localhost:8001

# Sales only (22 cases)
python evaluation/agent_evaluator.py --domain sales --backend http://localhost:8001

# IoT only (22 cases)
python evaluation/agent_evaluator.py --domain iot --backend http://localhost:8001

# Cross-DB routing only (20 ambiguous cases) — most important for supervisor testing
python evaluation/agent_evaluator.py --domain cross_db_routing --backend http://localhost:8001
```

---

### Smoke-test (fast — first N cases only)

Use `--limit` to cap the number of cases. Useful for verifying the harness works before committing to a long run.

```bash
# First 5 cases only
python evaluation/agent_evaluator.py --limit 5 --backend http://localhost:8001

# First 10 cases with output saved
python evaluation/agent_evaluator.py \
    --limit 10 \
    --backend http://localhost:8001 \
    --output evaluation/results/smoke.json
```

---

### Combined flags

```bash
# Finance domain, first 10 cases, save results
python evaluation/agent_evaluator.py \
    --domain finance \
    --limit 10 \
    --backend http://localhost:8001 \
    --output evaluation/results/finance_smoke.json

# Cross-DB routing, full 20 cases, timestamped output
python evaluation/agent_evaluator.py \
    --domain cross_db_routing \
    --backend http://localhost:8001 \
    --output evaluation/results/routing_$(date +%Y%m%d_%H%M).json
```

---

### Programmatic use (from a Python script or notebook)

```python
import asyncio
from evaluation.agent_evaluator import AgentEvaluator

evaluator = AgentEvaluator()

# Full run
results = asyncio.run(evaluator.run(backend_url="http://localhost:8001"))

# Domain-specific
results = asyncio.run(evaluator.run(
    backend_url="http://localhost:8001",
    domain="cross_db_routing"
))

# With case limit
results = asyncio.run(evaluator.run(
    backend_url="http://localhost:8001",
    domain="health",
    limit=5
))

print(results["summary"])
```

Each test case uses a unique session ID (`eval_{case_id}`) so session history from one case never contaminates routing decisions in subsequent cases.

---

## Reading the Output

### Terminal summary (always printed)

```
======================================================================
  LocalGenBI-Agent Evaluation Summary
======================================================================
  Total test cases           : 108
  Elapsed                    : 4823.4s
  Routing accuracy           : 87.0%
  Cross-DB routing accuracy  : 75.0%
  SQL keyword coverage       : 72.3%
  Answer relevancy (DeepEval): 68.1%
  Faithfulness (DeepEval)    : 71.4%
======================================================================
```

### JSON output structure

When `--output results.json` is passed, the file has this structure:

```json
{
  "summary": {
    "total_cases": 108,
    "elapsed_seconds": 4823.4,
    "routing_accuracy": 0.87,
    "cross_db_routing_accuracy": 0.75,
    "avg_sql_keyword_coverage": 0.723,
    "avg_answer_relevancy": 0.681,
    "avg_faithfulness": 0.714
  },
  "routing": {
    "overall_routing_accuracy": 0.87,
    "correct": 94,
    "total": 108,
    "cross_db_routing_accuracy": 0.75,
    "cross_correct": 15,
    "cross_total": 20,
    "routing_failures": [
      {
        "id": "X-002",
        "input": "Show me revenue by month for the last year",
        "expected": "finance",
        "actual": ["sales"],
        "sql": ["SELECT SUM(opportunity_value)..."],
        "is_cross": true
      }
    ]
  },
  "sql": {
    "avg_sql_coverage": 0.723,
    "cases": [
      {"id": "H-001", "coverage": 1.0, "found": ["patient_history", "COUNT"], "missing": []},
      {"id": "H-013", "coverage": 0.57, "found": ["SUM", "claims"], "missing": ["ORDER BY", "LIMIT", "GROUP BY"]}
    ],
    "low_coverage_cases": [...]
  },
  "quality": {
    "avg_relevancy": 0.681,
    "avg_faithfulness": 0.714,
    "evaluated_cases": 103,
    "failed_cases": [...]
  }
}
```

Note: for cross-DB routing failures, `actual` is a list of matched domains rather than a single string.

---

## Score Interpretation

### Routing accuracy

| Score | Interpretation |
|---|---|
| > 90% | Supervisor routing is working well across clear-domain queries |
| 75–90% | Acceptable for a POC; review `routing_failures` for patterns |
| 60–75% | Routing is unreliable; prompt tuning or keyword list expansion needed |
| < 60% | Fundamental routing problem — check supervisor prompt and LLM response parsing |

**Cross-DB routing accuracy is expected to be lower** than overall accuracy. These 20 cases are deliberately hard. A score of 65–75% on cross-DB routing with an 8B model is reasonable; above 80% indicates the supervisor prompt handles ambiguity well.

### SQL keyword coverage

| Score | Interpretation |
|---|---|
| > 80% | SQL generation is structurally correct on most queries |
| 60–80% | The model generates valid SQL but misses some expected constructs (ORDER BY, HAVING) |
| < 60% | SQL generation is weak; review low-coverage cases for patterns by difficulty |

Look at `low_coverage_cases` — if failures cluster on `difficulty: hard` (3-table JOINs, CTEs), that is expected for an 8B model. If `easy` cases are failing SQL coverage, there is likely a prompt or schema introspection problem.

### Answer relevancy and faithfulness

These metrics require the DeepEval evaluator LLM to judge outputs, so they are themselves subject to LLM variance. Treat them as directional signals rather than precise scores.

| Score | Interpretation |
|---|---|
| > 75% | Analyst agent is generating coherent, on-topic answers |
| 60–75% | Some answers are vague or incomplete; check failed_cases for patterns |
| < 60% | Analyst prompt may need revision, or data is often empty (routing/SQL failures upstream) |

**Note:** Answer quality metrics are meaningless if routing or SQL accuracy is poor. Always check routing and SQL scores first.

### What to do with routing failures

1. Check `routing_failures` in the JSON output
2. Look for clustering by `query_type` — if all failures are `cross_db_routing`, the supervisor keyword list or LLM prompt is the problem
3. Look at the `sql` field in each failure — does the SQL contain tables from the wrong domain?
4. Adjust `SUPERVISOR_ROUTING_KEYWORDS` in `config/constants.py` to add domain-specific terms, then re-run

### What to do with SQL coverage failures

1. Check `low_coverage_cases` in the JSON output
2. Check which keywords are in `missing` — are they SQL keywords (JOIN, HAVING) or table names? Missing table names suggest the model is not reading the schema correctly
3. If `missing` always includes `ORDER BY` and `LIMIT`, the model may be generating valid SQL but ignoring the ranking/limiting instruction in the prompt
4. Adjust the sql_agent prompt in `config/prompts.py`

---

## Known Measurement Gaps

### Routing inference is indirect

The evaluator infers routing from SQL table names. If the SQL contains no table names (e.g. a syntax error that produces no output), the routing is reported as `no_sql_executed` in failures, not as a routing error. Track `no_sql_executed` entries in `routing_failures` separately from wrong-domain cases.

### SQL coverage is syntactic, not semantic

A case with `expected_sql_keywords: ["JOIN", "patient_history", "claims"]` will pass coverage if those strings appear anywhere in the SQL — even if the JOIN is incorrect or the logic is wrong. Coverage measures structure, not correctness. Additionally, semantically equivalent SQL that uses different syntax (e.g. `WHERE EXTRACT(year FROM date) = 2024` instead of `WHERE date >= '2024-01-01'`) will miss date-filter keyword matches even when functionally correct.

### DeepEval metrics depend on evaluator model quality

`AnswerRelevancyMetric` and `FaithfulnessMetric` both make LLM calls using the evaluator model. If the evaluator model produces inconsistent judgements, the scores will have high variance across runs. Do not rely on single-digit percentage differences in these metrics as significant signals.

### Cases with errors are excluded from DeepEval metrics

If the agent returns `error` or an empty `answer` (routing failed, SQL failed after all retries, Ollama timeout), that case is excluded from the DeepEval `evaluated_cases` count. DeepEval scores are computed only over cases that produced any answer — they are an upper bound on true quality.

### No ground-truth SQL

The dataset does not have an `expected_sql` field with a single correct SQL query because there are often multiple correct ways to write a query for the same question. The keyword coverage approach trades exactness for flexibility.

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

Increment the number from the highest existing ID in that domain.

### Adding cross-DB routing cases

Cross-DB routing cases are the most valuable to add. A good routing case has:
- A query that uses terms genuinely common to two or more domains
- A clear correct answer (one database is unambiguously right given the full query context)
- A `context` field that explicitly states the routing trap

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

### Validating after adding cases

```bash
python -c "
import json
from pathlib import Path

data   = json.load(open('evaluation/golden_dataset.json'))
required = {'id', 'input', 'expected_database', 'expected_sql_keywords', 'context'}
errors = []
ids    = set()

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
results = asyncio.run(run_evaluation(golden, 'http://localhost:8001'))

with open('ci_eval_results.json', 'w') as f:
    json.dump(results, f, indent=2)

accuracy = results['summary']['cross_db_routing_accuracy']
print(f'Cross-DB routing accuracy: {accuracy:.1%}')
sys.exit(0 if accuracy >= 0.70 else 1)
"
```

---

## Evaluation Architecture Reference

```
evaluation/
├── agent_evaluator.py
│   ├── _check_backend_reachable()  Liveness probe before the evaluation loop
│   ├── load_golden_dataset()       Load + validate golden_dataset.json
│   ├── call_agent()                POST /api/query (unique session_id per case)
│   ├── evaluate_routing()          Table-fingerprint routing accuracy
│   │                               Cross-DB: full domain-set matching
│   ├── evaluate_sql_coverage()     Keyword presence check
│   ├── build_deepeval_test_cases() Map responses → LLMTestCase objects
│   ├── run_deepeval()              AnswerRelevancyMetric + FaithfulnessMetric
│   │                               Score extraction via metrics_data (deepeval >= 0.21)
│   ├── run_evaluation()            Main pipeline: check → gather → routing → sql → quality
│   ├── AgentEvaluator              Class for programmatic / notebook use
│   └── main()                      CLI entry point
│
└── golden_dataset.json             108 test cases
    ├── _meta                       Version, total_cases, breakdown, schema
    └── test_cases[]                H-001…H-022, F-001…F-022,
                                    S-001…S-022, I-001…I-022,
                                    X-001…X-020
```

### How routing fingerprinting works

```python
fingerprints = {
    "health"  : ["PATIENT_HISTORY", "CLAIMS", "PROCEDURES", "DIAGNOSIS_CODE"],
    "finance" : ["TRANSACTIONS", "SUBSCRIPTIONS", "PAYMENT_FAILURES", "MONTHLY_FEE"],
    "sales"   : ["LEADS", "OPPORTUNITIES", "SALES_REPS", "OPPORTUNITY_VALUE"],
    "iot"     : ["DAILY_STEPS", "HEART_RATE_AVG", "SLEEP_HOURS", "STEP_COUNT"],
}
# Single-domain: returns the one matched domain, or None if no match.
# Cross-DB: returns the full set of matched domains.
# A cross-DB case is correct if expected_database appears anywhere in the matched set.
```

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
        │  POST /api/query → FastAPI → LangGraph → response JSON
        │  (unique session per case — no history bleed between cases)
        ▼
responses[]  (same order as golden[])
        │
        ├─── evaluate_routing()          → routing_report
        ├─── evaluate_sql_coverage()     → sql_report
        └─── build_deepeval_test_cases() → LLMTestCase[]
                        │
                        ▼
                  run_deepeval()          → quality_report
                        │
                        ▼
              {summary, routing, sql, quality}
                        │
              _print_summary() + optional JSON file write
```