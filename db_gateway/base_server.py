# DEPENDENCIES
import re
import ssl
import time
import asyncpg
import structlog
from uuid import UUID
from typing import Any
from typing import Dict
from typing import List
from datetime import date
from typing import Optional
from decimal import Decimal
from datetime import datetime
from datetime import timedelta
from datetime import time as dt_time
from config.settings import settings
from config.schemas import DbToolResult
from guardrails.pii_redaction import pii_redactor
from guardrails.sql_validator import sql_validator
from config.constants import BLOCKED_OUTPUT_COLUMNS


# Setup Logging
logger = structlog.get_logger()


def _build_ssl_context(ssl_mode: str):
    """
    Convert settings.db_ssl_mode string to a value asyncpg accepts:
    - asyncpg does not accept raw strings like 'require' or 'verify-full'; it expects: False | True | ssl.SSLContext
    - 'disable'     → None  (no SSL)
    - 'require'     → True  (SSL, no cert verification — encrypted transport only)
    - 'verify-full' → ssl.SSLContext with check_hostname + verify_mode CERT_REQUIRED
    """
    if (ssl_mode == "disable"):
        return None

    if (ssl_mode == "require"):
        return True

    if (ssl_mode == "verify-full"):
        ctx                  = ssl.create_default_context()
        ctx.check_hostname   = True
        ctx.verify_mode      = ssl.CERT_REQUIRED
        return ctx

    # Unknown value — log and fall back to no SSL rather than crashing
    logger.warning("Unknown db_ssl_mode; defaulting to no SSL",
                   ssl_mode = ssl_mode,
                  )
    return None


