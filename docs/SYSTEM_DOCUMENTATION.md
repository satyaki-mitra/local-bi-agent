# Application Architecture

This document describes the internal design of LocalGenBI-Agent: the agent pipeline, data flow, security layers, statistical analysis layer, and the export subsystem.

---

## Agent Pipeline

Every query follows a fixed five-node LangGraph state machine. The state is a typed Python dict (`BIState`) that flows through each node.

```
User query
    │
    ▼
┌──────────────┐
│  supervisor  │  Routes query to one database based on keyword
│   _agent     │  matching + LLM confirmation. Validates cross-DB
│              │  pairs against ALLOWED_CROSS_DB_PAIRS allowlist.
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ fetch_schema │  Calls /gateway → get_schema on target database
│              │  Injects live schema JSON into LangGraph state
└──────┬───────┘
       │
       ▼
┌──────────────┐  ┌──────────────────────────────────────────┐
│  sql_agent   │  │  Guardrail layer 1: SQLValidator          │
│              │──│  - keyword blocklist (frozenset, O(1))    │
│              │  │  - prohibited pattern regex               │
│              │  │  - LIMIT injection (outer-wrap approach   │
│              │  │    to prevent subquery bypass)            │
│              │  │                                           │
│              │  │  Guardrail layer 2: Orchestrator check    │
│              │  │  - PROHIBITED_SQL_PATTERNS from constants │
└──────┬───────┘  └──────────────────────────────────────────┘
       │
       │  [conditional edge — retry on error, up to MAX_AGENT_RETRIES]
       │
       ▼
┌──────────────┐
│ execute_sql  │  Posts to /gateway → query_database
│              │  Strips BLOCKED_OUTPUT_COLUMNS from result
│              │  On cross-DB: partial results returned on
│              │  per-domain failure (does not abort full query)
└──────┬───────┘
       │
       │  [conditional edge — retry on DB error, up to MAX_AGENT_RETRIES]
       │
       ▼
┌──────────────┐
│  analyst     │  Reconstructs DataFrame from state data
│   _agent     │  Calls DataAnalyzer for statistical metrics
│              │  Calls LLM with data table + metrics to generate answer
│              │  PII-redacts answer
│              │  Generates visualization (if enabled)
└──────┬───────┘
       │
       ▼
QueryResponse (FastAPI) → Chainlit → User
```

### State fields

| Field | Type | Set by |
|---|---|---|
| `query` | `str` | API layer (PII-redacted at entry) |
| `session_id` | `str` | API layer |
| `planned_databases` | `List[str]` | supervisor_agent |
| `is_cross_db` | `bool` | supervisor_agent |
| `current_schema` | `str` | fetch_schema (single-DB path) |
| `cross_db_schemas` | `Dict[str, str]` | fetch_schema (cross-DB path) |
| `sql` | `str` | sql_agent |
| `data` | `List[Dict]` | execute_sql |
| `cross_db_results` | `Dict[str, List]` | execute_sql (cross-DB path) |
| `metrics` | `Dict` | analyst_agent |
| `answer` | `str` | analyst_agent |
| `errors` | `List[str]` | Any node that encounters an error |
| `retry_count` | `int` | sql_agent / execute_sql |
| `visualization` | `Dict \| None` | analyst_agent |

---

## Supervisor Routing

The supervisor uses a two-phase approach to pick the target database.

**Phase 1 — Keyword pre-check:** A configurable dict maps domain names to keyword lists (`SUPERVISOR_ROUTING_KEYWORDS` in `config/constants.py`). Every domain whose keywords appear in the query is added to `pre_targets`. All matching domains are collected (not just the first) to support cross-DB scenarios.

**Phase 2 — LLM confirmation:** The supervisor sends the query and a list of valid databases to the LLM and asks it to return a JSON array. The LLM result is parsed with a regex guard against malformed JSON. If it produces an empty or invalid response, the system falls back to `pre_targets`, then to the configured default (`SUPERVISOR_DEFAULT_DATABASE`).

**Cross-DB pair validation:** When `ENABLE_CROSS_DB_JOINS=true`, the supervisor validates the planned database pair against `ALLOWED_CROSS_DB_PAIRS` (a frozenset-of-frozensets in `config/constants.py`). Only explicitly whitelisted pairs are permitted. This prevents the LLM from constructing unauthorised cross-domain queries.

This two-phase design means a complete LLM failure degrades to keyword routing, not a complete failure.

---

## SQL Generation and Validation

`sql_agent` sends the live schema (fetched from the gateway in the previous node) together with the domain system prompt and user query to the LLM. The LLM is instructed to output raw PostgreSQL with no markdown or explanation.

After stripping think-tags and markdown fences, the SQL passes through two independent validation layers:

