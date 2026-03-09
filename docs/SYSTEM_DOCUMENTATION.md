# Application Architecture

This document describes the internal design of LocalGenBI-Agent: the agent pipeline, data flow, dual memory system, DataAnalyzer integration, security layers, statistical analysis layer, and the export subsystem.

---

## Agent Pipeline

Every query passes through a six-node LangGraph state machine. The state is a typed Python dict (`BIState`) that flows through each node. The first conditional edge after `supervisor_agent` determines whether the query enters the SQL pipeline or bypasses it entirely via the conversational path.

```
User query (browser → app.py → FastAPI)
    │
    ▼
┌──────────────────┐
│  process_query() │  Loads short-term history (last N turns)
│  (orchestrator)  │  Loads long-term memory (facts, preferred domains, key entities)
│                  │  PII-redacts the raw query
│                  │  Builds long_term_context string for BIState
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  supervisor      │  Three-phase routing:
│  _agent          │  Phase 1 — Conversational keyword fast-path (0 ms, no LLM)
│                  │  Phase 2 — Domain keyword scan (no LLM)
│                  │  Phase 3 — LLM confirmation with history + long-term context
└────────┬─────────┘
         │
         ├─── ["conversational"] ───────────────────────────────────────────┐
         │                                                                   │
         │ ["health"|"finance"|"sales"|"iot"]                               ▼
         ▼                                               ┌───────────────────────────┐
┌──────────────────┐                                     │  conversational           │
│  fetch_schema    │  Calls /gateway → get_schema        │  _agent                   │
│                  │  Injects live schema into state      │                           │
└────────┬─────────┘                                     │  Single LLM call.         │
         │                                               │  Uses CONVERSATIONAL_     │
         ▼                                               │  AGENT_PROMPT + history.  │
┌──────────────────┐  ┌──────────────────────────────┐  │  No schema, no SQL,       │
│  sql_agent       │  │  Guardrail layer 1            │  │  no DB call.              │
│                  │──│  SQLValidator:                 │  └───────────────┬───────────┘
│                  │  │  - keyword blocklist (O(1))    │                  │
│                  │  │  - prohibited pattern regex    │                  ▼
│                  │  │  - LIMIT injection             │  session_store.append_short_term()
│                  │  │                                │  session_store.save_long_term()
│                  │  │  Guardrail layer 2             │  (conversational turns still saved)
│                  │  │  Orchestrator pattern check    │                  │
└────────┬─────────┘  └──────────────────────────────┘                  ▼
         │                                                              END
         │  [conditional edge — retry on error, up to MAX_AGENT_RETRIES]
         │
         ▼
┌──────────────────┐
│  execute_sql     │  Posts to /gateway → query_database
│                  │  Strips BLOCKED_OUTPUT_COLUMNS from result
│                  │  On cross-DB: partial results returned on
│                  │  per-domain failure (does not abort full query)
└────────┬─────────┘
         │
         │  [conditional edge — retry on DB error, up to MAX_AGENT_RETRIES]
         │
         ▼
┌──────────────────┐
│  analyst_agent   │  Reconstructs DataFrame from state data
│                  │  DataAnalyzer.generate_comprehensive_report()
│                  │  _build_rich_metrics() → augments metrics dict
│                  │  _build_analyst_data_summary() → stats context for LLM prompt
│                  │  Calls LLM with data table + rich metrics to generate answer
│                  │  _clean_llm_answer() strips boilerplate + caps bullets
│                  │  Computes query_confidence (deductions for retries / errors / zero rows)
│                  │  Smart viz gate — skips chart for 1-row / string-only results
│                  │  Generates visualization if gate passes
└────────┬─────────┘
         │
         ▼
session_store.append_short_term()   — save BI turn to short-term history
session_store.save_long_term()      — heuristic fact/domain/entity extraction
         │
         ▼
QueryResponse (FastAPI) → app.py → HTML SPA → User
```

### State fields

