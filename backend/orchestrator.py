# DEPENDENCIES
import re
import json
import time
import httpx
import structlog
import numpy as np
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
from features.data_analyzer import data_analyzer
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
from config.prompts import CONVERSATIONAL_AGENT_PROMPT
from config.constants import ANALYST_METRICS_ID_EXCLUDE
from config.constants import SUPERVISOR_ROUTING_KEYWORDS
from config.constants import SUPERVISOR_DEFAULT_DATABASE
from config.prompts import CONVERSATIONAL_INTENT_KEYWORDS
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
    conversation_history : List[Dict]     # short-term: recent N turns
    long_term_context    : str            # long-term: formatted persistent facts
    cross_db_schemas     : Dict[str, str]
    cross_db_sqls        : Dict[str, str]
    cross_db_results     : Dict[str, List[Dict]]
    is_cross_db          : bool
    reasoning_trace      : List[str]
    query_confidence     : float


# Routing tables

PROMPT_MAP : Dict[str, str] = {"health"  : HEALTH_AGENT_PROMPT,
                               "finance" : FINANCE_AGENT_PROMPT,
                               "sales"   : SALES_AGENT_PROMPT,
                               "iot"     : IOT_AGENT_PROMPT,
                              }

PORT_MAP : Dict[str, int]   = {"health"  : settings.gateway_health_port,
                               "finance" : settings.gateway_finance_port,
                               "sales"   : settings.gateway_sales_port,
                               "iot"     : settings.gateway_iot_port,
                              }

DB_HOST           : str     = settings.db_base_host
_GATEWAY_ENDPOINT           = "/gateway"
 

# append_short_term / save_long_term.  We fall back gracefully to the legacy
# get() / append() so the orchestrator runs unchanged against either version.
async def _store_get_short_term(session_id: str, last_n: int) -> List[Dict]:
    """
    Load recent conversation turns (short-term episodic buffer)
    """
    fn = getattr(session_store, "get_short_term", None) or getattr(session_store, "get", None)
    
    if fn is None:
        return []
    
    try:
        return await fn(session_id, last_n = last_n)
    
    except Exception as exc:
        logger.warning("Short-term history load failed", error=str(exc))
        return []


async def _store_get_long_term(session_id: str) -> Dict[str, Any]:
    """
    Load persistent long-term memory and returns a dictionary when the store does not support long-term memory yet

    Expected keys in the returned dict:
      preferred_domains : List[str]           -- frequently queried domains
      key_entities      : Dict[str, str]      -- named entities e.g. {"product": "Pro Plan"}
      key_facts         : List[str]           -- notable findings from past turns
    """
    fn = getattr(session_store, "get_long_term", None)

    if fn is None:
        return {}

    try:
        result = await fn(session_id)
        return result if isinstance(result, dict) else {}
    
    except Exception as exc:
        logger.warning("Long-term memory load failed", 
                       error = str(exc),
                      )
        return {}


async def _store_append_short_term(session_id: str, query: str, answer: str, sql: str, domain: str, row_count: int, max_turns: int) -> None:
    """
    Append current turn to the short-term buffer
    """
    fn = (getattr(session_store, "append_short_term", None) or
          getattr(session_store, "append", None),
         )

    if fn is None:
        return

    try:
        await fn(session_id = session_id, 
                 query      = query, 
                 answer     = answer,
                 sql        = sql, 
                 domain     = domain, 
                 row_count  = row_count, 
                 max_turns  = max_turns,
                )

    except Exception as exc:
        logger.warning("Short-term history save failed", 
                       error = str(exc),
                      )


async def _store_save_long_term(session_id: str, facts: Dict[str, Any]) -> None:
    """
    Persist updated long-term facts
    """
    fn = getattr(session_store, "save_long_term", None)

    if fn is None:
        return

    try:
        await fn(session_id = session_id, 
                 facts      = facts,
                )

    except Exception as exc:
        logger.warning("Long-term memory save failed", 
                       error = str(exc),
                      )


def _format_long_term_for_prompt(lt: Dict[str, Any]) -> str:
    """
    Convert the long-term memory dict into a compact, human-readable block that can be appended to any agent's history context and
    returns "" when the dict is empty
    """
    if not lt:
        return ""

    parts: List[str] = list()

    preferred        = lt.get("preferred_domains", [])

    if preferred:
        parts.append(f"User's frequently queried domains: {', '.join(preferred)}")

    entities         = lt.get("key_entities", {})

    if entities:
        entity_str = "; ".join(f"{k}={v}" for k, v in list(entities.items())[:6])
        parts.append(f"Known entities from past sessions: {entity_str}")

    facts            = lt.get("key_facts", [])
    
    if facts:
        parts.append("Key findings from previous sessions:")
        for fact in facts[:5]:
            parts.append(f"  \u2022 {fact}")

    if not parts:
        return ""

    return "\n\n[LONG-TERM MEMORY \u2014 persistent context]\n" + "\n".join(parts)