class BaseDbServer:
    """
    Base class for all domain database gateway servers

    Security pipeline per query:
      1. sql_validator.validate()      — keyword + pattern + LIMIT injection
      2. asyncpg parameterized fetch   — no string interpolation on user SQL
      3. _strip_blocked_columns()      — remove BLOCKED_OUTPUT_COLUMNS from every row
      4. _serialize_row()              — type-safe JSON conversion + per-value PII redaction
    """
    def __init__(self, host: str, port: int, database: str, user: str, password: str, server_name: str):
        self.host                         = host
        self.port                         = port
        self.database                     = database
        self.user                         = user
        self.password                     = password
        self.server_name                  = server_name
        self.pool: Optional[asyncpg.Pool] = None


    async def connect(self) -> None:
        """
        Initialize the asyncpg connection pool
        """
        try:
            self.pool = await asyncpg.create_pool(
                host                             = self.host,
                port                             = self.port,
                database                         = self.database,
                user                             = self.user,
                password                         = self.password,
                min_size                         = settings.db_pool_min_size,
                max_size                         = settings.db_pool_max_size,
                command_timeout                  = settings.sql_timeout_seconds,
                max_inactive_connection_lifetime = settings.db_pool_max_inactive_lifetime,
                ssl                              = _build_ssl_context(settings.db_ssl_mode),
                max_queries                      = settings.db_pool_max_queries,
            )

            logger.info("Gateway connected",
                        server   = self.server_name,
                        database = self.database,
                       )

        except Exception as e:
            logger.error("Failed to connect to database",
                         server = self.server_name,
                         error  = str(e),
                        )
            raise


    async def disconnect(self) -> None:
        """
        Close the connection pool gracefully
        """
        if self.pool:
            await self.pool.close()
            logger.info("Gateway disconnected",
                        server = self.server_name,
                       )


    @staticmethod
    def _strip_blocked_columns(row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Last line of defence — even if the LLM generates a query that accidentally SELECTs a sensitive column, 
        it will be stripped here before any result leaves this layer
        """
        return {k: v for k, v in row.items() if k.lower() not in BLOCKED_OUTPUT_COLUMNS}


    def _serialize_value(self, value: Any) -> Any:
        """
        Recursively convert all PostgreSQL types to JSON-safe Python types

        - PII redaction is applied at the string-fallback level (last resort)
        - Structured redaction across full rows is handled by _strip_blocked_columns
          and pii_redactor.redact_records() in execute_query()
        """
        if value is None:
            return None

        # Numeric & Boolean — safe, return directly
        if isinstance(value, Decimal):
            return float(value)

        if isinstance(value, (int, float, bool)):
            return value

        # Date / Time
        if isinstance(value, (datetime, date, dt_time)):
            return value.isoformat()

        if isinstance(value, timedelta):
            return value.total_seconds()

        # UUID
        if isinstance(value, UUID):
            return str(value)

        # Arrays / Lists
        if isinstance(value, (list, tuple)):
            return [self._serialize_value(item) for item in value]

        # Dicts
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}

        # Bytes
        if isinstance(value, (bytes, bytearray)):
            return value.hex()

        # PostgreSQL Range types — guard against str which also has .lower() / .upper()
        if (not isinstance(value, str) and hasattr(value, "lower") and hasattr(value, "upper")):
            return {"lower" : self._serialize_value(value.lower),
                    "upper" : self._serialize_value(value.upper),
                   }

        # Default: stringify and apply PII redaction as a safety net
        return pii_redactor.redact(str(value)).sanitized_text


    def _serialize_row(self, row: asyncpg.Record) -> Dict[str, Any]:
        """
        Serialize one asyncpg Record to a JSON-safe dict
        """
        return {key: self._serialize_value(value) for key, value in dict(row).items()}


    async def execute_query(self, sql: str) -> DbToolResult:
        """
        Full security + execution pipeline: validate → execute → strip blocked columns → serialize → return
        """
        if not self.pool:
            return DbToolResult(success = False,
                                error   = "Database connection not initialized",
                               )

        start_time = time.time()

        # Validate (sql_validator injects LIMIT internally)
        validation = sql_validator.validate(sql)

        if not validation.is_valid:
            logger.warning("Query rejected",
                           server = self.server_name,
                           reason = validation.error_message,
                          )

            return DbToolResult(success = False,
                                error   = validation.error_message,
                               )

        final_sql = validation.sanitized_sql

        try:
            async with self.pool.acquire() as conn:
                logger.info("Executing SQL",
                            server      = self.server_name,
                            sql_preview = final_sql[:200],
                           )

                rows = await conn.fetch(final_sql)

            # Serialize all asyncpg types to JSON-safe Python
            serialized     = [self._serialize_row(row) for row in rows]

            # Strip blocked columns (passwords, SSNs, tokens, etc.)
            stripped       = [self._strip_blocked_columns(row = row) for row in serialized]

            # Apply structured PII redaction across all string values in every row
            data           = pii_redactor.redact_records(stripped)
            execution_time = int((time.time() - start_time) * 1000)

            logger.info("Query executed",
                        server  = self.server_name,
                        rows    = len(data),
                        time_ms = execution_time,
                       )

            return DbToolResult(success           = True,
                                data              = data,
                                row_count         = len(data),
                                execution_time_ms = execution_time,
                               )

        except asyncpg.PostgresSyntaxError as e:
            return DbToolResult(success = False,
                                error   = f"SQL syntax error: {str(e)}",
                               )

        except asyncpg.QueryCanceledError:
            return DbToolResult(success = False,
                                error   = f"Query timed out after {settings.sql_timeout_seconds}s",
                               )

        except Exception as e:
            logger.error("Query execution failed",
                         server = self.server_name,
                         error  = str(e),
                        )

            return DbToolResult(success = False,
                                error   = f"Query failed: {str(e)}",
                               )


    async def get_schema(self, table_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Return schema metadata for LLM context injection: uses parameterized query — safe against SQL injection on table_name
        and queries information_schema directly (internal method, bypasses sql_validator)
        """
        if not self.pool:
            return {"success" : False,
                    "error"   : "Database connection not initialized",
                   }

        try:
            async with self.pool.acquire() as conn:
                query = """
                            SELECT
                                t.table_name,
                                c.column_name,
                                c.data_type,
                                c.is_nullable
                            FROM information_schema.tables  t
                            JOIN information_schema.columns c
                                ON t.table_name = c.table_name
                            WHERE t.table_schema = 'public'
                            AND t.table_type  = 'BASE TABLE'
                            AND ($1::text IS NULL OR t.table_name = $1)
                            ORDER BY t.table_name, c.ordinal_position;
                        """
                rows  = await conn.fetch(query, table_name)

            if not rows:
                return {"success" : False,
                        "error"   : "No schema information found",
                       }

            schema_map: Dict[str, List[Dict]] = dict()

            for r in rows:
                name = r["table_name"]
                schema_map.setdefault(name, []).append({"column" : r["column_name"],
                                                        "type"   : r["data_type"],
                                                        "null"   : r["is_nullable"],
                                                       })

            return {"success"  : True,
                    "database" : self.database,
                    "tables"   : schema_map,
                   }

        except Exception as e:
            logger.error("Schema retrieval failed",
                         server = self.server_name,
                         error  = str(e),
                        )

            return {"success" : False,
                    "error"   : str(e),
                   }


    async def get_table_sample(self, table_name: str, limit: int = 5) -> Dict[str, Any]:
        """
        Return sample rows for LLM pattern recognition: table name is validated by regex before use; quoted for safety
        """
        if not self.pool:
            return {"success" : False,
                    "error"   : "Database connection not initialized",
                   }

        if not table_name or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
            return {"success" : False,
                    "error"   : f"Invalid table name: '{table_name}'",
                   }

        safe_limit = min(max(1, limit), 100)

        try:
            async with self.pool.acquire() as conn:
                query = f'SELECT * FROM "{table_name}" LIMIT $1'
                rows  = await conn.fetch(query, safe_limit)

            serialized = [self._serialize_row(row) for row in rows]
            stripped   = [self._strip_blocked_columns(row = row) for row in serialized]
            data       = pii_redactor.redact_records(stripped)

            return {"success"     : True,
                    "table"       : table_name,
                    "sample_data" : data,
                    "row_count"   : len(data),
                   }

        except Exception as e:
            logger.error("Sample retrieval failed",
                         server = self.server_name,
                         error  = str(e),
                        )

            return {"success" : False,
                    "error"   : str(e),
                   }


    async def handle_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Route incoming gateway requests to the correct internal method
        """
        logger.info("Handling request",
                    server = self.server_name,
                    method = method,
                   )

        if (method == "query_database"):
            result = await self.execute_query(sql = params.get("sql", ""))
            return result.model_dump()

        elif (method == "get_schema"):
            return await self.get_schema(table_name = params.get("table"))

        elif (method == "get_table_sample"):
            return await self.get_table_sample(table_name = params.get("table", ""),
                                               limit      = params.get("limit", 5),
                                              )

        else:
            return {"success" : False,
                    "error"   : f"Unknown method: '{method}'",
                   }