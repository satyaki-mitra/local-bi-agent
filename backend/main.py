# DEPENDENCIES
import uuid
import time
import asyncio
import inspect
import uvicorn
import structlog
import pandas as pd
from typing import Any
from typing import Dict
from typing import List
from pathlib import Path
from typing import Literal
from typing import Optional
from fastapi import FastAPI
from datetime import datetime
from fastapi import HTTPException
from config.settings import settings
from config.schemas import HistoryEntry
from config.schemas import QueryRequest
from config.schemas import QueryResponse
from config.schemas import ExportRequest
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from config.schemas import HealthCheckResponse
from backend.session_store import session_store
from config.schemas import SessionHistoryResponse
from llm_client.ollama_client import OllamaClient
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from features.export_manager import export_manager
from backend.orchestrator import AgentOrchestrator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest


logger                                  = structlog.get_logger()
orchestrator : AgentOrchestrator | None = None


# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator

    orchestrator  = AgentOrchestrator()
    cleanup_task  = asyncio.create_task(export_manager.schedule_cleanup())
 
    # Start session store background cleanup if the store supports it
    store_cleanup = getattr(session_store, "schedule_cleanup", None)
    store_task    = asyncio.create_task(store_cleanup()) if store_cleanup else None

    logger.info("LocalGenBI backend started")
    yield

    cleanup_task.cancel()
    if store_task:
        store_task.cancel()

    logger.info("Backend stopped")


# Application

app = FastAPI(title       = "LocalGenBI-Agent API",
              description = "Autonomous Generative BI Platform",
              version     = "1.0.0",
              lifespan    = lifespan,
             )

app.add_middleware(GZipMiddleware, minimum_size = 1000)

# CORS — tighten allow_origins in production via settings
_cors_origins = getattr(settings, "cors_allow_origins", getattr(settings, "allow_origins", ["*"]))

app.add_middleware(CORSMiddleware,
                   allow_origins     = _cors_origins,
                   allow_credentials = "*" not in _cors_origins,
                   allow_methods     = ["GET", "POST", "DELETE", "OPTIONS"],
                   allow_headers     = ["Content-Type", "Authorization", "X-Request-ID"],
                  )


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        request_id                       = str(uuid.uuid4())

        structlog.contextvars.bind_contextvars(request_id = request_id)
        
        response                         = await call_next(request)
        
        structlog.contextvars.clear_contextvars()
        
        response.headers["X-Request-ID"] = request_id
        
        return response

app.add_middleware(RequestIDMiddleware)


#  ROUTES
@app.get("/health", response_model = HealthCheckResponse)
async def health_check():
    """
    Liveness + dependency check: Called by the frontend sidebar on load to display Ollama / guardrail status
    """
    async with OllamaClient() as client:
        ollama_alive = await client.ping()

    return HealthCheckResponse(status        = "healthy",
                               ollama_status = "running" if ollama_alive else "unreachable",
                               databases     = {},
                               timestamp     = datetime.utcnow(),
                              )