| Field | Type | Set by |
|---|---|---|
| `query` | `str` | API layer (PII-redacted at entry) |
| `session_id` | `str` | API layer |
| `planned_databases` | `List[str]` | supervisor_agent (`["conversational"]` or domain list) |
| `is_cross_db` | `bool` | supervisor_agent |
| `current_schema` | `str` | fetch_schema (single-DB path) |
| `cross_db_schemas` | `Dict[str, str]` | fetch_schema (cross-DB path) |
| `sql` | `str` | sql_agent |
| `data` | `List[Dict]` | execute_sql |
| `cross_db_results` | `Dict[str, List]` | execute_sql (cross-DB path) |
| `metrics` | `Dict` | analyst_agent (includes `data_shape`, `trend_direction`, `outlier_columns`, `top_correlation`) |
| `answer` | `str` | analyst_agent or conversational_agent |
| `errors` | `List[str]` | Any node that encounters an error |
| `retry_count` | `int` | sql_agent / execute_sql |
| `visualization` | `Dict \| None` | analyst_agent |
| `reasoning_trace` | `List[str]` | Every node (human-readable pipeline step log) |
| `query_confidence` | `float` | analyst_agent (0.0–1.0) |
| `conversation_history` | `List[Dict]` | Loaded by `process_query()` before `graph.ainvoke()` |
| `long_term_context` | `str` | Loaded by `process_query()` — pre-formatted `[LONG-TERM MEMORY]` block injected into supervisor + analyst prompts |

---

## Supervisor Routing

The supervisor uses a **three-phase approach** to classify every query. Phases execute from fastest to slowest and short-circuit at the first definitive result.

**Phase 1 — Conversational keyword fast-path (zero LLM cost):**
`CONVERSATIONAL_INTENT_KEYWORDS` in `config/prompts.py` is a tuple of ~50 phrases covering greetings ("hi", "hello", "hey"), identity questions ("who are you", "what can you do"), wellbeing queries ("how are you"), thanks/compliments, and test pings. The supervisor checks whether the lowercased query matches or starts with any of these before making any LLM call. A match immediately sets `planned_databases = ["conversational"]` and routes to `conversational_agent`, bypassing the entire SQL pipeline with zero added latency.

**Phase 2 — Domain keyword pre-check (zero LLM cost):**
A configurable dict maps domain names to keyword lists (`SUPERVISOR_ROUTING_KEYWORDS` in `config/constants.py`). Every domain whose keywords appear in the query is added to `pre_targets`. All matching domains are collected — not just the first — to support cross-DB scenarios.

**Phase 3 — LLM confirmation:**
Used only when Phase 1 and Phase 2 are inconclusive. The supervisor sends the query plus the list of valid route values (including `"conversational"`) to the LLM and asks it to return a JSON array. Conversation history and the `long_term_context` block are both injected here via `format_history_for_prompt()` so the LLM can resolve referential follow-ups and apply user preferences from previous sessions. If the LLM returns an unparseable or empty response, the system falls back to `pre_targets`, then to the configured default (`SUPERVISOR_DEFAULT_DATABASE`).

**`"conversational"` sentinel:** Both Phase 1 and the LLM (Phase 3) can produce this value. The `route_after_supervisor()` conditional edge checks for it and routes to `conversational_agent → END`. The SQL pipeline nodes are never entered.

**Cross-DB pair validation:** When `ENABLE_CROSS_DB_JOINS=true`, the supervisor validates the planned database pair against `ALLOWED_CROSS_DB_PAIRS` (a frozenset-of-frozensets in `config/constants.py`). Only explicitly whitelisted pairs are permitted.

The three-phase design means a complete LLM failure degrades gracefully: conversational queries are still handled by Phase 1, and BI queries fall back to keyword routing rather than failing entirely.

---

## Conversational Agent

`conversational_agent` is a dedicated LangGraph node that handles all non-BI queries. It is reached only when `supervisor_agent` sets `planned_databases = ["conversational"]`.

**What it handles:**
- Greetings: "hi", "hello", "good morning", "hey"
- Identity/capability questions: "who are you", "what can you do", "what databases do you have"
- Wellbeing: "how are you", "are you there", "ping"
- Thanks/compliments: "thanks", "great job", "awesome"
- General knowledge and math: "what is 2+2", "capital of France"
- Follow-up questions about the last result visible in history