1. **SQLValidator** (`guardrails/sql_validator.py`)
   - Keyword blocklist: rejects any SQL containing `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, `TRUNCATE`, etc. (frozenset, case-normalised, comment-stripped before check to prevent `DR--comment\nOP` bypass)
   - Prohibited pattern check: rejects `PG_SLEEP`, `INFORMATION_SCHEMA`, `COPY`, `LO_*`, `XP_*`
   - LIMIT injection: wraps the entire query as a subquery: `SELECT * FROM ({sql}) _limited LIMIT {max_rows}`. This prevents the subquery bypass where an inner `LIMIT 99999` could escape the outer limit check.

2. **Orchestrator pattern check** (`orchestrator.py`)
   - A second independent check against `PROHIBITED_SQL_PATTERNS` from `config/constants.py`

If either check fails, the error is appended to `state["errors"]`, `retry_count` is incremented, and the conditional edge routes back to `sql_agent`. After `MAX_AGENT_RETRIES` failures, the pipeline routes directly to `analyst_agent` which returns a graceful error message.

---

## DB Gateway Security Pipeline

Each gateway applies these operations in order after query execution:

1. **asyncpg parameterised fetch** — SQL is sent post-validation; table names in `get_table_sample` are regex-validated before the connection is acquired
2. **`_strip_blocked_columns()`** — removes any column whose name (lowercase) appears in `BLOCKED_OUTPUT_COLUMNS`
3. **`_serialize_row()`** — converts all PostgreSQL native types to JSON-safe Python types
4. **`pii_redactor.redact_records()`** — applies regex PII redaction across all string values in all result rows

The orchestrator applies `_strip_blocked_columns` a second time after receiving data from the gateway. This redundancy ensures sensitive columns are stripped even if the gateway is bypassed or upgraded independently.

The SSL connection to PostgreSQL is handled by `_build_ssl_context()` in `base_server.py`, which converts the `DB_SSL_MODE` setting string to the correct type that asyncpg accepts (`None` / `True` / `ssl.SSLContext`). Passing a raw string like `"require"` directly to asyncpg is a silent failure — the helper ensures the correct type is always used.

---

## Data Serialization

`_serialize_value()` in `base_server.py` handles the full range of PostgreSQL types that asyncpg returns:

| PostgreSQL Type | Python Output |
|---|---|
| `DECIMAL` / `NUMERIC` | `float` |
| `INTEGER`, `FLOAT`, `BOOLEAN` | native Python equivalent |
| `TIMESTAMP`, `DATE`, `TIME` | ISO 8601 string |
| `INTERVAL` | `float` (total seconds) |
| `UUID` | `str` |
| `ARRAY` | `List` (recursive) |
| `JSONB` / `JSON` | `dict` (recursive) |
| `BYTEA` | hex string |
| Range types (`int4range`, etc.) | `{"lower": ..., "upper": ...}` |
| Everything else | `str()` with PII redaction |

---

## Statistical Analysis Layer

`data_analyzer.py` computes statistical metrics on query results before the analyst LLM generates its answer. This grounds the response in verified numbers rather than having the LLM estimate from raw rows.

| Function | What it computes |
|---|---|
| `generate_summary_statistics()` | Shape, dtypes, missing value counts, per-column mean / median / std / Q1 / Q3 / IQR (non-ID numeric cols only — columns named `*_id` or `*_key` are excluded), categorical value distributions |
| `generate_correlation_analysis()` | Pairwise correlations for all non-ID numeric column pairs, ranked by absolute value. Supports `method='pearson'` (default, linear) and `method='spearman'` (rank-based, more robust for skewed BI data such as claim amounts or transaction values) |
| `detect_outliers()` | IQR (Tukey fence) with configurable `iqr_sensitivity` (1.5× standard; 3.0× conservative for right-skewed financial / health data), or Z-score (3-sigma). Returns bounds and outlier values. |
| `generate_time_series_analysis()` | OLS linear regression slope + direction via `scipy.stats.linregress`, R², coefficient of variation (std/mean × 100), half-period comparison. Trend threshold normalised to `max(1% × |mean|, 0.001)` to prevent near-zero-mean series from misclassifying noise as trends. |

`generate_comprehensive_report()` calls summary statistics unconditionally, correlation analysis if ≥ 2 numeric columns are present, and time-series analysis if at least one datetime column is detected. The y-axis column for time-series is selected by business-metric name priority (`amount`, `revenue`, `value`, `cost`, etc.) rather than always taking the first numeric column, which could be a zip code or status code.

---

## Visualization

`visualization_generator.py` selects chart type based on DataFrame column types:

| Condition | Chart type |
|---|---|
| DataFrame has a datetime column | Line chart (datetime on x, first numeric on y) |
| Categorical column + numeric column | Bar chart (or horizontal bar if avg label length > threshold) |
| Two or more numeric columns | Scatter plot |
| Exactly one numeric column | Histogram with mean/median reference lines |
| Fallback | Bar chart using first two columns |

Scatter plots are automatically sampled down to 2,000 points when the DataFrame exceeds this threshold. A `(sample 2,000)` note is appended to the chart title so the user knows sampling occurred.

All charts use a consistent dark BI theme (`VIZ_PALETTE` in `config/constants.py`). `figure_to_base64()` encodes the figure as a data URI PNG and always closes the matplotlib figure in a `finally` block to prevent memory leaks.

`seaborn` is not imported — it was removed after it was confirmed to be unused in this module.

---

## Export Flow

```
User clicks "⬇ CSV" in Chainlit
    │
    ▼
