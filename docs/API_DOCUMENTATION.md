# API Reference

Base URL: `http://localhost:8001`

The FastAPI backend exposes three endpoint groups: system health, query execution, and result export. All request and response bodies are JSON. All error responses return a `detail` field describing what went wrong.

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
    {"rep_name": "Rep 14", "closed_deals": 8, "total_value": 342150.00},
    "..."
  ],
  "visualization": {
    "type": "auto",
    "title": "Which sales rep closed the most deals last quarter?",
    "data": {"row_count": 20, "avg_total_value": 158000.0},
    "base64_image": "data:image/png;base64,iVBORw..."
  },
  "reasoning_trace": [],
  "execution_time_ms": 4820,
  "error": null
}
```

| Field | Type | Notes |
|---|---|---|
| `session_id` | `string` | Echo of the session ID (generated or provided). Pass this back in subsequent requests for multi-turn conversation. |
| `query` | `string` | Echo of the input query after PII redaction |
| `answer` | `string` | Natural-language analyst response; PII-redacted |
| `sql_executed` | `string[]` | List of SQL statements that executed without errors. Empty if execution failed. SQL is wrapped in an outer LIMIT subquery by the validator. |
| `data` | `object[]` | Serialised query result rows; blocked columns stripped; PII redacted |
| `visualization` | `object \| null` | Null if visualisation is disabled or no appropriate chart could be generated. Scatter plots are sampled to 2,000 points. |
| `visualization.base64_image` | `string` | PNG encoded as a data URI; ready for `<img src="...">` |
| `reasoning_trace` | `string[]` | Reserved; always empty in the current implementation |
| `execution_time_ms` | `integer` | Total wall-clock time including LLM inference |
| `error` | `string \| null` | Last error message from the agent pipeline, if any. A non-null error does not always mean `answer` is empty — the analyst may produce a degraded response explaining the error. |

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

Returned on unexpected backend failures. The raw error is not exposed:
```json
{
  "detail": "An internal error occurred. Please try again."
}
```

---

## `POST /api/export/{format}`

Generate a downloadable export file from a previous query result. Returns the file as an `application/octet-stream` binary response with a `Content-Disposition: attachment; filename=...` header.

**Path parameter**

| `format` | Content | Requires |
|---|---|---|
| `csv` | Tabular data as comma-separated values | Non-empty `data` |
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
  "reasoning_trace": []
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | `string` | ✅ | |
| `answer` | `string` | ✅ | |
| `data` | `object[]` | ❌ | Required for `csv`, `html`, `png`, `analysis` |
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

## `DELETE /api/sessions/{session_id}/history`

Clear the conversation history for a session. Called by the Chainlit frontend when the user types `/clear`.

**Response `200 OK`**
```json
{
  "cleared": true,
  "session_id": "3f8a1c2d-..."
}
```

**Response `404 Not Found`**

Returned if the session ID does not exist:
```json
{
  "detail": "Session not found"
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
| 400 | Query pipeline error; export format requires data that was not provided |
| 404 | Session not found (DELETE history endpoint) |
| 422 | Request body failed Pydantic validation (invalid field types, missing required fields, unknown export format literal) |
| 500 | Unexpected internal error; raw error detail is not exposed to the caller |
| 503 | Backend is starting up — orchestrator not yet initialised; retry after a few seconds |