@app.post("/api/query", response_model = QueryResponse)
async def query_endpoint(request: QueryRequest):
    """
    Main BI query endpoint: accepts a natural-language question and an optional session_id

    Returns the full structured response including:
      - answer           (LLM narrative)
      - sql_executed     (list of SQL strings)
      - visualization    (base64_image + chart_type)
      - data             (raw result rows)
      - metrics          (statistical summary, trend, outliers, correlation)
      - reasoning_trace  (pipeline step log)
      - query_confidence (0.0–1.0)
      - retry_count
      - execution_time_ms
    """
    if orchestrator is None:
        raise HTTPException(status_code = 503,
                            detail      = "Service not ready. Please retry.",
                           )

    session_id = request.session_id or str(uuid.uuid4())
    start_time = time.time()

    logger.info("Query received",
                query      = request.query,
                session_id = session_id,
               )

    try:
        result         = await orchestrator.process_query(query      = request.query,
                                                          session_id = session_id,
                                                         )

        execution_time = int((time.time() - start_time) * 1000)
        answer         = result.get("answer", "")
        errors         = result.get("errors", [])

        if errors and not answer:
            raise HTTPException(status_code = 400,
                                detail      = {"type"   : "graph_error", 
                                               "errors" : errors,
                                              },
                               )

        if not answer:
            answer = "No records found for your query."

        response = QueryResponse(session_id        = session_id,
                                 query             = request.query,
                                 answer            = answer,
                                 sql_executed      = result.get("sql_executed", []),
                                 data              = result.get("data", []),
                                 metrics           = result.get("metrics", {}),
                                 visualization     = result.get("visualization"),
                                 reasoning_trace   = result.get("reasoning_trace", []),
                                 execution_time_ms = execution_time,
                                 error             = errors[-1] if errors else None,
                                 errors            = errors,
                                 databases_queried = result.get("databases_queried", []),
                                 is_cross_db       = result.get("is_cross_db", False),
                                 query_confidence  = result.get("query_confidence", 1.0),
                                 retry_count       = result.get("retry_count", 0),
                                )

        logger.info("Query completed",
                    session_id        = session_id,
                    execution_time_ms = execution_time,
                    databases_queried = result.get("databases_queried", []),
                    is_cross_db       = result.get("is_cross_db", False),
                    confidence        = result.get("query_confidence", 1.0),
                   )

        return response

    except HTTPException:
        raise

    except Exception as exc:
        logger.error("Internal failure", error = str(exc))
        raise HTTPException(status_code = 500,
                            detail      = "An internal error occurred. Please try again.",
                           )


# Session history endpoints
@app.get("/api/sessions/{session_id}/history", response_model = SessionHistoryResponse)
async def get_session_history(session_id: str, last_n: int = 20, memory_type: str = "short_term"):
    """
    Retrieve conversation history for a session

    Query params:
      last_n (default 20) — number of most-recent turns to return
      memory_type (short_term | all) — 'all' also returns long-term memory summary
    """
    if not settings.session_history_enabled:
        raise HTTPException(status_code = 404,
                            detail      = "Session history is disabled.",
                           )

    fn_st           = (getattr(session_store, "get_short_term", None) or getattr(session_store, "get", None))

    history_raw     = await fn_st(session_id, last_n = last_n) if fn_st else []
    history_entries = [HistoryEntry(**entry) for entry in history_raw]

    response        = SessionHistoryResponse(session_id = session_id,
                                             turns      = len(history_entries),
                                             history    = history_entries,
                                            )

    # Optionally attach long-term memory when requested
    if (memory_type == "all"):
        fn_lt = getattr(session_store, "get_long_term", None)

        if fn_lt:
            try:
                lt = await fn_lt(session_id)
                if hasattr(response, "long_term_memory"):
                    response.long_term_memory = lt

            except Exception:
                pass

    return response


@app.delete("/api/sessions/{session_id}/history", status_code = 204)
async def clear_session_history(session_id: str, memory_type: str = "all"):
    """
    Clear conversation history for a session: returns 204 No Content on success

    memory_type: 'short_term' | 'long_term' | 'all' (default)
    """
    if not settings.session_history_enabled:
        raise HTTPException(status_code = 404,
                            detail      = "Session history is disabled.",
                           )

    fn = getattr(session_store, "clear", None)

    if fn:
        try:
            sig = inspect.signature(fn)
            
            if ("memory_type" in sig.parameters):
                await fn(session_id, memory_type = memory_type)

            else:
                await fn(session_id)
        except Exception as exc:
            logger.warning("Session clear failed", 
                           error = str(exc),
                          )

    logger.info("Session history cleared",
                session_id  = session_id,
                memory_type = memory_type,
               )


