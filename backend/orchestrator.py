# DEPENDENCIES
import re
import json
import httpx
import structlog
import pandas as pd
from typing import Any
from typing import List
from typing import Dict
from typing import Optional
from typing import TypedDict
from langgraph.graph import END
from config.settings import settings
from langgraph.graph import StateGraph
from config.prompts import IOT_AGENT_PROMPT
from config.prompts import SALES_AGENT_PROMPT
from config.prompts import HEALTH_AGENT_PROMPT
from config.constants import THINK_TAG_PATTERN
from config.constants import VIZ_TITLE_MAX_LEN
from config.prompts import ANALYST_AGENT_PROMPT
from config.prompts import FINANCE_AGENT_PROMPT
from backend.session_store import session_store
from llm_client.ollama_client import OllamaClient
from guardrails.pii_redaction import pii_redactor
from guardrails.sql_validator import sql_validator
from config.prompts import SUPERVISOR_SYSTEM_PROMPT
from config.constants import BLOCKED_OUTPUT_COLUMNS
from config.constants import ALLOWED_CROSS_DB_PAIRS
from config.constants import PROHIBITED_SQL_PATTERNS
from config.constants import ANALYST_DATA_TABLE_ROWS
from config.prompts import CROSS_DB_SQL_AGENT_PROMPT
from config.prompts import format_history_for_prompt
from config.constants import ANALYST_METRICS_ID_EXCLUDE
from config.constants import SUPERVISOR_ROUTING_KEYWORDS
from config.constants import SUPERVISOR_DEFAULT_DATABASE
from features.visualization_generator import viz_generator


# Setup Logging
logger = structlog.get_logger()


# LangGraph State
class BIState(TypedDict):
    query                : str
    session_id           : str
    planned_databases    : List[str]
    current_schema       : str
    sql                  : str
    data                 : List[Dict[str, Any]]
    metrics              : Dict[str, Any]
    answer               : str
    errors               : List[str]
    retry_count          : int
    visualization        : Optional[Dict[str, Any]]
    conversation_history : List[Dict]               # loaded before graph.ainvoke(), never mutated inside
    cross_db_schemas     : Dict[str, str]           # domain → schema text
    cross_db_sqls        : Dict[str, str]           # domain → validated SQL
    cross_db_results     : Dict[str, List[Dict]]    # domain → rows
    is_cross_db          : bool


# Routing maps
PROMPT_MAP : Dict[str, str] = {"health"  : HEALTH_AGENT_PROMPT,
                               "finance" : FINANCE_AGENT_PROMPT,
                               "sales"   : SALES_AGENT_PROMPT,
                               "iot"     : IOT_AGENT_PROMPT,
                              }

PORT_MAP   : Dict[str, int] = {"health"  : settings.gateway_health_port,
                               "finance" : settings.gateway_finance_port,
                               "sales"   : settings.gateway_sales_port,
                               "iot"     : settings.gateway_iot_port,
                              }

DB_HOST    : str            = settings.db_base_host
_GATEWAY_ENDPOINT           = "/gateway"


