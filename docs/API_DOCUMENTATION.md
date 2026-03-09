# API Reference

Base URL: `http://localhost:8000`

The FastAPI backend (`backend/main.py`) is served via `app.py` which adds the `FrontendMiddleware` (HTML SPA at `/`) and `ExceptionLoggingMiddleware` (full traceback on 500s). All API routes are mounted under `/api/`. All request and response bodies are JSON. All error responses return a `detail` field describing what went wrong.

---

## `GET /health`

Liveness check. Returns the operational status of the backend and verifies that Ollama is reachable.

**Response `200 OK`**
```json
{
  "status": "healthy",
  "ollama_status": "running",
  "databases": {},
  "timestamp": "2025-03-01T12:00:00.000000+00:00"
}
```

| Field | Type | Notes |
|---|---|---|
| `status` | `string` | Always `"healthy"` if the endpoint is reachable |
| `ollama_status` | `string` | `"running"` if Ollama responds to a ping; `"unreachable"` otherwise |
| `databases` | `object` | Reserved; currently empty |
| `timestamp` | ISO 8601 | UTC time of the health check (timezone-aware, `+00:00` suffix) |

---

## `POST /api/query`

Submit a natural-language question. The backend runs the full LangGraph pipeline (supervisor → schema fetch → SQL generation → execution → analyst) and returns the result.