**What it does:**
Runs a single LLM call with `CONVERSATIONAL_AGENT_PROMPT` (from `config/prompts.py`). The prompt knows the system's four data domains and their capabilities, and contains numbered response rules for each conversation type. Conversation history is injected the same way as in `analyst_agent` so the agent can reference the last result if the user asks about it.

**What it does NOT do:**
- No schema fetch
- No SQL generation or validation
- No database call
- No visualization
- No export data

**Session history:** Conversational turns are still appended to `session_store` via `append_short_term()` so follow-up BI queries can reference "what I just said". The `domain` field is stored as `"conversational"` and `row_count` as `0`. Long-term memory is not updated for conversational turns — only successful BI queries with `row_count > 0` contribute to long-term memory.

---

## SQL Validation

`sql_agent` sends the live schema (fetched from the gateway in the previous node) together with the domain system prompt and user query to the LLM. The LLM is instructed to output raw PostgreSQL with no markdown or explanation.

After stripping think-tags and markdown fences, the SQL passes through two independent validation layers:

1. **SQLValidator** (`guardrails/sql_validator.py`)
   - Keyword blocklist: rejects any SQL containing `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, `TRUNCATE`, etc. (frozenset, case-normalised, comment-stripped before check to prevent `DR--comment\nOP` bypass)
   - Prohibited pattern check: rejects `PG_SLEEP`, `INFORMATION_SCHEMA`, `COPY`, `LO_*`, `XP_*`
   - LIMIT injection: wraps the entire query as a subquery: `SELECT * FROM ({sql}) _limited LIMIT {max_rows}`. This prevents the subquery bypass where an inner `LIMIT 99999` could escape the outer limit check.

2. **Orchestrator pattern check** (`orchestrator.py`)
   - A second independent check against `PROHIBITED_SQL_PATTERNS` from `config/constants.py`

If either check fails, the error is appended to `state["errors"]`, `retry_count` is incremented, and the conditional edge routes back to `sql_agent`. After `MAX_AGENT_RETRIES` failures, the pipeline routes directly to `analyst_agent` which returns a graceful error message.

**DB-level errors also trigger retries via `execute_sql`.** When the gateway returns a DB error (e.g. `column must appear in GROUP BY`, `missing FROM-clause entry`), `execute_sql` returns early with `retry_count` incremented. `route_after_execute` then routes back to `sql_agent` with the error injected into the prompt context so the LLM can attempt a correction. The `errors` list is cleared on a clean execution so only the most recent error reaches the analyst on final failure.

---

## DB Gateway Security Pipeline

Each gateway applies these operations in order after query execution:

1. **asyncpg parameterised fetch** — SQL is sent post-validation; table names in `get_table_sample` are regex-validated before the connection is acquired
2. **`_strip_blocked_columns()`** — removes any column whose name (lowercase) appears in `BLOCKED_OUTPUT_COLUMNS`
3. **`_serialize_row()`** — converts all PostgreSQL native types to JSON-safe Python types
4. **`pii_redactor.redact_records()`** — applies regex PII redaction across all string values in all result rows

The orchestrator applies `_strip_blocked_columns` a second time after receiving data from the gateway. This redundancy ensures sensitive columns are stripped even if the gateway is bypassed or upgraded independently.

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
| `detect_all_outliers()` | Calls `detect_outliers()` across all non-ID numeric columns, returning a list of column names that contain outliers. Used to populate `outlier_columns` in the metrics dict. |
| `generate_time_series_analysis()` | OLS linear regression slope + direction via `scipy.stats.linregress`, R², coefficient of variation (std/mean × 100), half-period comparison. Trend threshold normalised to `max(1% × \|mean\|, 0.001)` to prevent near-zero-mean series from misclassifying noise as trends. |

`generate_comprehensive_report()` calls summary statistics unconditionally, correlation analysis if ≥ 2 numeric columns are present, and time-series analysis if at least one datetime column is detected. The y-axis column for time-series is selected by business-metric name priority (`amount`, `revenue`, `value`, `cost`, etc.) via `_pick_ts_value_col()` rather than always taking the first numeric column, which could be a zip code or status code.

---

## DataAnalyzer Integration in Orchestrator

Two orchestrator helper functions bridge the `data_analyzer` output into the agent pipeline:

### `_build_rich_metrics(df, base_metrics)`

Called by `analyst_agent` after computing base metrics (row count, avg/total/max/min of the primary numeric column). Augments the metrics dict with flat key-value pairs derived from the comprehensive report:

| Key added | Source | Example |
|---|---|---|
| `data_shape` | report shape | `"42 rows × 5 cols"` |
| `trend_direction` | time-series direction | `"upward"` |
| `period_change_pct` | half-period comparison | `12.4` |
| `outlier_columns` | `detect_all_outliers()` | `["claim_amount"]` |
| `top_correlation` | ranked correlation list | `"revenue ↔ deals (0.87)"` |

These keys are returned in the `metrics` field of the API response and rendered directly by the frontend's `renderMetrics()` function — no frontend changes are needed when new metrics keys are added.

### `_build_analyst_data_summary(report)`

Converts the nested comprehensive report to a compact text block injected into the analyst LLM prompt via `{data_summary}`. The block includes per-column statistics (mean / median / std / distribution shape / skewness), time-series direction + R² + period delta, top 3 correlations, and outlier-flagged columns. This replaces the previous approach of having the LLM eyeball raw rows — the model now generates answers from statistically-grounded, pre-computed facts.

---

## Analyst Agent

`analyst_agent` reconstructs a DataFrame from `state["data"]`, calls `DataAnalyzer` for statistical metrics, then calls the LLM with the data table, rich metrics, and original question to produce a natural-language answer.

**`_clean_llm_answer()` post-processing:** The raw LLM output passes through this helper before it is stored in state:
- `_BOILERPLATE_PREFIX_RE` strips opening phrases that add no information: "Here is the answer to the user's question:", "Based on the dataset, the answer is:", "According to the data,", and ~10 similar variants.
- "Business Insight:" / "Key Takeaway:" trailing sections are stripped — insights are expected to be woven into the answer naturally.
- Bullet list cap: if the LLM enumerates more than `_ANSWER_MAX_BULLETS` (default 5) individual items, only the first 5 are kept and a single-line export nudge is appended: *"…and N more — use ⬇ CSV to download all results."*

**`query_confidence` computation:**
A score of `1.0` is computed first, then deductions are applied:
- `−0.2` per retry (up to `−0.4` maximum)
- `−0.3` if any errors were present in the pipeline
- `−0.5` if `row_count == 0`
- `1.0` fixed for conversational responses (no SQL involved)

**PII handling:** PII redaction is applied to the *query input* (`process_query()`, before any LLM call) and to *DB result rows* (at the gateway and again in the orchestrator). It is **not** applied to the analyst's text answer — applying it to the answer corrupted legitimate data the user explicitly requested (email addresses rendered as `@.com`, phone numbers as `[PHONE]`).

**Smart visualization gate:** `auto_visualize()` is skipped when:
- `row_count <= 1` — a single aggregate (e.g. `SELECT COUNT(*)`) produces a meaningless 1-bar chart.
- All result columns are strings (`_viz_str_only`) — lookup results like email lists or names have no numeric axis to chart.

When the gate passes, `auto_visualize()` returns `Optional[Tuple[Figure, str]]`; the tuple is unpacked into figure and chart caption. The figure is base64-encoded to a PNG data URI.

| Condition | Chart type |
|---|---|
| DataFrame has a datetime column | Line chart (datetime on x, first numeric on y) |
| Categorical + numeric, multiple series detected | Multi-series line chart |
| Categorical column + numeric column | Bar chart (or horizontal bar if avg label length > threshold) |
| Two or more numeric columns | Scatter plot |
| One numeric column + categorical, ≤8 categories | Donut chart |
| One numeric column + categorical, stacked | Stacked bar chart |
| Two categorical + one numeric | Heatmap |
| Exactly one numeric column | Histogram with mean/median reference lines |
| Fallback | Bar chart using first two columns |

Scatter plots are automatically sampled down to 2,000 points when the DataFrame exceeds this threshold. A `(sample 2,000)` note is appended to the chart title. All charts use a consistent dark BI theme (`VIZ_PALETTE` in `config/constants.py`). `figure_to_base64()` always closes the matplotlib figure in a `finally` block to prevent memory leaks.

---

## Dual Memory System

`backend/session_store.py` maintains two independent memory tiers per session.

### Short-Term Memory

Short-term memory holds the last N episodic conversation turns (configurable via `SESSION_CONTEXT_TURNS`). It is used to resolve referential follow-up queries ("filter those", "compare to last time") and to provide recency context to the supervisor and analyst.

| Method | Purpose |
|---|---|
| `append_short_term(session_id, entry)` | Append one conversation turn (query, answer, domain, row_count, sql) |
| `get_short_term(session_id, last_n)` | Return last N turns, newest first |
| `get_for_prompt(session_id)` | Return formatted string for LLM injection (LAST RESULT SNAPSHOT + history list) |

Short-term entries have the shape:
```python
{
    "role"      : "user" | "assistant",
    "content"   : str,
    "domain"    : str,       # "health" | "finance" | "sales" | "iot" | "conversational"
    "row_count" : int,
    "sql"       : str | None,
    "timestamp" : float,
}
```

### Long-Term Memory

Long-term memory holds cross-session persistent knowledge extracted heuristically from successful BI query answers. It is loaded once per query in `process_query()` and formatted into a `[LONG-TERM MEMORY]` block injected into supervisor (Phase 3) and analyst prompts via `long_term_context` in `BIState`.

| Method | Purpose |
|---|---|
| `get_long_term(session_id)` | Return the long-term memory dict for a session |
| `save_long_term(session_id, lt_dict)` | Persist the updated long-term memory dict |

Long-term memory structure:
```python
{
    "preferred_domains" : List[str],       # domains queried most frequently
    "key_entities"      : Dict[str, str],  # named entities mentioned in past queries
    "key_facts"         : List[str],       # rolling window of 10 most recent BI facts
}
```

`_extract_facts_for_long_term()` is called by `analyst_agent` after every successful BI query (`row_count > 0`, `domain != "conversational"`). It:
1. Adds the queried domain to `preferred_domains` (deduped, ordered by recency)
2. Extracts the first sentence of the analyst answer as a key fact if it is between 10 and 120 characters (rolling window of 10 — oldest facts are dropped)
3. Does not update long-term memory if `query_confidence < 0.5` or if `errors` is non-empty, to avoid storing facts from failed or degraded queries

**Current limitations:** Fact extraction is regex-heuristic — it does not detect semantic contradictions (a new fact about Q3 revenue will not overwrite an older, now-stale one). The store lives in process memory and does not survive backend restarts. Redis or a persistent KV store is required for true cross-session long-term memory.

### Session Store API

| Method | Notes |
|---|---|
| `get_short_term(session_id, last_n)` | Returns `List[Dict]`. Backward-compatible — falls back to `get()` if not present. |
| `append_short_term(session_id, entry)` | Backward-compatible — falls back to `append()` if not present. |
| `get_long_term(session_id)` | Returns `Dict`. Returns `{}` if session has no long-term memory. |
| `save_long_term(session_id, lt_dict)` | No-op if method not present on the store (compat shim). |
| `get_for_prompt(session_id)` | Returns formatted LAST RESULT SNAPSHOT + history list string for LLM injection. |
| `get_stats(session_id)` | Returns `{turn_count, domains_queried, last_activity, ...}`. |
| `clear(session_id, memory_type)` | `memory_type`: `"short_term"` \| `"long_term"` \| `"all"` (default). |

All store methods use per-session `asyncio.Lock` — concurrent sessions never block each other. The global lock is held only during dict mutation (key creation/deletion), not during data reads.

---

## Unified Server Architecture

`app.py` at the project root is the single entry point for the web application. It imports `backend.main:app` (the pure FastAPI application) and mounts two Starlette middleware layers on top of it:

### FrontendMiddleware

Intercepts all `GET /` requests and returns `frontend/index.html` as a `FileResponse`. All other paths (including `/api/...`, `/health`, `/static/...`) are passed through to the FastAPI backend unchanged. If `frontend/index.html` does not exist, returns a styled 404 HTML page pointing to the expected path.

### ExceptionLoggingMiddleware

Wraps every request in a try/except. On any unhandled exception from the backend:
- Prints the full traceback to stdout (for server log visibility)
- Logs the exception type and args
- Returns a sanitised `{"detail": "Internal server error: ...", "type": "ExceptionType"}` JSON response with HTTP 500

Raw exception details are **never** forwarded to the client — only the exception type string.

### Why this split

`backend/main.py` is intentionally kept free of all frontend concerns:
- No `StaticFiles` mounting
- No `HTMLResponse` imports
- No path detection for `/` routes
- No `FRONTEND_DIR` or `INDEX_HTML` references

This makes `backend.main:app` independently deployable as a pure API service (e.g. behind a reverse proxy that handles static file serving). `app.py` is the glue layer for the integrated local development setup.

**Entry point note:** Always run `uvicorn app:app` (not `uvicorn backend.main:app`) when using the unified server. Running `backend.main:app` directly bypasses both middleware layers — the frontend will not be served and exceptions will not be caught.

```bash
# Correct — frontend + API + exception logging
python app.py
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# Raw API only (no frontend, no exception middleware)
uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