@app.get("/api/sessions/{session_id}/stats")
async def get_session_stats(session_id: str):
    """
    Return usage statistics for a session and falls back to a minimal turn-count response if the store doesn't support get_stats()
    """
    if not settings.session_history_enabled:
        raise HTTPException(status_code = 404,
                            detail      = "Session history is disabled.",
                           )

    fn_stats = getattr(session_store, "get_stats", None)
    
    if fn_stats:
        try:
            stats = await fn_stats(session_id)
            return JSONResponse(content = {"session_id" : session_id, **stats})
    
        except Exception as exc:
            logger.warning("Session stats failed", error = str(exc))

    # Minimal fallback
    fn_get = (getattr(session_store, "get_short_term", None) or getattr(session_store, "get", None))
    turns  = 0

    if fn_get:
        try:
            history = await fn_get(session_id, last_n = 1000)
            turns   = len(history)
        
        except Exception:
            pass

    return JSONResponse(content = {"session_id" : session_id,
                                   "turn_count" : turns,
                                   "note"       : "Extended stats unavailable in this session_store version",
                                  }
                       )


# ── Export endpoints ──────────────────────────────────────────────────────────

@app.post("/api/export/{format}")
async def export_result(format: Literal["csv", "json", "html", "png", "analysis", "txt", "xlsx"], request: ExportRequest,):
    """
    Export query results in the requested format

    Supported formats:
      csv       — tabular CSV                  (requires data)
      json      — full result JSON             (always available)
      html      — styled HTML table            (requires data)
      png       — chart PNG                    (requires data)
      analysis  — statistical analysis .txt    (requires data)
      txt       — plain-text summary
      xlsx      — Excel spreadsheet            (requires data)
    """
    try:
        query     = request.query
        answer    = request.answer
        data      = request.data
        sql_list  = request.sql_queries
        dataframe = pd.DataFrame(data) if data else pd.DataFrame()

        if (format == "csv"):
            if dataframe.empty:
                raise HTTPException(status_code = 400,
                                    detail      = "No tabular data available for CSV export",
                                   )

            filepath = export_manager.export_csv_from_dataframe(dataframe)

        elif (format == "json"):
            filepath = export_manager.export_json(query, answer, data, sql_list, [])

        elif (format == "html"):
            if not data:
                raise HTTPException(status_code = 400,
                                    detail      = "No data available for HTML export",
                                   )

            filepath = export_manager.export_html_table(data, 
                                                        title = query,
                                                       )

        elif (format == "png"):
            if dataframe.empty:
                raise HTTPException(status_code = 400,
                                    detail      = "No data available for visualization",
                                   )

            filepath = export_manager.export_visualization(dataframe, 
                                                           title = query,
                                                          )

            if not filepath:
                raise HTTPException(status_code = 500,
                                    detail      = "Visualization generation failed",
                                   )

        elif (format == "analysis"):
            if dataframe.empty:
                raise HTTPException(status_code = 400,
                                    detail      = "No data available for analysis",
                                   )

            filepath = export_manager.export_analysis_report(dataframe, 
                                                             output_format = "txt",
                                                            )

        elif (format == "txt"):
            filepath = export_manager.export_simple_text(query, answer)

        elif (format == "xlsx"):
            if dataframe.empty:
                raise HTTPException(status_code = 400,
                                    detail      = "No tabular data available for Excel export",
                                   )

            fn_xlsx = getattr(export_manager, "export_xlsx", None)

            if fn_xlsx is None:
                logger.warning("export_xlsx not found; falling back to CSV")
                filepath = export_manager.export_csv_from_dataframe(dataframe)

            else:
                filepath = fn_xlsx(dataframe, title = query[:50])

        return FileResponse(filepath,
                            media_type = "application/octet-stream",
                            filename   = Path(filepath).name,
                           )

    except HTTPException:
        raise

    except Exception as exc:
        logger.error("Export failed", format = format, error = str(exc))
        raise HTTPException(status_code = 500,
                            detail      = "Export generation failed. Please try again.",
                           )

# Entry point
if __name__ == "__main__":
    uvicorn.run("backend.main:app",
                host   = settings.fastapi_host,
                port   = settings.fastapi_port,
                reload = settings.fastapi_reload,
               )