**Request body**
```json
{
  "query": "Which sales rep closed the most deals last quarter?",
  "session_id": "optional-client-session-uuid"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | `string` | ✅ | Must be non-empty after whitespace strip; max length `MAX_QUERY_LENGTH` (default 1000) |
| `session_id` | `string` | ❌ | If omitted, the backend generates one. Reuse the same ID across turns for session history. |

**Response `200 OK`**
```json
{
  "session_id": "3f8a1c2d-...",
  "query": "Which sales rep closed the most deals last quarter?",
  "answer": "Rep 14 closed the most opportunities in Q3 2024, with 8 deals in the Closed Won stage totalling $342,150 in opportunity value.",
  "sql_executed": [
    "SELECT * FROM (SELECT s.rep_name, COUNT(o.opportunity_id) AS closed_deals, SUM(o.opportunity_value) AS total_value FROM sales_reps s JOIN leads l ON ... ) _limited LIMIT 10000"
  ],
  "data": [
    {"rep_name": "Rep 14", "closed_deals": 8, "total_value": 342150.00}
  ],
  "metrics": {
    "row_count": 20,
    "avg_total_value": 158000.0,
    "data_shape": "20 rows × 3 cols",
    "trend_direction": "upward",
    "period_change_pct": 14.2,
    "outlier_columns": ["total_value"],
    "top_correlation": "closed_deals ↔ total_value (0.91)"
  },
  "visualization": {
    "type": "auto",
    "title": "Which sales rep closed the most deals last quarter?",
    "data": {"row_count": 20},
    "base64_image": "data:image/png;base64,iVBORw..."
  },
  "reasoning_trace": [
    "[Supervisor] Routed to: sales",
    "[Schema] Fetched schema for sales_db",
    "[SQL] Generated SQL on attempt 1",
    "[Execute] 20 rows returned",
    "[Analyst] Generated answer"
  ],
  "execution_time_ms": 4820,
  "query_confidence": 0.9,
  "retry_count": 0,
  "errors": [],
  "error": null,
  "databases_queried": ["sales"],
  "is_cross_db": false
}
```

| Field | Type | Notes |
|---|---|---|
| `session_id` | `string` | Echo of the session ID (generated or provided). Pass this back in subsequent requests for multi-turn conversation. |
| `query` | `string` | Echo of the input query after PII redaction |
| `answer` | `string` | Natural-language analyst response. For conversational queries (greetings, general knowledge) this is a direct response with no SQL or data attached. |
| `sql_executed` | `string[]` | List of SQL statements that executed without errors. Empty if execution failed or if the query was conversational. SQL is wrapped in an outer LIMIT subquery by the validator. |
| `data` | `object[]` | Serialised query result rows; blocked columns stripped; PII redacted. Empty for conversational queries. |
| `metrics` | `object` | Statistical summary computed by DataAnalyzer. Always includes `row_count`. Additional keys when data is present: `data_shape`, `trend_direction`, `period_change_pct`, `outlier_columns`, `top_correlation`, and base aggregation metrics (avg/total/max/min of the primary numeric column). Empty `{}` for conversational queries. |
| `metrics.data_shape` | `string` | `"N rows × M cols"` — shape of the result DataFrame |
| `metrics.trend_direction` | `string` | `"upward"` / `"downward"` / `"flat"` — OLS trend direction (present only when a datetime column exists) |
| `metrics.period_change_pct` | `float` | Percentage change between first and second half of the time series (present only when trend analysis runs) |
| `metrics.outlier_columns` | `string[]` | Column names containing IQR outliers (present only when numeric columns exist) |
| `metrics.top_correlation` | `string` | Formatted string of the highest absolute correlation (present only when ≥2 numeric columns exist) |
| `visualization` | `object \| null` | Null if the smart viz gate blocked it (1-row or string-only results), or no chart could be generated. |
| `visualization.base64_image` | `string` | PNG encoded as a data URI; ready for `<img src="...">` |
| `reasoning_trace` | `string[]` | Human-readable log of pipeline steps: supervisor routing decision, schema fetch, SQL attempts, analyst notes. Populated on every query. |
| `query_confidence` | `float` | Confidence score 0.0–1.0 set by analyst_agent. `1.0` for conversational responses (no SQL involved). Deductions: −0.2 per retry (max −0.4), −0.3 if errors present, −0.5 if row_count is 0. |
| `retry_count` | `integer` | Number of SQL generation retries performed (0 if first attempt succeeded). |
| `errors` | `string[]` | All error messages from the agent pipeline, in order. Empty if no errors occurred. A non-empty list does not always mean `answer` is empty — the analyst may produce a degraded response explaining the error. |
| `error` | `string \| null` | Last error message from `errors` (convenience field). `null` if no errors. |
| `execution_time_ms` | `integer` | Total wall-clock time including all LLM inference calls |
| `databases_queried` | `string[]` | List of database domain(s) that were actually queried. Empty for conversational queries. |
| `is_cross_db` | `bool` | True if the query was routed to two domains simultaneously (fan-out cross-DB path). |

**Note on conversational queries:** When the supervisor routes to `"conversational"` (greetings, identity questions, general knowledge), the response will have `sql_executed: []`, `data: []`, `visualization: null`, `retry_count: 0`, `errors: []`, `metrics: {}`, `query_confidence: 1.0`, and `databases_queried: []`. The `answer` field contains the conversational response. `reasoning_trace` will contain a single entry: `"[Supervisor] Conversational query — bypassing SQL pipeline"`.

**Response `400 Bad Request`**

Returned when the LangGraph pipeline produced errors and no usable answer:
```json
{
  "detail": {
    "type": "graph_error",
    "errors": ["Validation error: SQL contains prohibited keyword DROP"]
  }
}
```

**Response `503 Service Unavailable`**

Returned when the backend is starting up and the orchestrator is not yet initialised:
```json
{
  "detail": "Service not ready. Please retry."
}
```

**Response `500 Internal Server Error`**

Returned on unexpected backend failures. The raw error is not exposed to the client (it is logged server-side by `ExceptionLoggingMiddleware`):
```json
{
  "detail": "An internal error occurred. Please try again."
}
```

---

## `GET /api/sessions/{session_id}/history`

Retrieve conversation history for a session.

**Path parameter:** `session_id` — the session UUID

**Query parameters:**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `last_n` | `integer` | `20` | Number of most-recent turns to return |
| `memory_type` | `string` | `"short_term"` | `"short_term"` — episodic turns only; `"all"` — also includes long-term memory summary if supported by the session store |

**Response `200 OK`**
```json
{
  "session_id": "3f8a1c2d-...",
  "turns": 5,
  "history": [
    {
      "role": "user",
      "content": "Which sales rep closed the most deals?",
      "domain": "sales",
      "row_count": 20,
      "sql": "SELECT ...",
      "timestamp": 1709300000.0
    }
  ]
}
```

**Response `404 Not Found`**

Returned if session history is disabled in settings:
```json
{
  "detail": "Session history is disabled."
}
```

---

## `DELETE /api/sessions/{session_id}/history`

Clear the conversation history for a session.

**Path parameter:** `session_id` — the session UUID

**Query parameters:**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `memory_type` | `string` | `"all"` | `"short_term"` — clear episodic turns only; `"long_term"` — clear persistent facts only; `"all"` — clear everything |

**Response `204 No Content`**

Returned on success. No response body.

**Response `404 Not Found`**

Returned if session history is disabled in settings:
```json
{
  "detail": "Session history is disabled."
}
```

---

## `GET /api/sessions/{session_id}/stats`

Return usage statistics for a session.

**Path parameter:** `session_id` — the session UUID

**Response `200 OK`**
```json
{
  "session_id": "3f8a1c2d-...",
  "turn_count": 12,
  "domains_queried": ["sales", "finance"],
  "last_activity": 1709300000.0,
  "short_term_turns": 12,
  "long_term_facts": 5
}
```

Falls back to a minimal response if the session store does not implement `get_stats()`:
```json
{
  "session_id": "3f8a1c2d-...",
  "turn_count": 12,
  "note": "Extended stats unavailable in this session_store version"
}
```

**Response `404 Not Found`**

Returned if session history is disabled:
```json
{
  "detail": "Session history is disabled."
}
```

---

## `POST /api/export/{format}`

Generate a downloadable export file from a previous query result. Returns the file as an `application/octet-stream` binary response with a `Content-Disposition: attachment; filename=...` header.

**Path parameter**

| `format` | Content | Requires |
|---|---|---|
| `csv` | Tabular data as comma-separated values | Non-empty `data` |
| `xlsx` | Excel workbook with branded headers, alternating row shading, freeze panes, and auto-fit columns | Non-empty `data` |
| `json` | Full result package: query, answer, SQL, reasoning trace, data | Nothing |
| `html` | Styled HTML table | Non-empty `data` |
| `png` | Matplotlib dark-theme chart image | Non-empty `data` |
| `analysis` | Plain-text statistical analysis report (summary / correlation / trend) | Non-empty `data` |
| `txt` | Plain-text query + answer summary | Nothing |

Unknown format values are rejected by FastAPI with `422 Unprocessable Entity` before reaching application code.

**Request body**
```json
{
  "query": "Which sales rep closed the most deals last quarter?",
  "answer": "Rep 14 closed the most...",
  "data": [
    {"rep_name": "Rep 14", "closed_deals": 8, "total_value": 342150.00}
  ],
  "sql_queries": ["SELECT ..."],
  "reasoning_trace": ["[Supervisor] Routed to: sales", "..."]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | `string` | ✅ | |
| `answer` | `string` | ✅ | |
| `data` | `object[]` | ❌ | Required for `csv`, `xlsx`, `html`, `png`, `analysis` |
| `sql_queries` | `string[]` | ❌ | Included in `json` export |
| `reasoning_trace` | `string[]` | ❌ | Included in `json` export |

**Response `200 OK`**

Binary file download. Example response headers:
```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="query_result_20250301_142233.csv"
```

**Response `400 Bad Request`**

Returned when required data is missing for the requested format:
```json
{
  "detail": "No tabular data available for CSV export"
}
```

**Response `500 Internal Server Error`**
```json
{
  "detail": "Export generation failed. Please try again."
}
```

---

## Gateway Endpoints

Each DB gateway (`gateway-health`, `gateway-finance`, `gateway-sales`, `gateway-iot`) exposes two endpoints. These are **internal** — the FastAPI backend calls them; client applications should not call them directly.

### `POST /gateway`

Execute a database operation.

**Request body**
```json
{
  "method": "query_database",
  "params": {
    "sql": "SELECT COUNT(*) FROM claims"
  }
}
```

| `method` | Params | Notes |
|---|---|---|
| `query_database` | `{"sql": "..."}` | Runs validated SQL through full security pipeline; returns serialised rows |
| `get_schema` | `{"table": null}` | Returns full public schema; `table` filters to one table |
| `get_table_sample` | `{"table": "...", "limit": 5}` | Returns up to `limit` sample rows (max 100) |

**Response `200 OK`**
```json
{
  "success": true,
  "data": [...],
  "row_count": 42,
  "execution_time_ms": 18
}
```

**Response `200 OK` (error case)**

Gateway errors always return HTTP 200 with `success: false`. HTTP 5xx is reserved for unhandled exceptions:
```json
{
  "success": false,
  "error": "Internal gateway error"
}
```

### `GET /health`

Liveness probe used by Docker Compose `healthcheck` and the backend orchestrator.

```json
{
  "status": "healthy",
  "server": "health-gateway",
  "domain": "health",
  "database": "health_db",
  "pool": "connected"
}
```

`status` is `"degraded"` if the connection pool is not yet initialised or has zero available connections. `pool` will be `"disconnected"` in the same case.

---

## Error Code Summary

| HTTP Code | When |
|---|---|
| 400 | Query pipeline error with no usable answer; export format requires data that was not provided |
| 404 | Session history is disabled (session endpoints) |
| 422 | Request body failed Pydantic validation (invalid field types, missing required fields, unknown export format literal) |
| 500 | Unexpected internal error; raw error detail is not exposed to the caller (logged server-side) |
| 503 | Backend is starting up — orchestrator not yet initialised; retry after a few seconds |