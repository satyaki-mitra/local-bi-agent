# DEPENDENCIES
import re
import structlog
from typing import Optional
from config.settings  import settings
from config.schemas   import SQLValidationResult
from config.constants import PROHIBITED_SQL_KEYWORDS
from config.constants import PROHIBITED_SQL_PATTERNS


logger = structlog.get_logger()


class SQLValidator:
    """
    Validate and sanitise SQL queries before execution

    Defence layers (in order):
      1. Empty-query check
      2. Comment removal         — prevents hiding keywords inside comments
      3. Prohibited keyword scan — word-boundary regex on uppercased SQL (frozenset, O(1))
      4. Prohibited pattern scan — regex patterns from PROHIBITED_SQL_PATTERNS (blocks PG_SLEEP, INFORMATION_SCHEMA, COPY, etc.)
      5. Must start with SELECT or WITH
      6. Multi-statement detection
      7. LIMIT injection         — ensures row cap is always enforced at SQL level
    """
    def __init__(self):
        self.prohibited_keywords = PROHIBITED_SQL_KEYWORDS
        self._prohibited_patterns = [re.compile(p, re.IGNORECASE) for p in PROHIBITED_SQL_PATTERNS]
        self._max_rows = settings.max_sql_rows


    def validate(self, sql: str) -> SQLValidationResult:
        """
        Full validation pipeline: returns SQLValidationResult(is_valid=True, sanitized_sql=...) on success,
        or SQLValidationResult(is_valid=False, error_message=...) on rejection
        """
        if not sql or not sql.strip():
            return SQLValidationResult(is_valid      = False, 
                                       error_message = "Empty SQL query",
                                      )

        logger.debug("Validating SQL", sql_preview = sql[:120])

        # strip comments before any keyword scan
        sanitized = self._remove_comments(sql)
        sql_upper = sanitized.upper()

        # Prohibited keyword scan (frozenset iteration, O(1) per keyword)
        for keyword in self.prohibited_keywords:
            if re.search(r"\b" + re.escape(keyword) + r"\b", sql_upper):
                logger.warning("Prohibited SQL keyword detected", 
                               keyword = keyword,
                              )

                return SQLValidationResult(is_valid      = False,
                                           error_message = f"Prohibited keyword detected: {keyword}",
                                          )

        # Prohibited pattern scan (catches PG_SLEEP, INFORMATION_SCHEMA, COPY, etc.)
        for pattern in self._prohibited_patterns:
            if pattern.search(sanitized):
                logger.warning("Prohibited SQL pattern detected", 
                               pattern = pattern.pattern,
                              )

                return SQLValidationResult(is_valid      = False,
                                           error_message = f"Prohibited SQL pattern detected: {pattern.pattern}",
                                          )

        # Must start with SELECT or WITH
        stripped_upper = sanitized.strip().upper()

        if not (stripped_upper.startswith("SELECT") or stripped_upper.startswith("WITH")):
            return SQLValidationResult(is_valid      = False,
                                       error_message = "Only SELECT or WITH queries are allowed",
                                      )

        # Multi-statement detection
        non_empty = [s.strip() for s in sanitized.split(";") if s.strip()]

        if (len(non_empty) > 1):
            logger.warning("Multiple SQL statements detected")
            return SQLValidationResult(is_valid      = False,
                                       error_message = "Multiple SQL statements are not allowed",
                                      )

        # Inject LIMIT if not already present
        sanitized = self._inject_limit(sanitized)

        logger.info("SQL validation passed")
        return SQLValidationResult(is_valid      = True, 
                                   sanitized_sql = sanitized,
                                  )


    def is_read_only(self, sql: str) -> bool:
        """
        Boolean convenience check: delegates to the full validate() pipeline and returns True only 
        if the query passes ALL validation layers, not just the write-operation check 
        — name is intentionally conservative
        """
        return self.validate(sql).is_valid


    def _remove_comments(self, sql: str) -> str:
        """
        Strip single-line (--) and multi-line (/* */) SQL comments before any
        keyword scan so that 'DR--comment\\nOP' cannot bypass checks
        """
        sql = re.sub(r"--[^\n]*",    "",  sql)
        sql = re.sub(r"/\*.*?\*/",   "",  sql, flags=re.DOTALL)
        return sql.strip()


    def _inject_limit(self, sql: str) -> str:
        """
        Append LIMIT <max_rows> if the query has no LIMIT clause: enforces settings.max_sql_rows at SQL level, 
        not just application level and also handles trailing semicolons gracefully
        """
        # Strip trailing semicolon before checking / appending
        clean = sql.rstrip().rstrip(";").rstrip()

        if not re.search(r"\bLIMIT\b", clean, re.IGNORECASE):
            clean = f"{clean} LIMIT {self._max_rows}"
            logger.debug("LIMIT clause injected", 
                         limit = self._max_rows,
                        )

        return clean


# GLOBAL INSTANCE
sql_validator = SQLValidator()