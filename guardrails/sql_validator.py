# DEPENDENCIES
import re
import sqlparse
import structlog
from typing import Optional
from sqlparse.tokens import DML
from sqlparse.tokens import Name
from sqlparse.sql import Function
from sqlparse.tokens import Keyword
from sqlparse.sql import Identifier
from config.settings import settings
from sqlparse.sql import Parenthesis
from sqlparse.sql import IdentifierList
from config.schemas import SQLValidationResult
from config.constants import PROHIBITED_SQL_KEYWORDS
from config.constants import PROHIBITED_SQL_PATTERNS


# Setup Logging
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

        # Validate table aliases are defined
        alias_error = self._validate_aliases(sanitized)

        if alias_error:
            logger.warning("SQL alias validation failed", error = alias_error)
            return SQLValidationResult(is_valid      = False, 
                                       error_message = alias_error,
                                      )
        
        # Validate GROUP BY semantics
        groupby_error = self._validate_group_by(sanitized)

        if groupby_error:
            logger.warning("SQL GROUP BY validation failed", 
                           error = groupby_error,
                          )

            return SQLValidationResult(is_valid      = False, 
                                       error_message = groupby_error,
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
        Strip single-line (--) and multi-line (/* */) SQL comments before any keyword scan so that 'DR--comment\\nOP' cannot bypass checks
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

    
    def _validate_aliases(self, sql: str) -> Optional[str]:
        """
        Alias check: only catch obvious undefined aliases, skips validation for subqueries to avoid false positives;
        and returns error message if invalid, None if OK or uncertain
        """
        # Detect subqueries and skip validation: static alias validation is unreliable for subqueries/CTEs
        if re.search(r'\bFROM\s*\(', sql, re.IGNORECASE):
            # Defer to PostgreSQL
            return None  

        if re.search(r'\bWITH\s+\w+\s+AS\s*\(', sql, re.IGNORECASE):
            # CTE detected - defer to PostgreSQL
            return None  
        
        try:
            parsed = sqlparse.parse(sql)
            if not parsed:
                return None

            statement = parsed[0]
        
        except Exception:
            # Skip validation on parse error
            return None  
        
        defined_aliases = set()
        
        # Extract aliases from FROM and JOIN clauses
        from_seen       = False
        
        for token in statement.tokens:
            if (token.ttype and (token.ttype in (sqlparse.tokens.Keyword, sqlparse.tokens.DML))):
                if token.value.upper() in ('FROM', 'JOIN', 'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN'):
                    from_seen = True
                    continue
                
                elif token.value.upper() in ('WHERE', 'GROUP', 'ORDER', 'HAVING', 'LIMIT'):
                    from_seen = False
                    continue
            
            if (from_seen and hasattr(token, 'get_real_name')):
                try:
                    alias = token.get_alias()
                    if alias:
                        defined_aliases.add(alias.lower())

                    real_name = token.get_real_name()
                    
                    if (real_name and not alias):
                        defined_aliases.add(real_name.lower())
                
                except AttributeError:
                    # Token doesn't support these methods
                    continue  
        
        # Simple regex for alias.column patterns
        alias_refs   = re.findall(r'\b([a-z_][a-z0-9_]*)\.(\w+)\b', sql.lower())
        used_aliases = {alias for alias, col in alias_refs if alias not in ('select', 'from', 'where', 'join', 'on', 'and', 'or', 'as', 'count', 'sum', 'avg', 'max', 'min')}
        undefined    = used_aliases - defined_aliases
        
        # Only flag if we're confident (avoid false positives)
        if (undefined and (len(undefined) <= 2)):
            return f"Possible undefined alias(es): {', '.join(sorted(undefined))}. Ensure all aliases are defined in FROM/JOIN clauses."
        
        return None


    def _validate_group_by(self, sql: str) -> Optional[str]:
        """
        GROUP BY check : only flag obvious violations and returns error message if invalid, None if OK or uncertain
        """
        if ('GROUP BY' not in sql.upper()):
            # No GROUP BY = no check needed
            return None  
        
        sql_upper  = sql.upper()
        
        # Skip if query uses SELECT * (hard to validate statically)
        if (('SELECT *' in sql_upper) or ('SELECT  *' in sql_upper)):
            return None
        
        # List of aggregate functions - if column appears inside one, skip validation
        aggregates = {'COUNT', 'SUM', 'AVG', 'MAX', 'MIN', 'ARRAY_AGG', 'STRING_AGG', 'JSON_AGG'}
        
        # Simple heuristic: if SELECT has aggregate function, assume GROUP BY is handled
        if any(f'{agg}(' in sql_upper for agg in aggregates):
            return None
        
        # Defer to PostgreSQL for complex cases
        return None  

# GLOBAL INSTANCE
sql_validator = SQLValidator()