---

## Frontend (HTML SPA)

`frontend/index.html` is a single-file HTML+CSS+JS application (~1,200 lines). It is served by `FrontendMiddleware` at `GET /` and communicates with the backend exclusively via `fetch()` calls to `/api/...`.

**Session management:** `sessionId` is generated client-side as a UUID and persisted in `localStorage`. It is sent with every `POST /api/query` request, enabling multi-turn conversation continuity.

**Key UI components:**
- Sidebar with starter chips (💊 Patient Overview, 💰 Revenue by Month, 📈 Top Sales Reps, ❤️ IoT Heart Rate, 🔗 Health + Finance, 🔄 Subscription Churn) and health status indicator
- Chat message area with assistant bubbles containing answer text, inline chart, data table (collapsible), metrics panel, and export buttons
- Single-line input bar with send button

**Response rendering pipeline** (client-side):
1. `answer` → rendered as markdown-like text in the message bubble
2. `visualization.base64_image` → `<img>` element rendered directly (PNG data URI)
3. `data` → collapsible `<table>` with column headers auto-derived from first row keys
4. `metrics` → key-value grid rendered by `renderMetrics()` — any key added to the metrics dict appears automatically
5. `sql_executed` → collapsible code block
6. `reasoning_trace` → collapsible step log

**Export buttons** (`⬇ CSV`, `⬇ Excel`, `⬇ JSON`, `⬇ Analysis`) are shown only when `data` is non-empty. Each button triggers `POST /api/export/{format}` with the current query's data.