# Guardrail helpers 
def _strip_blocked_columns(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove any column whose normalised name appears in BLOCKED_OUTPUT_COLUMNS: applied to every result set before it reaches the analyst agent or the API
    """
    if not rows:
        return rows

    cols_in_result  = set(rows[0].keys())
    blocked_present = {col for col in cols_in_result if col.lower().replace(" ", "_") in BLOCKED_OUTPUT_COLUMNS}

    if not blocked_present:
        return rows

    logger.warning("Blocking sensitive columns from result set",
                   columns = list(blocked_present),
                  )

    return [{k: v for k, v in row.items() if k not in blocked_present} for row in rows]


def _check_sql_for_prohibited_patterns(sql: str) -> Optional[str]:
    """
    Belt-and-suspenders check on top of SQLValidator: returns an error message if the SQL matches any PROHIBITED_SQL_PATTERNS, else None
    """
    sql_upper = sql.upper()

    for pattern in PROHIBITED_SQL_PATTERNS:
        if re.search(pattern, sql_upper):
            return f"SQL contains prohibited pattern: {pattern}"

    return None


def _strip_think_tags(text: str) -> str:
    """
    Remove DeepSeek-R1 <think>…</think> blocks from any LLM output: belt-and-suspenders: OllamaClient.complete() strips these too
    """
    return re.sub(THINK_TAG_PATTERN, "", text, flags = re.DOTALL).strip()


# Graph Nodes
async def supervisor_agent(state: BIState) -> BIState:
    """
    Analyse the user query and decide which database(s) to target

    Two-phase routing:
    1. Keyword pre-check (all `if`, not `elif`) — catches multi-domain queries
    2. LLM confirmation                         — catches queries where domain is implied
    
    - Injects conversation history so the LLM can resolve referential queries
    - Validates cross-DB pairs against ALLOWED_CROSS_DB_PAIRS allowlist
    """
    query                  = state["query"]
    query_lower            = query.lower()

    # Keyword pre-check
    pre_targets: List[str] = list()

    for db, keywords in SUPERVISOR_ROUTING_KEYWORDS.items():
        if any(kw in query_lower for kw in keywords):
            pre_targets.append(db)

    valid_dbs              = list(SUPERVISOR_ROUTING_KEYWORDS.keys())
    valid_dbs_json         = json.dumps(valid_dbs)
    default_db_str         = SUPERVISOR_DEFAULT_DATABASE.value

    # Inject history into prompt
    history_context        = ""

    if (settings.session_history_enabled and state.get("conversation_history")):
        history_context = format_history_for_prompt(history   = state["conversation_history"],
                                                    max_turns = settings.session_context_turns,
                                                   )

    prompt             = (f"{SUPERVISOR_SYSTEM_PROMPT.format(history_context = history_context)}\n\n"
                          f'User Query: "{query}"\n\n'
                          "Determine which databases are required. "
                          f"Available: {valid_dbs_json}. "
                          "Respond with ONLY a valid JSON array. "
                          f'Example: ["{default_db_str}"]'
                         )

    raw_response       = await _llm_complete(prompt)
    cleaned_response   = _strip_think_tags(raw_response)

    targets: List[str] = []

    try:
        match = re.search(r"\[.*\]", cleaned_response, re.DOTALL)

        if match:
            parsed = json.loads(match.group(0))

            for item in parsed:
                if isinstance(item, list) and item:
                    item = item[0]

                if isinstance(item, str):
                    db = item.lower().replace("_db", "").strip()

                    if db in valid_dbs:
                        targets.append(db)

    except json.JSONDecodeError:
        logger.warning("Supervisor LLM returned invalid JSON",
                       raw = cleaned_response,
                      )

    # Fallback hierarchy: LLM → keyword → configured default
    if not targets:
        targets = pre_targets

    if not targets:
        logger.warning("Routing failed; using configured default",
                       default = default_db_str,
                      )
        targets = [default_db_str]

    # Cross-DB validation: deduplicate while preserving order
    seen           = set()
    unique_targets = list()

    for t in targets:
        if t not in seen:
            seen.add(t)
            unique_targets.append(t)

    targets = unique_targets

    if (len(targets) > 1):
        if not settings.enable_cross_db_joins:
            # Feature flag off → silently trim to first domain only
            logger.info("Cross-DB joins disabled; trimming to single domain",
                        kept    = targets[0],
                        dropped = targets[1:],
                       )

            targets = [targets[0]]

        else:
            # Validate the pair is in the allowlist
            pair = frozenset(targets[:2])

            if pair not in ALLOWED_CROSS_DB_PAIRS:
                logger.warning("Cross-DB pair not in allowlist; trimming to first domain",
                               pair = list(pair),
                              )

                targets = [targets[0]]

            # Enforce max_cross_db_domains cap
            targets = targets[:settings.max_cross_db_domains]

    logger.info("Supervisor routing decision",
                databases = targets,
               )

    return {**state,
            "planned_databases" : targets,
           }


async def fetch_schema(state: BIState) -> BIState:
    """
    Retrieve live DB schema(s) via the gateway server

    - Iterates all planned_databases and stores each schema in cross_db_schemas dict
    - Primary schema still goes to current_schema for backward compatibility with the single-DB sql_agent path
    """
    cross_db_schemas: Dict[str, str] = dict()

    for database in state["planned_databases"]:
        target_port = PORT_MAP.get(database, settings.gateway_sales_port)

        try:
            async with httpx.AsyncClient(timeout = settings.db_schema_timeout_seconds) as client:
                response = await client.post(f"http://{DB_HOST}:{target_port}{_GATEWAY_ENDPOINT}",
                                             json = {"method" : "get_schema", 
                                                     "params" : {},
                                                    },
                                            )
                response.raise_for_status()

            result = response.json()
            schema = (json.dumps(result.get("tables", {}), indent = 4)
                      if result.get("success")
                      else "Schema unavailable.")

        except Exception as e:
            logger.error("Schema fetch failed",
                         database = database,
                         error    = str(e),
                        )
            schema = "Schema fetch failed"

        cross_db_schemas[database] = schema

    primary_schema = cross_db_schemas.get(state["planned_databases"][0], "Schema unavailable.")

    return {**state,
            "current_schema"  : primary_schema,
            "cross_db_schemas": cross_db_schemas,
           }


async def sql_agent(state: BIState) -> BIState:
    """
    Generate domain-specific SQL, validated through two independent layers:
    1. SQLValidator           — syntax check + read-only enforcement + LIMIT injection
    2. Orchestrator guardrail — prohibited-pattern check (belt-and-suspenders)

    (cross-DB path): Generates separate SQL per domain:
    a. First domain : standard domain prompt
    b. Second domain: CROSS_DB_SQL_AGENT_PROMPT with column hints from first SQL

    - Validated SQLs are stored in cross_db_sqls.

    - History is intentionally NOT injected into sql_agent — doing so causes the LLM to hallucinate CTEs and column aliases from previous queries
    """
    databases     = state["planned_databases"]
    is_cross_db   = len(databases) > 1
    cross_db_sqls = dict()

    error_context = (f"\nPREVIOUS ERROR TO FIX: {state['errors'][-1]}\nDo not repeat this error." if (state["errors"] and state["retry_count"] > 0) else "")

    # Single-DB path
    if not is_cross_db:
        database      = databases[0]
        domain_prompt = PROMPT_MAP.get(database, SALES_AGENT_PROMPT)

        prompt  = (f"{domain_prompt}\n\n"
                   f"CRITICAL: You are connected ONLY to the {database.upper()} database.\n"
                   "Use ONLY the tables and columns shown in the schema below.\n\n"
                   f"{database.upper()} DATABASE SCHEMA:\n{state['current_schema']}\n\n"
                   f"User Request: {state['query']}\n"
                   f"{error_context}\n\n"
                   "Output ONLY valid PostgreSQL. No markdown, no explanations."
                  )

        raw_sql = await _llm_complete(prompt)
        sql     = _strip_think_tags(raw_sql)
        sql     = sql.replace("```postgresql", "").replace("```sql", "").replace("```", "").strip()

        validation = sql_validator.validate(sql)

        if not validation.is_valid:
            err = f"Validation error: {validation.error_message}"
            logger.warning("SQL rejected by validator", 
                           error = err,
                          )

            return {**state, 
                    "errors"      : state["errors"] + [err], 
                    "retry_count" : state["retry_count"] + 1,
                   }

        pattern_error = _check_sql_for_prohibited_patterns(validation.sanitized_sql)

        if pattern_error:
            err = f"Orchestrator guardrail: {pattern_error}"

            logger.warning("SQL blocked by orchestrator guardrail", 
                           error = err,
                          )

            return {**state, 
                    "errors"      : state["errors"] + [err], 
                    "retry_count" : state["retry_count"] + 1,
                   }

        logger.info("SQL validated and accepted", database = database)

        return {**state,
                "sql"          : validation.sanitized_sql,
                "errors"       : [],
                "cross_db_sqls": {database: validation.sanitized_sql},
               }

    # Cross-DB path
    first_sql = ""

    for idx, database in enumerate(databases):
        schema        = state["cross_db_schemas"].get(database, "Schema unavailable.")
        domain_prompt = PROMPT_MAP.get(database, SALES_AGENT_PROMPT)

        if (idx == 0):
            # First domain: standard prompt
            prompt = (f"{domain_prompt}\n\n"
                      f"CRITICAL: You are connected ONLY to the {database.upper()} database.\n"
                      "Use ONLY the tables and columns shown in the schema below.\n\n"
                      f"{database.upper()} DATABASE SCHEMA:\n{schema}\n\n"
                      f"User Request: {state['query']}\n"
                      f"{error_context}\n\n"
                      "Output ONLY valid PostgreSQL. No markdown, no explanations."
                     )
        else:
            # Second domain: include column hints from first SQL
            other_db   = databases[0]
            # Extract rough column list from first SQL (best-effort, used as hints only)
            col_hints  = re.findall(r'\b(\w+)\b', first_sql.split("FROM")[0]) if "FROM" in first_sql.upper() else []
            col_hints  = [c for c in col_hints if c.upper() not in ("SELECT", "DISTINCT", "AS") and len(c) > 2]
            col_sample = ", ".join(col_hints[:10]) or "various columns"

            prompt     = CROSS_DB_SQL_AGENT_PROMPT.format(target_database = database.upper(),
                                                          other_database  = other_db.upper(),
                                                          other_columns   = col_sample,
                                                          schema          = schema,
                                                          query           = state["query"],
                                                         )

        raw_sql    = await _llm_complete(prompt)
        sql        = _strip_think_tags(raw_sql)
        sql        = sql.replace("```postgresql", "").replace("```sql", "").replace("```", "").strip()

        validation = sql_validator.validate(sql)

        if not validation.is_valid:
            err = f"Validation error [{database}]: {validation.error_message}"

            logger.warning("Cross-DB SQL rejected", 
                           database = database, 
                           error    = err,
                          )

            return {**state, 
                    "errors"      : state["errors"] + [err], 
                    "retry_count" : state["retry_count"] + 1,
                   }

        pattern_error = _check_sql_for_prohibited_patterns(validation.sanitized_sql)

        if pattern_error:
            err = f"Orchestrator guardrail [{database}]: {pattern_error}"

            logger.warning("Cross-DB SQL blocked", 
                           database = database, 
                           error    = err,
                          )

            return {**state, 
                    "errors"      : state["errors"] + [err], 
                    "retry_count" : state["retry_count"] + 1,
                   }

        cross_db_sqls[database] = validation.sanitized_sql

        if (idx == 0):
            first_sql = validation.sanitized_sql

        logger.info("Cross-DB SQL validated", 
                    database = database,
                   )

    # Primary sql stored for backward-compat (first domain's SQL)
    primary_sql = cross_db_sqls.get(databases[0], "")

    return {**state,
            "sql"          : primary_sql,
            "errors"       : [],
            "cross_db_sqls": cross_db_sqls,
           }


async def execute_sql(state: BIState) -> BIState:
    """
    Execute validated SQL via the gateway server: output guardrails (blocked columns) applied before storing results

    Also, executes each SQL against its domain's gateway, stores per-domain rows in cross_db_results, marks is_cross_db = True
    when multiple domains are present
    """
    databases     = state["planned_databases"]
    cross_db_sqls = state.get("cross_db_sqls", {})

    # If cross_db_sqls is empty (shouldn't happen), fall back to single-DB
    if not cross_db_sqls:
        cross_db_sqls = {databases[0]: state["sql"]}

    cross_db_results : Dict[str, List[Dict]] = dict()
    primary_data     : List[Dict]            = list()

    for database, sql in cross_db_sqls.items():
        target_port = PORT_MAP.get(database, settings.gateway_sales_port)

        try:
            async with httpx.AsyncClient(timeout = settings.db_query_timeout_seconds) as client:
                response = await client.post(f"http://{DB_HOST}:{target_port}{_GATEWAY_ENDPOINT}",
                                             json = {"method" : "query_database", 
                                                     "params" : {"sql": sql},
                                                    },
                                            )

                response.raise_for_status()

            result = response.json()

            if not result.get("success"):
                err = f"DB error [{database}]: {result.get('error', 'unknown')}"
                logger.warning("Domain query failed, continuing with partial results", 
                               database = database, 
                               error    = err,
                              )

                # Add to errors but continue the loop
                state["errors"].append(err)
                continue

            raw_data  = result.get("data", [])
            safe_data = _strip_blocked_columns(raw_data)

            logger.info("Query executed",
                        rows     = len(safe_data),
                        database = database,
                       )

            cross_db_results[database] = safe_data

            if (database == databases[0]):
                primary_data = safe_data

        except Exception as e:
            err = f"Execution error [{database}]: {str(e)}"

            logger.error("SQL execution failed", 
                         database = database, 
                         error    = str(e),
                        )

            return {**state,
                    "errors"     : state["errors"] + [err],
                    "retry_count": state["retry_count"] + 1,
                   }

    is_cross_db = (len(cross_db_results) > 1)

    return {**state,
            "data"            : primary_data,
            "cross_db_results": cross_db_results,
            "is_cross_db"     : is_cross_db,
           }


async def analyst_agent(state: BIState) -> BIState:
    """
    Generate a professional BI narrative from query results

    Produces:
    - metrics dict (numerical summary of the primary column)
    - PII-redacted natural-language answer
    - auto-visualization (if settings.enable_visualization is True)
    - Injects conversation history for follow-up query context
    - Merges DataFrames from multiple domains with a _source_domain label column before analysis. Drops that column before charting (text column breaks auto-viz)
    """
    data   = state.get("data", [])
    errors = state.get("errors", [])

    if errors and not data:
        return {**state,
                "answer": f"Query encountered an error: {errors[-1]}",
               }

    # Cross-DB DataFrame merge
    is_cross_db      = state.get("is_cross_db", False)
    cross_db_results = state.get("cross_db_results", {})

    if (is_cross_db and (len(cross_db_results) > 1)):
        frames = list()

        for domain, rows in cross_db_results.items():
            if rows:
                df_part                   = pd.DataFrame(data = rows)
                df_part["_source_domain"] = domain

                frames.append(df_part)

        if frames:
            df = pd.concat(objs         = frames, 
                           ignore_index = True, 
                           sort         = False,
                          )
        
        else:
            df = pd.DataFrame()

    else:
        df = pd.DataFrame(data = data) if data else pd.DataFrame()

    if df.empty:
        return {**state,
                "answer": "No records were found matching your query.",
               }

    row_count               = len(df)
    numeric_cols            = [c for c in df.select_dtypes(include=["int64", "float64"]).columns if not any(excl in c.lower() for excl in ANALYST_METRICS_ID_EXCLUDE)]
    metrics: Dict[str, Any] = {"row_count" : row_count}

    if numeric_cols:
        col                     = numeric_cols[0]
        
        def _safe(v):
            try:
                f = float(v)
                return None if (pd.isna(f) or pd.isinf(f)) else round(f, 2)

            except (TypeError, ValueError):
                return None

        metrics[f"avg_{col}"]   = round(float(df[col].mean()), 2)
        metrics[f"total_{col}"] = round(float(df[col].sum()),  2)
        metrics[f"max_{col}"]   = round(float(df[col].max()),  2)
        metrics[f"min_{col}"]   = round(float(df[col].min()),  2)

    max_rows      = getattr(settings, "analyst_max_rows_in_prompt", ANALYST_DATA_TABLE_ROWS)

    # Drop the internal label column before feeding to the LLM (cosmetic)
    df_for_prompt = df.drop(columns=["_source_domain"], errors="ignore")

    try:
        data_table = df_for_prompt.head(max_rows).to_markdown(index = False)
    
    except ImportError:
        data_table = df_for_prompt.head(max_rows).to_csv(index = False)

    # Inject history into analyst prompt
    history_context = ""

    if settings.session_history_enabled and state.get("conversation_history"):
        history_context = format_history_for_prompt(history   = state["conversation_history"],
                                                    max_turns = settings.session_context_turns,
                                                   )

    cross_db_note   = ""

    if is_cross_db:
        domains       = list(cross_db_results.keys())
        cross_db_note = (f"\n\nNOTE: This result merges data from {len(domains)} databases: "
                         f"{', '.join(d.upper() for d in domains)}. "
                         "The _source_domain column identifies which database each row came from."
                        )

    prompt         = (f"{ANALYST_AGENT_PROMPT.format(history_context = history_context)}\n\n"
                      f"User question: {state['query']}\n"
                      f"{cross_db_note}\n\n"
                      "IMPORTANT:\n"
                      "- Answer based ONLY on the dataset below.\n"
                      "- DO NOT include Python code or code blocks.\n"
                      "- DO NOT explain the logic; provide the business insights.\n"
                      "- Format numbers with commas and 2 decimal places where appropriate.\n\n"
                      f"DATASET (first {max_rows} rows):\n{data_table}\n\n"
                      f"SUMMARY METRICS:\n{json.dumps(metrics, indent = 2)}"
                     )

    raw_answer     = await _llm_complete(prompt)
    cleaned_answer = _strip_think_tags(raw_answer)

    redacted       = pii_redactor.redact(cleaned_answer)
    answer         = redacted.sanitized_text

    # Visualization: drop _source_domain before charting — text column confuses auto_visualize
    df_for_viz     = df.drop(columns=["_source_domain"], errors="ignore")

    viz_title_len  = getattr(settings, "viz_title_max_len", VIZ_TITLE_MAX_LEN)
    viz            = None

    if (settings.enable_visualization and not df_for_viz.empty):
        try:
            fig = viz_generator.auto_visualize(df_for_viz, 
                                               title = state["query"][:viz_title_len],
                                              )

            if fig:
                b64 = viz_generator.figure_to_base64(fig)
                viz = {"type"         : "auto",
                       "title"        : state["query"][:viz_title_len],
                       "data"         : metrics,
                       "base64_image" : b64,
                      }

                logger.info("Visualization generated")

        except Exception as e:
            logger.error("Visualization failed", 
                         error = str(e),
                        )

    return {**state,
            "answer"        : answer,
            "metrics"       : metrics,
            "visualization" : viz,
            "data"          : df.to_dict("records"),    
           }


# Conditional edges
def route_after_sql(state: BIState) -> str:
    if (state["errors"] and (state["retry_count"] < settings.max_agent_retries)):
        return "sql_agent"

    return "execute" if not state["errors"] else "analyst"


def route_after_execute(state: BIState) -> str:
    if (state["errors"] and (state["retry_count"] < settings.max_agent_retries)):
        return "sql_agent"

    return "analyst"


# Graph builder
def _build_graph():
    """
    Compile the LangGraph state machine: node wiring is explicit (not data-driven) for easy audit and extension
    """
    graph = StateGraph(BIState)

    graph.add_node("supervisor", supervisor_agent)
    graph.add_node("fetch_schema", fetch_schema)
    graph.add_node("sql_agent", sql_agent)
    graph.add_node("execute", execute_sql)
    graph.add_node("analyst", analyst_agent)

    graph.set_entry_point("supervisor")

    graph.add_edge("supervisor", "fetch_schema")
    graph.add_edge("fetch_schema", "sql_agent")

    graph.add_conditional_edges("sql_agent", route_after_sql)
    graph.add_conditional_edges("execute", route_after_execute)

    graph.add_edge("analyst", END)

    return graph.compile()


# LLM singleton
_llm_instance: Optional[OllamaClient] = None


def _get_llm() -> OllamaClient:
    global _llm_instance

    if _llm_instance is None:
        _llm_instance = OllamaClient()

    return _llm_instance


async def _llm_complete(prompt: str, system_prompt: Optional[str] = None) -> str:
    """
    Thin wrapper so every graph node uses the same singleton + retry logic: OllamaClient.complete() already strips <think> tags; _strip_think_tags()
    in each node provides belt-and-suspenders coverage
    """
    return await _get_llm().complete(prompt, system_prompt = system_prompt)


# Public entry point
class AgentOrchestrator:
    """
    Public entry point for the BI agent pipeline: owns the compiled LangGraph and exposes a single async method that FastAPI calls
    """
    def __init__(self):
        self.graph = _build_graph()
        logger.info("AgentOrchestrator initialised with compiled LangGraph")


    async def process_query(self, query: str, session_id: str) -> Dict[str, Any]:
        """
        Run the full BI pipeline for a natural-language query: input PII redaction applied here — no PII ever reaches LLM prompts or logs

        - Loads session history before graph invocation and injects it into initial_state so supervisor + analyst nodes can use it for context
        - After graph completion, appends the new turn to session history
        """
        redacted_query                   = pii_redactor.redact(query).sanitized_text

        # Load session history
        conversation_history: List[Dict] = []

        if settings.session_history_enabled and session_id:
            conversation_history = await session_store.get(session_id,
                                                           last_n = settings.session_history_max_turns,
                                                          )

        initial_state: BIState = {"query"                : redacted_query,
                                  "session_id"           : session_id,
                                  "planned_databases"    : [],
                                  "current_schema"       : "",
                                  "sql"                  : "",
                                  "data"                 : [],
                                  "metrics"              : {},
                                  "answer"               : "",
                                  "errors"               : [],
                                  "retry_count"          : 0,
                                  "visualization"        : None,
                                  "conversation_history" : conversation_history,
                                  "cross_db_schemas"     : {},
                                  "cross_db_sqls"        : {},
                                  "cross_db_results"     : {},
                                  "is_cross_db"          : False,
                                 }

        final_state            = await self.graph.ainvoke(initial_state)

        # Save turn to session history
        executed_sql           = ([final_state.get("sql")] if (final_state.get("sql") and not final_state.get("errors")) else [])

        if settings.session_history_enabled and session_id and final_state.get("answer"):
            primary_domain = (final_state.get("planned_databases") or ["unknown"])[0]
            row_count      = len(final_state.get("data", []))

            await session_store.append(session_id = session_id,
                                       query      = redacted_query,
                                       answer     = final_state.get("answer", ""),
                                       sql        = executed_sql[0] if executed_sql else "",
                                       domain     = primary_domain,
                                       row_count  = row_count,
                                       max_turns  = settings.session_history_max_turns,
                                      )

        # Return
        databases_queried = final_state.get("planned_databases", [])
        is_cross_db       = final_state.get("is_cross_db", False)

        return {"answer"            : final_state.get("answer", ""),
                "sql_executed"      : executed_sql,
                "visualization"     : final_state.get("visualization"),
                "data"              : final_state.get("data", []),
                "metrics"           : final_state.get("metrics", {}),
                "reasoning_trace"   : [],
                "errors"            : final_state.get("errors", []),
                "databases_queried" : databases_queried,
                "is_cross_db"       : is_cross_db,
               }