def _extract_facts_for_long_term(query: str, answer: str, domain: str, row_count: int, lt_current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Heuristically extract long-term facts from a significant query result: only called for SQL queries that returned real data
    and returns an updated long-term dict, or None if nothing changed
    """
    if ((domain == "conversational") or (row_count == 0)):
        return None

    updated      = dict(lt_current)

    # Track domain usage (rolling window of 4)
    domains_used = list(updated.get("preferred_domains", []))

    if domain not in domains_used:
        domains_used.append(domain)
        updated["preferred_domains"] = domains_used[-4:]

    # Extract first sentence of answer as a key fact (≤120 chars)
    if answer:
        first_sentence = re.split(r"(?<=[.!?])\s", answer.strip())[0]

        if (10 < len(first_sentence) <= 120):
            key_facts = list(updated.get("key_facts", []))

            if first_sentence not in key_facts:
                key_facts.append(first_sentence)

                # rolling window
                updated["key_facts"] = key_facts[-10:]   

    return updated if (updated != lt_current) else None



def _strip_blocked_columns(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove BLOCKED_OUTPUT_COLUMNS from every row before it reaches the analyst or API
    """
    if not rows:
        return rows

    cols_in_result  = set(rows[0].keys())
    blocked_present = {c for c in cols_in_result if c.lower().replace(" ", "_") in BLOCKED_OUTPUT_COLUMNS}
    
    if not blocked_present:
        return rows
    
    logger.warning("Blocking sensitive columns from result set", 
                   columns = list(blocked_present),
                  )

    return [{k: v for k, v in row.items() if k not in blocked_present} for row in rows]


def _check_sql_for_prohibited_patterns(sql: str) -> Optional[str]:
    """
    Belt-and-suspenders check on top of SQLValidator
    """
    sql_upper = sql.upper()

    for pattern in PROHIBITED_SQL_PATTERNS:
        if re.search(pattern, sql_upper):
            return f"SQL contains prohibited pattern: {pattern}"

    return None


def _strip_think_tags(text: str) -> str:
    """
    Remove DeepSeek-R1 <think>...</think> blocks
    """
    return re.sub(THINK_TAG_PATTERN, "", text, flags = re.DOTALL).strip()


def _extract_json_array(text: str) -> Optional[List[str]]:
    """
    Extract the first valid JSON array from arbitrary LLM text
    """
    text = text.strip()

    try:
        parsed = json.loads(text)

        if isinstance(parsed, list):
            return parsed

    except (json.JSONDecodeError, TypeError):
        pass

    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed

    except (json.JSONDecodeError, TypeError):
        pass

    start = text.find("[")
    if (start == -1):
        return None

    depth       = 0
    in_string   = False
    escape_next = False

    for idx in range(start, len(text)):
        c = text[idx]
        
        if escape_next:
            escape_next = False
            continue

        if (c == "\\"):
            escape_next = True
            continue

        if (c == '"'):
            in_string = not in_string
            continue

        if in_string:
            continue

        if (c == "["):
            depth += 1

        elif (c == "]"):
            depth -= 1

            if (depth == 0):
                candidate = text[start : idx + 1]
                
                try:
                    parsed = json.loads(candidate)
                    
                    if isinstance(parsed, list):
                        return parsed

                except (json.JSONDecodeError, TypeError):
                    pass

                break

    return None


def _get_error_guidance(error_msg: str, schema_tables: List[str], schema_json: Optional[Dict] = None) -> str:
    """
    Return targeted SQL correction guidance based on error pattern
    """
    error_lower = error_msg.lower()

    table_hints = ""
    if (schema_json and ("tables" in schema_json)):
        tables      = list(schema_json["tables"].keys())[:6]
        table_hints = f"Available tables: {', '.join(tables)}. "


    patterns = [(r"undefined.*alias|from-clause entry.*table|relation.*does not exist",
                 lambda: (f"CRITICAL: Define ALL table aliases in FROM/JOIN clause before using them. "
                          f"\n{table_hints}Example: FROM transactions t JOIN subscriptions s ON "
                          f"t.customer_id = s.customer_id"
                         )
                ),
                (r"must appear in the group by clause|non-aggregated column",
                 lambda: ("GROUP BY RULE: Every SELECT column without an aggregate function "
                          "(COUNT, SUM, AVG, MAX, MIN) must be listed in GROUP BY. "
                          "Example: SELECT status, COUNT(*) FROM claims GROUP BY status"
                         )
                ),
                (r"column.*does not exist|hint.*perhaps you meant",
                 lambda: (f"COLUMN MISMATCH: Check column names against the schema above. "
                          f"{table_hints}Use exact spelling and case."
                         )
                ),
                (r"function.*does not exist|operator.*does not exist|type.*does not exist",
                 lambda: ("TYPE ERROR: PostgreSQL requires explicit casts. "
                          "Examples: '2024-01-01'::DATE, amount::DECIMAL."
                         )
                ),
                (r"syntax error.*at or near|unterminated.*quoted string|incomplete statement",
                 lambda: "SYNTAX ERROR: Check for unclosed quotes, missing commas, trailing operators."
                ),
                (r"permission denied|read-only|not allowed|prohibited",
                 lambda: "SECURITY: Only SELECT queries allowed. Remove INSERT, UPDATE, DELETE, DROP."
                ),
               ]

    for pattern, guidance_fn in patterns:
        if re.search(pattern, error_lower):
            return f"\n\u274c {guidance_fn()}"

    return (f"\n\U0001f4a1 SQL VALIDATION FAILED: {error_msg} "
            f"{table_hints}Review schema and retry with valid PostgreSQL SELECT syntax.")


def _build_rich_metrics(df: pd.DataFrame, base_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Augment the simple metrics dict with flat summary values from DataAnalyzer:  All added values are JSON-primitive (str / float / int) so the frontend
    renderMetrics() function can display them without modification

    Added keys (when data is sufficient):
      data_shape          : distribution shape of primary numeric column
      trend_direction     : "upward" | "downward" | "flat"
      period_change_pct   : recent half vs earlier half % change
      outlier_columns     : comma-separated columns with >5% outliers
      top_correlation     : "col_a x col_b  r=0.87"
    """
    if df is None or df.empty:
        return base_metrics

    metrics = dict(base_metrics)

    try:
        report = data_analyzer.generate_comprehensive_report(df)
    
    except Exception as exc:
        logger.warning("Comprehensive report generation failed", error=str(exc))
        return metrics

    # Distribution shape for primary numeric column
    num_summary = report.get("summary_statistics", {}).get("numerical_summary", {})
    
    if num_summary:
        first_col = next(iter(num_summary), None)
        
        if first_col:
            shape = num_summary[first_col].get("shape")
            
            if (shape and (shape != "unknown")):
                metrics["data_shape"] = shape

    # Time-series trend
    ts = report.get("time_series_analysis", {})

    if (ts and ("error" not in ts)):
        if ts.get("overall_trend"):
            metrics["trend_direction"] = ts["overall_trend"]

        cp = ts.get("recent_trend", {}).get("change_pct")

        if cp is not None:
            metrics["period_change_pct"] = round(cp, 2)

    # Outlier-flagged columns
    flagged = report.get("outlier_summary", {}).get("flagged", [])
    
    if flagged:
        metrics["outlier_columns"] = ", ".join(flagged[:4])

    # Top correlation pair
    top_corr = report.get("correlation_analysis", {}).get("top_correlations", [])
    
    if top_corr:
        tc                         = top_corr[0]
        metrics["top_correlation"] = (f"{tc['column1']} \u00d7 {tc['column2']}  r={tc['correlation']:+.2f}")

    return metrics


def _build_analyst_data_summary(report: Dict[str, Any]) -> str:
    """
    Convert a comprehensive_report into a compact analytical block injected into the analyst LLM prompt
    
    It gives the model richer context without pasting the entire nested dict
    """
    lines: List[str] = list()

    # Numerical stats for top 3 columns
    num_summary      = report.get("summary_statistics", {}).get("numerical_summary", {})

    if num_summary:
        lines.append("STATISTICAL SUMMARY (key columns):")
        
        for col, stats in list(num_summary.items())[:3]:
            shape = stats.get("shape", "")
            skew  = stats.get("skewness")
            mean  = stats.get("mean")
            med   = stats.get("median")
            std   = stats.get("std")
            
            if mean is None:
                continue
            
            lines.append(f"  {col}: mean={mean:.4g}  median={med:.4g}  std={std:.4g}"
                         f"  [{shape}"
                         + (f", skew={skew:.2f}" if skew is not None else "")
                         + "]"
                        )

    # Time-series
    ts = report.get("time_series_analysis", {})

    if ts and "error" not in ts:
        dr    = ts.get("date_range", {})
        trend = ts.get("overall_trend", "unknown").upper()
        r2    = ts.get("r_squared")
        cp    = ts.get("recent_trend", {}).get("change_pct")
        
        lines.append(f"\nTIME-SERIES: {dr.get('start', '')} \u2192 {dr.get('end', '')}  "
                     f"Trend={trend}"
                     + (f"  R\u00b2={r2:.3f}" if r2 is not None else "")
                     + (f"  Period \u0394={cp:+.1f}%" if cp is not None else "")
                    )

    # Top correlations
    top = report.get("correlation_analysis", {}).get("top_correlations", [])[:3]
    
    if top:
        lines.append("\nTOP CORRELATIONS:")
        for tc in top:
            lines.append(f"  {tc['column1']} \u00d7 {tc['column2']}: r={tc['correlation']:+.4f}")

    # Flagged outlier columns
    flagged = report.get("outlier_summary", {}).get("flagged", [])
    
    if flagged:
        lines.append(f"\nOUTLIER WARNING: columns with >5% outliers: {', '.join(flagged)}")

    return "\n".join(lines)


# GRAPH NODES
async def supervisor_agent(state: BIState) -> BIState:
    """
    Three-phase routing:
      Phase 1 — Conversational keyword fast-path (no LLM, ~0 ms)
      Phase 2 — Domain keyword pre-check (no LLM)
      Phase 3 — LLM routing with short + long-term context

    "conversational" sentinel bypasses the SQL pipeline
    """
    query       = state["query"]
    query_lower = query.lower().strip()

    # Phase 1
    if ((query_lower in CONVERSATIONAL_INTENT_KEYWORDS) or (any(query_lower.startswith(kw) for kw in CONVERSATIONAL_INTENT_KEYWORDS))):
        logger.info("Supervisor: conversational fast-path", 
                    query = query[:60],
                   )
         
        trace = list(state.get("reasoning_trace", []))

        trace.append("[Supervisor] Conversational query \u2014 bypassing SQL pipeline")

        return {**state, 
                "planned_databases" : ["conversational"], 
                "reasoning_trace"   : trace,
               }

    # Phase 2
    pre_targets: List[str] = list()

    for db, keywords in SUPERVISOR_ROUTING_KEYWORDS.items():
        if (any(kw in query_lower for kw in keywords)):
            pre_targets.append(db)

    valid_dbs       = list(SUPERVISOR_ROUTING_KEYWORDS.keys()) + ["conversational"]
    valid_dbs_json  = json.dumps(valid_dbs)
    default_db_str  = SUPERVISOR_DEFAULT_DATABASE.value

    # Phase 3
    history_context = ""

    if settings.session_history_enabled and state.get("conversation_history"):
        history_context = format_history_for_prompt(history   = state["conversation_history"],
                                                    max_turns = settings.session_context_turns,
                                                   )

    lt_block = state.get("long_term_context", "")

    if lt_block:
        history_context = history_context + lt_block

    prompt             = (f"{SUPERVISOR_SYSTEM_PROMPT.format(history_context=history_context)}\n\n"
                          f'User Query: "{query}"\n\n'
                          "Determine which backend to route to. "
                          f"Valid values: {valid_dbs_json}. "
                          "Return ONLY a valid JSON array \u2014 nothing else. "
                          f'Examples: ["{default_db_str}"] or ["conversational"]'
                         )

    raw_response       = await _llm_complete(prompt)
    cleaned_response   = _strip_think_tags(raw_response)

    targets: List[str] = list()

    extracted          = _extract_json_array(text = cleaned_response)

    if extracted:
        for item in extracted:
            if isinstance(item, list) and item:
                item = item[0]
            
            if isinstance(item, str):
                db = item.lower().replace("_db", "").strip()
                
                if db in valid_dbs:
                    targets.append(db)

    else:
        logger.warning("Supervisor LLM returned unparseable response",
                       raw_preview = cleaned_response[:120],
                      )

    if not targets:
        targets = pre_targets

    if not targets:
        logger.warning("Routing fallback to default", 
                       default = default_db_str,
                      )

        targets = [default_db_str]

    # Dedup
    seen, unique = set(), []

    for t in targets:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    targets = unique

    if ("conversational" in targets):
        trace = list(state.get("reasoning_trace", []))
        trace.append("[Supervisor] Conversational query \u2014 bypassing SQL pipeline")

        return {**state, 
                "planned_databases" : ["conversational"], 
                "reasoning_trace"   : trace,
               }

    # Cross-DB validation
    if (len(targets) > 1):
        if not settings.enable_cross_db_joins:
            logger.info("Cross-DB joins disabled; trimming",
                        kept    = targets[0], 
                        dropped = targets[1:],
                       )

            targets = [targets[0]]
        
        else:
            pair = frozenset(targets[:2])
            
            if pair not in ALLOWED_CROSS_DB_PAIRS:
                logger.warning("Cross-DB pair not in allowlist; trimming", 
                               pair = list(pair),
                              )

                targets = [targets[0]]

            targets = targets[: settings.max_cross_db_domains]

    logger.info("Supervisor routing decision", databases=targets)

    is_cross   = (len(targets) > 1)
    route_note = (f"Cross-DB query \u2192 routing to: {', '.join(t.upper() for t in targets)}" if is_cross else f"Routing to: {targets[0].upper() if targets else 'default'}")
    trace      = list(state.get("reasoning_trace", []))

    trace.append(f"[Supervisor] {route_note}")

    return {**state, 
            "planned_databases" : targets, 
            "reasoning_trace"   : trace,
           }


async def fetch_schema(state: BIState) -> BIState:
    """
    Retrieve live DB schema(s) via gateway server
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

            if result.get("success"):
                tables = result.get("tables", {})
                schema = ""

                for table, columns in tables.items():
                    schema += f"CREATE TABLE {table} (\n"
                    
                    for col in columns:
                        if isinstance(col, dict):
                            col_name = col.get("name", col)
                            col_type = col.get("type", "TEXT").upper()
                        
                        else:
                            col_name = col
                            col_type = "TEXT"

                        schema += f"  {col_name} {col_type},\n"

                    schema += ");\n\n"
            
            else:
                schema = "Schema unavailable."

        except Exception as exc:
            logger.error("Schema fetch failed", 
                         database = database, 
                         error    = str(exc),
                        )

            schema = "Schema fetch failed"

        cross_db_schemas[database] = schema

    primary_schema = cross_db_schemas.get(state["planned_databases"][0], "Schema unavailable.")
    schema_ok      = all("Schema" not in v for v in cross_db_schemas.values())
    db_list        = ", ".join(state["planned_databases"]).upper()

    trace          = list(state.get("reasoning_trace", []))

    trace.append(f"[Schema] Fetched for: {db_list} \u2014 {'OK' if schema_ok else 'partial failures'}")

    return {**state, 
            "current_schema"   : primary_schema,
            "cross_db_schemas" : cross_db_schemas, 
            "reasoning_trace"  : trace,
           }


async def sql_agent(state: BIState) -> BIState:
    """
    Generate domain-specific SQL validated through two independent layers:
    1. SQLValidator (syntax + read-only + LIMIT injection)
    2. Orchestrator guardrail (prohibited-pattern check)
    
    - Cross-DB generates separate validated SQL per domain
    - History intentionally NOT injected (causes hallucinated CTEs).
    """
    databases                     = state["planned_databases"]
    is_cross_db                   = len(databases) > 1
    cross_db_sqls: Dict[str, str] = dict()

    error_context                 = ""

    if (state["errors"] and (state["retry_count"] > 0)):
        last_error    = state["errors"][-1]
        schema_tables = list()
        schema_json   = None

        if state.get("current_schema"):
            try:
                schema_json   = json.loads(state["current_schema"])
                schema_tables = list(schema_json.get("tables", {}).keys())
            
            except Exception:
                schema_tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", state["current_schema"])

        guidance      = _get_error_guidance(last_error.lower(), schema_tables, schema_json)

        error_context = (f"\n\u26a0\ufe0f PREVIOUS SQL FAILED:\n   {last_error}\n\n"
                         f"\U0001f4cb SCHEMA REMINDER:\n"
                         f"   Available tables: {schema_tables if schema_tables else 'See schema above'}\n"
                         f"{guidance}\n\nGenerate corrected SQL:"
                        )

    if not is_cross_db:
        database      = databases[0]
        domain_prompt = PROMPT_MAP.get(database, SALES_AGENT_PROMPT)
        prompt        = (f"{domain_prompt}\n\n"
                         f"CRITICAL: You are connected ONLY to the {database.upper()} database.\n"
                         "Use ONLY the tables and columns shown in the schema below.\n\n"
                         f"{database.upper()} DATABASE SCHEMA:\n{state['current_schema']}\n\n"
                         f"User Request: {state['query']}\n"
                         f"{error_context}\n\n"
                         "Output ONLY valid PostgreSQL. No markdown, no explanations."
                        )
        raw_sql       = await _llm_complete(prompt)
        sql           = _strip_think_tags(raw_sql)
        sql           = sql.replace("```postgresql", "").replace("```sql", "").replace("```", "").strip()

        try:
            validation = sql_validator.validate(sql)
        
        except Exception as exc:
            logger.warning("SQL validation crashed", 
                           error = str(exc),
                          )
                          
            validation = type("obj", (object,), {"is_valid": True, "sanitized_sql": sql})()

        if not validation.is_valid:
            err = validation.error_message
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

        logger.info("SQL validated and accepted", 
                    database = database,
                   )

        trace = list(state.get("reasoning_trace", []))

        trace.append(f"[SQL Agent] Generated SQL for {database.upper()} \u2014 "
                     f"validated OK (attempt {state['retry_count'] + 1})"
                    )

        return {**state, 
                "sql"             : validation.sanitized_sql, 
                "errors"          : [],
                "cross_db_sqls"   : {database: validation.sanitized_sql}, 
                "reasoning_trace" : trace,
               }

    # Cross-DB path
    first_sql = ""

    for idx, database in enumerate(databases):
        schema        = state["cross_db_schemas"].get(database, "Schema unavailable.")
        domain_prompt = PROMPT_MAP.get(database, SALES_AGENT_PROMPT)

        if (idx == 0):
            prompt = (f"{domain_prompt}\n\n"
                      f"CRITICAL: You are connected ONLY to the {database.upper()} database.\n"
                      f"{database.upper()} DATABASE SCHEMA:\n{schema}\n\n"
                      f"User Request: {state['query']}\n{error_context}\n\n"
                      "Output ONLY valid PostgreSQL. No markdown, no explanations."
                     )

        else:
            other_db   = databases[0]
            col_hints  = (re.findall(r"\b(\w+)\b", first_sql.split("FROM")[0]) if "FROM" in first_sql.upper() else [])
            col_hints  = [c for c in col_hints if c.upper() not in ("SELECT", "DISTINCT", "AS") and len(c) > 2]

            col_sample = ", ".join(col_hints[:10]) or "various columns"

            prompt     = CROSS_DB_SQL_AGENT_PROMPT.format(target_database = database.upper(), 
                                                          other_database  = other_db.upper(),
                                                          other_columns   = col_sample,
                                                          schema          = schema, 
                                                          query           = state["query"],
                                                         )

        raw_sql = await _llm_complete(prompt)
        sql     = _strip_think_tags(raw_sql)
        sql     = sql.replace("```postgresql", "").replace("```sql", "").replace("```", "").strip()

        try:
            validation = sql_validator.validate(sql)

        except Exception as exc:
            logger.warning("Cross-DB SQL validation crashed",
                           error = str(exc),
                          )

            validation = type("obj", (object,), {"is_valid": True, "sanitized_sql": sql})()

        if not validation.is_valid:
            err = f"[{database}] {validation.error_message}"

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
                    "retry_count" : state["retry_count"] + 1}

        cross_db_sqls[database] = validation.sanitized_sql
        
        if (idx == 0):
            first_sql = validation.sanitized_sql

        logger.info("Cross-DB SQL validated", 
                    database = database,
                   )

    primary_sql = cross_db_sqls.get(databases[0], "")
    return {**state, 
            "sql"           : primary_sql, 
            "errors"        : [], 
            "cross_db_sqls" : cross_db_sqls,
           }


async def execute_sql(state: BIState) -> BIState:
    """
    Execute validated SQL via gateway server; apply output guardrails
    """
    databases     = state["planned_databases"]
    cross_db_sqls = state.get("cross_db_sqls", {})

    if not cross_db_sqls:
        cross_db_sqls = {databases[0]: state["sql"]}

    cross_db_results: Dict[str, List[Dict]] = dict()
    primary_data:     List[Dict]            = list()

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
                
                logger.warning("Domain query failed", 
                               database = database, 
                               error    = err,
                              )
                
                return {**state, 
                        "errors"      : state["errors"] + [err],
                        "retry_count" : state["retry_count"] + 1,
                       }

            safe_data = _strip_blocked_columns(result.get("data", []))

            logger.info("Query executed", 
                        rows     = len(safe_data), 
                        database = database,
                       )

            cross_db_results[database] = safe_data

            if (database == databases[0]):
                primary_data = safe_data

        except Exception as exc:
            err = f"Execution error [{database}]: {str(exc)}"
            
            logger.error("SQL execution failed", 
                         database = database, 
                         error    = str(exc),
                        )
            
            return {**state, 
                    "errors"      : state["errors"] + [err],
                    "retry_count" : state["retry_count"] + 1,
                   }

    return {**state, 
            "data"             : primary_data, 
            "cross_db_results" : cross_db_results,
            "is_cross_db"      : len(cross_db_results) > 1, 
            "errors"           : [],
           }


async def analyst_agent(state: BIState) -> BIState:
    """
    Generate a professional BI narrative from query results, produces: rich metrics (DataAnalyzer augmented), PII-redacted answer,
    auto-visualization, and confidence score
    """
    data   = state.get("data", [])
    errors = state.get("errors", [])

    if errors and not data:
        return {**state, 
                "answer" : f"Query encountered an error: {errors[-1]}",
               }

    is_cross_db      = state.get("is_cross_db", False)
    cross_db_results = state.get("cross_db_results", {})

    if (is_cross_db and (len(cross_db_results) > 1)):
        frames = list()

        for domain, rows in cross_db_results.items():
            if rows:
                df_part                   = pd.DataFrame(data = rows)
                df_part["_source_domain"] = domain
                
                frames.append(df_part)
        df = pd.concat(objs         = frames, 
                       ignore_index = True, 
                       sort         = False,
                      ) if frames else pd.DataFrame()
    
    else:
        df = pd.DataFrame(data=data) if data else pd.DataFrame()

    if df.empty:
        return {**state, 
                "answer" : "No records were found matching your query.",
               }

    row_count                    = len(df)
    numeric_cols                 = [c for c in df.select_dtypes(include = ["int64", "float64"]).columns if not any(excl in c.lower() for excl in ANALYST_METRICS_ID_EXCLUDE)]

    # Base metrics
    base_metrics: Dict[str, Any] = {"row_count": row_count}
    
    if numeric_cols:
        col = numeric_cols[0]
        
        def _safe(v):
            try:
                f = float(v)
                return None if (pd.isna(f) or np.isinf(f)) else round(f, 2)
        
            except (TypeError, ValueError):
                return None
        
        base_metrics[f"avg_{col}"]   = _safe(df[col].mean())
        base_metrics[f"total_{col}"] = _safe(df[col].sum())
        base_metrics[f"max_{col}"]   = _safe(df[col].max())
        base_metrics[f"min_{col}"]   = _safe(df[col].min())

    # Augment with DataAnalyzer
    df_clean             = df.drop(columns = ["_source_domain"], 
                                   errors  = "ignore",
                                  )

    metrics              = _build_rich_metrics(df_clean, base_metrics)

    # Compact analytical summary for the LLM prompt
    analyst_data_summary = ""

    try:
        report = data_analyzer.generate_comprehensive_report(df_clean)
        
        if report:
            analyst_data_summary = _build_analyst_data_summary(report)
    
    except Exception as exc:
        logger.warning("Data analysis for prompt failed", error=str(exc))

    # Data table
    max_rows      = getattr(settings, "analyst_max_rows_in_prompt", ANALYST_DATA_TABLE_ROWS)
    df_for_prompt = df.drop(columns = ["_source_domain"], 
                            errors  = "ignore",
                           )

    try:
        data_table = df_for_prompt.head(max_rows).to_markdown(index = False)

    except ImportError:
        data_table = df_for_prompt.head(max_rows).to_csv(index = False)

    # History context (short-term + long-term)
    history_context = ""

    if settings.session_history_enabled and state.get("conversation_history"):
        history_context = format_history_for_prompt(history   = state["conversation_history"],
                                                    max_turns = settings.session_context_turns,
                                                   )

    lt_block = state.get("long_term_context", "")

    if lt_block:
        history_context = history_context + lt_block

    cross_db_note = ""

    if is_cross_db:
        domains       = list(cross_db_results.keys())
        cross_db_note = (f"\n\nNOTE: This result merges data from {len(domains)} databases: "
                         f"{', '.join(d.upper() for d in domains)}. "
                         "The _source_domain column identifies the source database for each row."
                        )

    prompt        = (f"{ANALYST_AGENT_PROMPT.format(history_context=history_context)}\n\n"
                     f"User question: {state['query']}\n"
                     f"{cross_db_note}\n\n"
                     "IMPORTANT:\n"
                     "- Answer based ONLY on the dataset below.\n"
                     "- DO NOT include Python code or code blocks.\n"
                     "- DO NOT explain the logic; provide the business insights.\n"
                     "- Format numbers with commas and 2 decimal places where appropriate.\n\n"
                     f"DATASET (first {max_rows} rows):\n{data_table}\n\n"
                     f"SUMMARY METRICS:\n{json.dumps(metrics, indent=2)}"
                     + (f"\n\nDATA ANALYSIS:\n{analyst_data_summary}" if analyst_data_summary else "")
                    )

    raw_answer    = await _llm_complete(prompt)
    answer        = pii_redactor.redact(_strip_think_tags(raw_answer)).sanitized_text

    # Visualization
    df_for_viz    = df.drop(columns=["_source_domain"], errors="ignore")
    viz_title_len = getattr(settings, "viz_title_max_len", VIZ_TITLE_MAX_LEN)
    viz           = None

    if (settings.enable_visualization and not df_for_viz.empty):
        try:
            viz_result = viz_generator.auto_visualize(df_for_viz,
                                                      title = state["query"][:viz_title_len],
                                                     )

            if viz_result:
                fig, chart_type = viz_result
                b64             = viz_generator.figure_to_base64(fig)
                viz             = {"type"         : "auto", 
                                   "chart_type"   : chart_type,
                                   "title"        : state["query"][:viz_title_len],
                                   "data"         : metrics, 
                                   "base64_image" : b64,
                                  }

                logger.info("Visualization generated", 
                            chart_type = chart_type,
                           )

        except Exception as exc:
            logger.error("Visualization failed", 
                         error = str(exc),
                        )

    # Confidence
    confidence  = 1.0
    retry_count = state.get("retry_count", 0)
    had_errors  = bool(state.get("errors", []))
    
    if (retry_count >= 2):
        confidence -= 0.30

    elif (retry_count == 1):
        confidence -= 0.15

    if had_errors:
        confidence -= 0.15

    if row_count == 0:
        confidence -= 0.20
    
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    trace      = list(state.get("reasoning_trace", []))
    trace.append(f"[Analyst] Generated answer \u00b7 {row_count:,} rows \u00b7 confidence {confidence:.0%}")

    if viz:
        trace.append(f"[Viz] Generated {viz.get('chart_type', 'auto')} chart")

    if analyst_data_summary:
        trace.append("[Analysis] DataAnalyzer report generated")

    return {**state, 
            "answer"           : answer, 
            "metrics"          : metrics, 
            "visualization"    : viz,
            "data"             : df.to_dict("records"),
            "reasoning_trace"  : trace,
            "query_confidence" : confidence,
           }


async def conversational_agent(state: BIState) -> BIState:
    """
    Handle general / follow-up queries without the SQL pipeline
    """
    query           = state["query"]

    history_context = ""

    if (settings.session_history_enabled and state.get("conversation_history")):
        history_context = format_history_for_prompt(history   = state["conversation_history"],
                                                    max_turns = settings.session_context_turns,
                                                   )

    lt_block = state.get("long_term_context", "")

    if lt_block:
        history_context = history_context + lt_block

    prompt = (f"{CONVERSATIONAL_AGENT_PROMPT.format(history_context=history_context)}\n\n"
              f'User message: "{query}"\n\n'
              "Respond naturally according to the rules above."
             )

    raw    = await _llm_complete(prompt)
    answer = _strip_think_tags(raw)
    answer = re.sub(r"^User message\s*:.*\n?", "", answer, flags=re.IGNORECASE).strip()

    trace  = list(state.get("reasoning_trace", []))

    trace.append("[Conversational] Direct response \u2014 no SQL executed")
    logger.info("Conversational agent responded", 
                query = query[:60],
               )

    return {**state, 
            "answer"           : answer, 
            "reasoning_trace"  : trace, 
            "query_confidence" : 1.0,
           }


# Conditional edge functions
def route_after_supervisor(state: BIState) -> str:
    if (state.get("planned_databases") == ["conversational"]):
        return "conversational"

    return "fetch_schema"


def route_after_sql(state: BIState) -> str:
    if (state["errors"] and (state["retry_count"] < settings.max_agent_retries)):
        return "sql_agent"

    return "execute" if (not state["errors"]) else "analyst"


def route_after_execute(state: BIState) -> str:
    if (state["errors"] and (state["retry_count"] < settings.max_agent_retries)):
        return "sql_agent"
    
    return "analyst"


# Graph builder
def _build_graph():
    graph = StateGraph(BIState)
    graph.add_node("supervisor", supervisor_agent)
    graph.add_node("conversational", conversational_agent)
    graph.add_node("fetch_schema", fetch_schema)
    graph.add_node("sql_agent", sql_agent)
    graph.add_node("execute", execute_sql)
    graph.add_node("analyst", analyst_agent)
    graph.set_entry_point("supervisor")
    graph.add_conditional_edges("supervisor", route_after_supervisor, {"conversational": "conversational", "fetch_schema": "fetch_schema"})
    graph.add_edge("conversational", END)
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
    return await _get_llm().complete(prompt, 
                                     system_prompt = system_prompt,
                                    )


#  PUBLIC ENTRY POINT
class AgentOrchestrator:
    """
    Public entry point for the BI agent pipeline

    Memory model:
    -------------
    Short-term  — last N turns (episodic buffer); loaded + saved each request.
                  Used by supervisor (referential resolution), analyst (follow-up
                  context) and conversational agent.

    Long-term   — persistent cross-session facts / domain preferences / key findings.
                  Loaded each request; heuristically updated after SQL queries that
                  return real data.  Formatted as a compact block appended to the
                  history context string that all agents receive.

    Graceful fallbacks ensure the orchestrator runs unchanged against the old session_store (no get_long_term / save_long_term methods)
    """
    def __init__(self):
        self.graph = _build_graph()

        logger.info("AgentOrchestrator initialised", 
                    graph_nodes = list(self.graph.nodes),
                   )


    async def process_query(self, query: str, session_id: str) -> Dict[str, Any]:
        t0                               = time.perf_counter()
        redacted_query                   = pii_redactor.redact(query).sanitized_text

        # Load short-term + long-term memory
        conversation_history: List[Dict] = list()
        lt_memory: Dict[str, Any]        = dict()

        if settings.session_history_enabled and session_id:
            conversation_history = await _store_get_short_term(session_id, last_n=settings.session_history_max_turns)
            lt_memory            = await _store_get_long_term(session_id)

        long_term_context      = _format_long_term_for_prompt(lt_memory)

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
                                  "long_term_context"    : long_term_context,
                                  "cross_db_schemas"     : {},
                                  "cross_db_sqls"        : {},
                                  "cross_db_results"     : {},
                                  "is_cross_db"          : False,
                                  "reasoning_trace"      : [],
                                  "query_confidence"     : 1.0,
                                 }

        final_state            = await self.graph.ainvoke(initial_state)
        elapsed_ms             = int((time.perf_counter() - t0) * 1000)

        executed_sql           = ([final_state.get("sql")] if final_state.get("sql") and not final_state.get("errors") else [])
        primary_domain         = (final_state.get("planned_databases") or ["unknown"])[0]
        row_count              = len(final_state.get("data", []))

        # Save short-term turn
        if settings.session_history_enabled and session_id and final_state.get("answer"):
            await _store_append_short_term(session_id = session_id,
                                           query      = redacted_query,
                                           answer     = final_state.get("answer", ""),
                                           sql        = executed_sql[0] if executed_sql else "",
                                           domain     = primary_domain,
                                           row_count  = row_count,
                                           max_turns  = settings.session_history_max_turns,
                                          )

        # Update long-term memory
        if (settings.session_history_enabled and session_id and (row_count > 0)):
            updated_lt = _extract_facts_for_long_term(query      = redacted_query, 
                                                      answer     = final_state.get("answer", ""),
                                                      domain     = primary_domain, 
                                                      row_count  = row_count, 
                                                      lt_current = lt_memory,
                                                     )
            if updated_lt:
                await _store_save_long_term(session_id = session_id, 
                                            facts      = updated_lt,
                                           )

                logger.debug("Long-term memory updated", 
                             session_id = session_id,
                            )

        databases_queried = final_state.get("planned_databases", [])
        errors            = final_state.get("errors", [])

        logger.info("Query completed", 
                    session_id  = session_id, 
                    domain      = primary_domain,
                    rows        = row_count, 
                    elapsed_ms  = elapsed_ms, 
                    is_cross_db = final_state.get("is_cross_db", False),
                    confidence  = final_state.get("query_confidence", 1.0),
                   )

        return {"answer"            : final_state.get("answer", ""),
                "sql_executed"      : executed_sql,
                "visualization"     : final_state.get("visualization"),
                "data"              : final_state.get("data", []),
                "metrics"           : final_state.get("metrics", {}),
                "reasoning_trace"   : final_state.get("reasoning_trace", []),
                "errors"            : errors,
                "databases_queried" : databases_queried,
                "is_cross_db"       : final_state.get("is_cross_db", False),
                "query_confidence"  : final_state.get("query_confidence", 1.0),
                "retry_count"       : final_state.get("retry_count", 0),
                "execution_time_ms" : elapsed_ms,
               }