---

## Export Flow

```
User clicks "⬇ CSV" in the HTML SPA
    │
    ▼
frontend/index.html → fetch POST /api/export/csv {query, answer, data, sql_queries}
    │
    ▼
app.py middleware → FastAPI backend/main.py → export_result()
    │  pd.DataFrame(data)
    ▼
features/export_manager.py → export_csv_from_dataframe()
    │  _sanitize_filename()  → strips path-traversal chars, adds timestamp
    │  _safe_path()          → Path.is_relative_to() asserts path stays inside export_dir
    │  _write() / _write_bytes()  → writes to temp/exports/
    ▼
FastAPI → FileResponse (application/octet-stream)
    ▼
Browser → file download dialog
```

**Supported export formats:**

| Format | Handler | Notes |
|---|---|---|
| `csv` | `export_csv_from_dataframe()` | Standard comma-separated |
| `xlsx` | `export_xlsx()` | openpyxl: branded headers, alternating row shading, freeze panes, auto-fit columns |
| `json` | `export_json()` | Full result package: query + answer + SQL + reasoning trace + data |
| `html` | `export_html_table()` | Styled HTML table with `escape=True` (XSS prevention) |
| `png` | `export_visualization()` | Matplotlib dark-theme chart |
| `analysis` | `export_analysis_report()` | Plain-text statistical analysis report (summary / correlation / trend) |
| `txt` | `export_simple_text()` | Plain-text query + answer summary |

