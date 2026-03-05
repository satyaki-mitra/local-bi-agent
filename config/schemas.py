# DEPENDENCIES
from typing import Any
from typing import List
from typing import Dict
from pydantic import Field
from typing import Optional
from datetime import datetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import field_validator
from config.constants import ChartType
from config.constants import DatabaseType


class QueryRequest(BaseModel):
    """
    Request model for the /api/query endpoint
    """
    query                : str           = Field(..., max_length = 1000, description = "Natural language query")
    session_id           : Optional[str] = Field(None, description = "Session identifier for conversation history")
    enable_visualization : bool          = Field(True, description = "Whether to generate visualizations")


    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")

        return v.strip()


class PIIRedactionResult(BaseModel):
    """
    Result of a PII redaction operation
    """
    sanitized_text     : str
    was_redacted       : bool
    patterns_triggered : List[str] = []


class VisualizationData(BaseModel):
    """
    Visualization metadata and data
    """
    type         : ChartType
    title        : str
    data         : Dict[str, Any]
    base64_image : Optional[str] = None


class QueryResponse(BaseModel):
    """
    Response model for the /api/query endpoint
    """
    session_id        : str
    query             : str
    answer            : str
    sql_executed      : List[str]                   = Field(default_factory = list)
    data              : List[Dict[str, Any]]        = Field(default_factory = list)
    visualization     : Optional[VisualizationData] = None
    reasoning_trace   : List[str]                   = Field(default_factory = list)
    error             : Optional[str]               = None
    databases_queried : List[str]                   = Field(default_factory = list, description = "Domain(s) that were queried, e.g. ['health', 'finance']")
    is_cross_db       : bool                        = Field(default = False, description = "True when results were merged from >1 domain")
    execution_time_ms : int


class HistoryEntry(BaseModel):
    """
    One turn of session conversation history: stored compactly — answer is truncated to 1000 chars in session_store.py
    """
    turn       : int
    timestamp  : str
    query      : str
    answer     : str
    sql        : str        = ""
    domain     : str        = "unknown"
    row_count  : int        = 0


class SessionHistoryResponse(BaseModel):
    """
    Response model for GET /api/sessions/{session_id}/history
    """
    session_id : str
    turns      : int
    history    : List[HistoryEntry]


class SessionHistory(BaseModel):
    """
    Session conversation history (legacy model — kept for backward compatibility)
    """
    session_id : str
    queries    : List[Dict[str, Any]]
    created_at : datetime
    updated_at : datetime


class DatabaseSchema(BaseModel):
    """
    Database schema information
    """
    database : DatabaseType
    tables   : List[Dict[str, Any]]


class HealthCheckResponse(BaseModel):
    """
    Health check response
    """
    status        : str
    ollama_status : str
    databases     : Dict[str, str]
    timestamp     : datetime


class AgentState(BaseModel):
    """
    State object passed between agents in LangGraph
    """
    model_config = ConfigDict(arbitrary_types_allowed = True)

    query             : str
    session_id        : str
    current_agent     : str                      = ""
    databases_queried : List[str]                = Field(default_factory = list)
    reasoning_trace   : List[str]                = Field(default_factory = list)
    collected_data    : Dict[str, Any]           = Field(default_factory = dict)
    sql_queries       : List[str]                = Field(default_factory = list)
    error_log         : List[str]                = Field(default_factory = list)
    retry_count       : int                      = Field(default = 0, ge = 0)
    is_complete       : bool                     = False
    final_answer      : str                      = ""
    visualization     : Optional[Dict[str, Any]] = None


class SQLValidationResult(BaseModel):
    """
    Result of SQL validation
    """
    is_valid      : bool
    error_message : Optional[str] = None
    sanitized_sql : Optional[str] = None


class DbToolCall(BaseModel):
    """
    DB tool invocation
    """
    tool_name  : str
    arguments  : Dict[str, Any]
    database   : DatabaseType
    request_id : Optional[str] = None


class DbToolResult(BaseModel):
    """
    Result from DB tool execution
    """
    success           : bool
    data              : Optional[List[Dict[str, Any]]] = None
    row_count         : int                            = 0
    execution_time_ms : int                            = 0
    error             : Optional[str]                  = None
    request_id        : Optional[str]                  = None


class EvaluationMetrics(BaseModel):
    """
    Evaluation metrics from DeepEval
    """
    overall_score    : float                = Field(..., ge = 0.0, le = 1.0)
    tool_correctness : float                = Field(..., ge = 0.0, le = 1.0)
    sql_accuracy     : float                = Field(..., ge = 0.0, le = 1.0)
    faithfulness     : float                = Field(..., ge = 0.0, le = 1.0)
    failed_cases     : List[Dict[str, Any]] = Field(default_factory = list)


class EvaluationRequest(BaseModel):
    """
    Request for evaluation endpoint
    """
    test_suite : str       = "golden_dataset"
    metrics    : List[str] = Field(default_factory = lambda: ["tool_correctness", "sql_accuracy", "faithfulness"])


class CodeExecutionRequest(BaseModel):
    """
    Request to execute Python code in sandbox
    """
    code    : str
    context : Dict[str, Any] = Field(default_factory = dict)
    timeout : int            = Field(default = 60, ge = 1, le = 300)


class CodeExecutionResult(BaseModel):
    """
    Result of code execution
    """
    success           : bool
    output            : Optional[Any] = None
    stdout            : str           = ""
    stderr            : str           = ""
    execution_time_ms : int           = 0
    error             : Optional[str] = None


class ExportRequest(BaseModel):
    """
    Request model for export endpoints
    """
    query           : str                  = "exported_query"
    answer          : str                  = ""
    data            : List[Dict[str, Any]] = Field(default_factory = list)
    sql_queries     : List[str]            = Field(default_factory = list)
    reasoning_trace : List[str]            = Field(default_factory = list)