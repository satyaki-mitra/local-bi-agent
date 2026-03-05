# DEPENDENCIES
from enum import Enum


class DatabaseType(str, Enum):
    """
    Supported database types
    """
    HEALTH  = "health"
    FINANCE = "finance"
    SALES   = "sales"
    IOT     = "iot"


class AgentRole(str, Enum):
    """
    Agent roles in the multi-agent system
    """
    SUPERVISOR     = "supervisor"
    HEALTH_AGENT   = "health_agent"
    FINANCE_AGENT  = "finance_agent"
    SALES_AGENT    = "sales_agent"
    IOT_AGENT      = "iot_agent"
    ANALYST_AGENT  = "analyst_agent"


class ChartType(str, Enum):
    """
    Supported visualization types
    """
    BAR        = "bar"
    LINE       = "line"
    SCATTER    = "scatter"
    HISTOGRAM  = "histogram"
    HEATMAP    = "heatmap"
    PIE        = "pie"
    BOX        = "box"
    AUTO       = "auto"


# SQL Keywords that are prohibited
PROHIBITED_SQL_KEYWORDS      : frozenset   = frozenset(["DROP", "DELETE", "UPDATE", "INSERT", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE", "EXECUTE", "COMMIT", "ROLLBACK", "SAVEPOINT"])

ALLOWED_SQL_KEYWORDS         : frozenset   = frozenset(["SELECT", "FROM", "WHERE", "JOIN", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "OFFSET", "AS", "WITH", "UNION", "COUNT", "SUM", "AVG", "MAX", "MIN", "CAST", "COALESCE", "EXTRACT", "DATE_TRUNC", "AND", "OR", "NOT", "IN", "IS", "NULL"])

# Regex patterns targeting structural SQL injection vectors
PROHIBITED_SQL_PATTERNS      : list        = [r"\bINFORMATION_SCHEMA\b", r"\bPG_CATALOG\b", r"\bPG_SLEEP\b", r"\bCOPY\b", r"\bLO_\w+\b", r";\s*--", r"\bXP_\w+\b"]

# PII Patterns for redaction
PII_PATTERNS                 : dict        = {"ssn"         : r"\b\d{3}-\d{2}-\d{4}\b",
                                              "credit_card" : r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",
                                              "email"       : r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
                                              "phone"       : r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b",
                                             }

# Columns unconditionally stripped before any result reaches the user
BLOCKED_OUTPUT_COLUMNS       : set         = {# Auth / credentials
                                              "password", "password_hash", "hashed_password", "api_key", "secret_key", "access_token", "refresh_token", "auth_token",
                                              # Hard PII
                                              "ssn", "social_security_number", "national_id", "passport_number", "drivers_license", "date_of_birth", "dob", "full_name", "home_address", "ip_address",
                                              # Payment raw values
                                              "credit_card_number", "card_number", "cvv", "bank_account_number", "routing_number",
                                              # Internal operational
                                              "internal_notes", "admin_comment", "raw_payload", "debug_info",
                                             }

# Tool Names
DB_TOOLS                     : dict        = {DatabaseType.HEALTH  : "query_health_db",
                                              DatabaseType.FINANCE : "query_finance_db",
                                              DatabaseType.SALES   : "query_sales_db",
                                              DatabaseType.IOT     : "query_iot_db",
                                             }

# Domain → Table Map
DOMAIN_TABLE_MAP             : dict        = {DatabaseType.SALES   : ["OPPORTUNITIES", "LEADS", "SALES_REPS"],
                                              DatabaseType.FINANCE : ["TRANSACTIONS", "SUBSCRIPTIONS", "PAYMENT_FAILURES"],
                                              DatabaseType.HEALTH  : ["CLAIMS", "PROCEDURES", "PATIENT_HISTORY"],
                                              DatabaseType.IOT     : ["DAILY_STEPS", "HEART_RATE_AVG", "SLEEP_HOURS"],
                                             }

# Supervisor Routing
SUPERVISOR_ROUTING_KEYWORDS  : dict        = {"finance" : ["finance", "revenue", "transaction", "payment", "subscription", "fee", "invoice"],
                                              "health"  : ["health", "patient", "claim", "diagnosis", "procedure", "medical", "insurance"],
                                              "iot"     : ["step", "heart", "sleep", "iot", "wearable", "smartwatch", "fitness"],
                                              "sales"   : ["sale", "opportunity", "lead", "quota", "rep", "pipeline", "crm"],
                                             }

SUPERVISOR_DEFAULT_DATABASE  : DatabaseType = DatabaseType.SALES

# Cross-DB allowed domain pairs: used when settings.enable_cross_db_joins = True
# frozenset-of-frozensets so {"health","finance"} == {"finance","health"} (order-independent)
# Only these four pairs are permitted — all others fall back to single-domain routing
ALLOWED_CROSS_DB_PAIRS       : frozenset   = frozenset({frozenset({"health", "finance"}),
                                                        frozenset({"finance", "sales"}),
                                                        frozenset({"iot", "health"}),
                                                        frozenset({"sales", "iot"}),
                                                       })

# Analysis Thresholds
ANALYSIS_MAX_VALUE_COUNTS                  = 10
ANALYSIS_MAX_OUTLIER_VALUES                = 20
ANALYSIS_CORRELATION_TOP_N                 = 10
ANALYSIS_STRONG_CORR_POS                   = 0.7
ANALYSIS_STRONG_CORR_NEG                   = -0.7
ANALYST_DATA_TABLE_ROWS                    = 50
ANALYST_METRICS_ID_EXCLUDE                 = ("id",)

# Visualization Constants
VIZ_BAR_MAX_CATEGORIES                     = 20
VIZ_HBAR_MAX_CATEGORIES                    = 15
VIZ_HBAR_LABEL_LEN_THRESHOLD               = 12
VIZ_HISTOGRAM_MAX_BINS                     = 30
VIZ_TITLE_MAX_LEN                          = 60
VIZ_LINE_XTICK_THRESHOLD                   = 8
VIZ_BAR_XTICK_THRESHOLD                    = 5

VIZ_PALETTE                  : dict        = {"bg_figure"  : "#0D1117",
                                              "bg_axes"    : "#161B22",
                                              "grid"       : "#21262D",
                                              "text_main"  : "#E6EDF3",
                                              "text_muted" : "#8B949E",
                                              "accent"     : "#00D4AA",
                                              "accent_2"   : "#1F6FEB",
                                              "accent_3"   : "#D29922",
                                              "series"     : ["#00D4AA", "#1F6FEB", "#D29922", "#F85149", "#3FB950", "#58A6FF", "#BC8CFF", "#FF7B72"],
                                             }

VIZ_FONT_MAIN                              = "DejaVu Sans"

# Export
EXPORT_MAX_FILENAME_LEN                    = 30

# HTML Export Style Tokens
HTML_ACCENT_COLOR                          = VIZ_PALETTE["accent"]
HTML_BG_COLOR                              = VIZ_PALETTE["bg_figure"]
HTML_SURFACE_COLOR                         = VIZ_PALETTE["bg_axes"]
HTML_BORDER_COLOR                          = VIZ_PALETTE["grid"]
HTML_TEXT_COLOR                            = VIZ_PALETTE["text_main"]
HTML_MUTED_COLOR                           = VIZ_PALETTE["text_muted"]

# Result / Text Rendering
RESULT_BRANDING_NAME                       = "LocalGenBI"
RESULT_BRANDING_TAGLINE                    = "Autonomous BI Platform"
RESULT_MARKDOWN_TOP_RECORDS                = 5000

# LLM Output Post-processing
THINK_TAG_PATTERN                          = r"<think>.*?</think>"

# Error / Success Messages
ERROR_MESSAGES               : dict        = {"db_connection"  : "Failed to connect to database. Please check connection settings.",
                                              "sql_syntax"     : "SQL syntax error. The agent will attempt to correct and retry.",
                                              "timeout"        : "Query execution timeout. Please try a more specific query.",
                                              "pii_detected"   : "PII detected in output. Automatic redaction applied.",
                                              "prohibited_sql" : "Query contains prohibited SQL operations.",
                                              "ollama_offline" : "LLM service is not responding. Please check Ollama container.",
                                              "max_retries"    : "Maximum retry attempts reached. Please rephrase your query.",
                                             }

SUCCESS_MESSAGES             : dict        = {"query_complete"        : "Query executed successfully.",
                                              "visualization_created" : "Visualization generated.",
                                              "data_merged"           : "Data from multiple sources merged successfully.",
                                             }