Export files are deleted automatically by `schedule_cleanup()` — an `asyncio` background task started in the FastAPI lifespan. Cleanup runs every `EXPORT_CLEANUP_INTERVAL_HOURS` hours and deletes files older than `EXPORT_CLEANUP_DAYS` days.

---

## LLM Client

`llm_client/ollama_client.py` wraps the Ollama `/api/chat` HTTP endpoint with:

- **Retry logic** via `tenacity`: exponential back-off (1 s → 2 s → 4 s) on connection errors, configurable max attempts
- **`<think>` tag stripping**: Implemented for model-swap compatibility. If `OLLAMA_MODEL` is changed to a reasoning model such as DeepSeek-R1, the stripping logic requires no code changes. The orchestrator also strips them independently as belt-and-suspenders.
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

**History injection:** `format_history_for_prompt()` in `config/prompts.py` renders the last `SESSION_CONTEXT_TURNS` turns into two sections:

1. **LAST RESULT SNAPSHOT** — a structured box showing the most recent turn's domain, row count, and truncated answer. This is the primary anchor for referential follow-up queries ("filter those", "show me the top ones", "compare to last time"). The LLM reads this first.
2. **Full history list** (most recent first) — all turns with Q/A pairs for richer multi-turn context. Answers are truncated to 300 chars to keep the injected block under ~1,500 tokens.