frontend/app.py → _trigger_export("csv", session_id)
    │  POST /api/export/csv  {query, answer, data, sql_queries}
    ▼
backend/main.py → export_result()
    │  pd.DataFrame(data)
    ▼
features/export_manager.py → export_csv_from_dataframe()
    │  _sanitize_filename()  → strips path-traversal chars, adds timestamp
    │  _safe_path()          → Path.is_relative_to() asserts path stays inside export_dir
    │                          (str.startswith() bypass patched — '/tmp/exports_evil'
    │                           would pass a string prefix check for '/tmp/exports')
    │  _write()              → writes to temp/exports/
    ▼
FastAPI → FileResponse (application/octet-stream)
    ▼
frontend/app.py → writes to CHAINLIT_TEMP_EXPORT_DIR
    │  cl.File(path=...) attached to message
    ▼
User downloads file
```

Export files are deleted automatically by `schedule_cleanup()` — an `asyncio` background task started in the FastAPI lifespan. Cleanup runs every `EXPORT_CLEANUP_INTERVAL_HOURS` hours and deletes files older than `EXPORT_CLEANUP_DAYS` days.

---

## LLM Client

`llm_client/ollama_client.py` wraps the Ollama `/api/chat` HTTP endpoint with:

- **Retry logic** via `tenacity`: exponential back-off (1 s → 2 s → 4 s) on connection errors, configurable max attempts
- **`<think>` tag stripping**: DeepSeek-R1 returns chain-of-thought reasoning inside `<think>...</think>` blocks. These are stripped by the LLM client before returning to the caller. The orchestrator also strips them independently as belt-and-suspenders.
- **`ping()`**: Calls `/api/tags` and returns a bool. Used by the FastAPI `/health` endpoint for real Ollama liveness status.
- **Singleton pattern**: `_get_llm()` in `orchestrator.py` returns a single `OllamaClient` instance per process, avoiding httpx client recreation on every agent node call.

---

## Session History

`backend/session_store.py` provides per-session conversation history with the following design:

- Per-session `asyncio.Lock` — concurrent sessions never block each other
- Global lock held only during dict mutation (key creation / deletion), not during data reads
- History is stored in process memory — it does not persist across backend restarts
- With `FASTAPI_WORKERS > 1`, workers do not share session state (use a single worker or switch to Redis for multi-worker setups)
- The `get()` method checks for session existence before acquiring the session lock, preventing phantom session creation on read

The session history is injected into the supervisor and analyst agent prompts via `format_history_for_prompt()` in `config/prompts.py`. History is truncated to `SESSION_CONTEXT_TURNS` most recent turns with per-turn character limits to stay within context window bounds.

---

## Configuration

All runtime configuration is managed through `config/settings.py` (Pydantic `BaseSettings`). Values are read from environment variables, with defaults for everything except passwords.

Notable validators:

| Validator | Effect |
|---|---|
| `validate_ssl_in_production` | Raises `ValidationError` if `environment=production` and `db_ssl_mode=disable` |
| `validate_log_level` | Rejects typos like `WARNIING` |
| `validate_reload_workers` | Raises if `fastapi_reload=True` and `fastapi_workers > 1` |
| `chainlit_allow_origins` field validator | Parses comma-separated string from env var into `List[str]` |
| `backend_url` computed field | Builds URL from `backend_host` and `fastapi_port` — single source of truth |

All operational constants (keyword sets, pattern lists, visualization config, prompt strings) are in `config/constants.py` and `config/prompts.py`. Application code imports from these files rather than defining literals inline — changes to security rules or routing keywords require editing exactly one file.

### Environment variable naming

Settings fields map to environment variables by uppercasing the field name. Key mappings:

| settings.py field | Environment variable |
|---|---|
| `gateway_health_port` | `GATEWAY_HEALTH_PORT` |
| `gateway_finance_port` | `GATEWAY_FINANCE_PORT` |
| `gateway_sales_port` | `GATEWAY_SALES_PORT` |
| `gateway_iot_port` | `GATEWAY_IOT_PORT` |
| `db_query_timeout_seconds` | `DB_QUERY_TIMEOUT_SECONDS` |
| `db_schema_timeout_seconds` | `DB_SCHEMA_TIMEOUT_SECONDS` |
| `db_base_host` | `DB_BASE_HOST` |
