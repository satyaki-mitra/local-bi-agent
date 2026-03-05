# DEPENDENCIES
import uuid
import time
import asyncio
import uvicorn
import structlog
import pandas as pd
from pathlib import Path
from typing import Literal
from fastapi import FastAPI
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from config.settings import settings
from config.schemas import HistoryEntry
from config.schemas import QueryRequest
from config.schemas import QueryResponse
from config.schemas import ExportRequest
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from fastapi.responses import RedirectResponse
from config.schemas import HealthCheckResponse
from backend.session_store import session_store
from llm_client.ollama_client import OllamaClient
from config.schemas import SessionHistoryResponse
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
    orchestrator = AgentOrchestrator()
    cleanup_task = asyncio.create_task(export_manager.schedule_cleanup())

    logger.info("FastAPI backend started")
    yield

    cleanup_task.cancel()
    logger.info("Backend stopped")


# Application
app = FastAPI(title       = "LocalGenBI-Agent API",
              description = "Autonomous Generative BI Platform",
              version     = "1.0.0",
              lifespan    = lifespan,
             )

app.add_middleware(GZipMiddleware, minimum_size = 1000)
app.add_middleware(CORSMiddleware,
                   allow_origins     = settings.chainlit_allow_origins,
                   allow_credentials = "*" not in settings.chainlit_allow_origins,
                   allow_methods     = ["GET", "POST", "DELETE"],        # DELETE added for history clear
                   allow_headers     = ["Content-Type", "Authorization"],
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


# Routes
@app.get("/", include_in_schema = False)
async def root():
    return RedirectResponse(url = "/docs")


@app.get("/health", response_model = HealthCheckResponse)
async def health_check():
    """
    Liveness check
    """
    async with OllamaClient() as client:
        ollama_alive = await client.ping()

    return HealthCheckResponse(status        = "healthy",
                               ollama_status = "running" if ollama_alive else "unreachable",
                               databases     = {},
                               timestamp     = datetime.now(timezone.utc),
                              )


@app.post("/api/query", response_model = QueryResponse)
async def query_endpoint(request: QueryRequest):
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

        if (errors and not answer):
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
                                 visualization     = result.get("visualization"),
                                 reasoning_trace   = result.get("reasoning_trace", []),
                                 execution_time_ms = execution_time,
                                 error             = errors[-1] if errors else None,
                                 databases_queried = result.get("databases_queried", []),
                                 is_cross_db       = result.get("is_cross_db", False),
                                )

        logger.info("Query completed",
                    session_id        = session_id,
                    execution_time_ms = execution_time,
                    databases_queried = result.get("databases_queried", []),
                    is_cross_db       = result.get("is_cross_db", False),
                   )

        return response

    except HTTPException:
        raise

    except Exception as e:
        logger.error("Internal failure", error = str(e))

        raise HTTPException(status_code = 500,
                            detail      = "An internal error occurred. Please try again.",
                           )


# Session History endpoints 
@app.get("/api/sessions/{session_id}/history", response_model = SessionHistoryResponse)
async def get_session_history(session_id: str, last_n: int = 20):
    """
    Retrieve conversation history for a session

    Query params:
    - last_n (default 20): how many of the most recent turns to return
    """
    if not settings.session_history_enabled:
        raise HTTPException(status_code = 404,
                            detail      = "Session history is disabled (SESSION_HISTORY_ENABLED=false).",
                           )

    history_raw     = await session_store.get(session_id, last_n = last_n)

    history_entries = [HistoryEntry(**entry) for entry in history_raw]

    return SessionHistoryResponse(session_id = session_id,
                                  turns      = len(history_entries),
                                  history    = history_entries,
                                 )


@app.delete("/api/sessions/{session_id}/history", status_code = 204)
async def clear_session_history(session_id: str):
    """
    Clear all conversation history for a session and returns 204 No Content on success
    """
    if not settings.session_history_enabled:
        raise HTTPException(status_code = 404,
                            detail      = "Session history is disabled (SESSION_HISTORY_ENABLED=false).",
                           )

    await session_store.clear(session_id)

    logger.info("Session history cleared via API", session_id = session_id)


# Export endpoints
@app.post("/api/export/{format}")
async def export_result(format: Literal["csv", "json", "html", "png", "analysis", "txt"], request: ExportRequest):
    """
    Export query results in the requested format: `format` is validated as a Literal by FastAPI — unknown values are rejected
    at the framework level before reaching Python code
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
                                                        title = query[:50],
                                                       )

        elif (format == "png"):
            if dataframe.empty:
                raise HTTPException(status_code = 400,
                                    detail      = "No data available for visualization",
                                   )
            filepath = export_manager.export_visualization(dataframe, 
                                                           title = query[:50],
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

        return FileResponse(filepath,
                            media_type = "application/octet-stream",
                            filename   = Path(filepath).name,
                           )

    except HTTPException:
        raise

    except Exception as e:
        logger.error("Export failed", 
                     format = format,
                     error  = str(e),
                    )

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