Both `supervisor_agent` (Phase 3 only — for routing referential queries to the correct domain) and `analyst_agent` (for interpreting follow-up questions) receive this history block via `{history_context}` in their prompts. The `long_term_context` block is appended separately as `[LONG-TERM MEMORY]` after the history block. When history is empty, `format_history_for_prompt()` returns `""` and the placeholder disappears silently.

Conversational turns (domain `"conversational"`, row count `0`) are saved to short-term history so the user can reference "what I just said" in a subsequent BI query, but they do not update long-term memory.

---

## Configuration

All runtime configuration is managed through `config/settings.py` (Pydantic `BaseSettings`). Values are read from environment variables, with defaults for everything except passwords.

Notable validators:

| Validator | Effect |
|---|---|
| `validate_ssl_in_production` | Raises `ValidationError` if `environment=production` and `db_ssl_mode=disable` |
| `validate_log_level` | Rejects typos like `WARNIING` |
| `validate_reload_workers` | Raises if `fastapi_reload=True` and `fastapi_workers > 1` |
| `cors_allow_origins` field validator | Parses comma-separated string from env var into `List[str]` |
| `backend_url` computed field | Builds URL from `backend_host` and `fastapi_port` — single source of truth |

All operational constants (keyword sets, pattern lists, visualization config, prompt strings) are in `config/constants.py` and `config/prompts.py`. Application code imports from these files rather than defining literals inline — changes to security rules or routing keywords require editing exactly one file.

### Environment variable naming

Settings fields map to environment variables by uppercasing the field name. Key mappings:

| settings.py field | Environment variable |
|---|---|
| `fastapi_host` | `FASTAPI_HOST` |
| `fastapi_port` | `FASTAPI_PORT` |
| `fastapi_reload` | `FASTAPI_RELOAD` |
| `gateway_health_port` | `GATEWAY_HEALTH_PORT` |
| `gateway_finance_port` | `GATEWAY_FINANCE_PORT` |
| `gateway_sales_port` | `GATEWAY_SALES_PORT` |
| `gateway_iot_port` | `GATEWAY_IOT_PORT` |
| `db_query_timeout_seconds` | `DB_QUERY_TIMEOUT_SECONDS` |
| `db_schema_timeout_seconds` | `DB_SCHEMA_TIMEOUT_SECONDS` |
| `db_base_host` | `DB_BASE_HOST` |
| `session_context_turns` | `SESSION_CONTEXT_TURNS` |
| `enable_cross_db_joins` | `ENABLE_CROSS_DB_JOINS` |