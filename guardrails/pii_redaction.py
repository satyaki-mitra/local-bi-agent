# DEPENDENCIES
import re
import structlog
from typing import Any
from typing import Dict
from typing import List
from config.constants import PII_PATTERNS
from config.schemas import PIIRedactionResult


# Setup Logging
logger = structlog.get_logger()


class PIIRedactor:
    """
    Redact PII from free text and structured record data

    - Patterns are driven exclusively by PII_PATTERNS in config/constants.py
    - Adding a new pattern there automatically picks it up here — no code change needed
    """
    # Replacement tokens keyed by pattern name — extensible alongside PII_PATTERNS
    _REPLACEMENTS: Dict[str, str] = {"ssn"         : "***-**-****",
                                     "credit_card" : "****-****-****-****",
                                     "phone"       : "***-***-****",
                                    }


    def __init__(self):
        # Pre-compile all patterns once for performance
        self._compiled = {name : re.compile(pattern, re.IGNORECASE) for name, pattern in PII_PATTERNS.items()}


    def redact(self, text: str) -> PIIRedactionResult:
        """
        Apply all PII patterns to `text`: Safe to call on both user input (before LLM) and LLM output
        """
        if not text:
            return PIIRedactionResult(sanitized_text = text or "", 
                                      was_redacted   = False,
                                     )

        was_redacted       = False
        patterns_triggered = list()

        for name, compiled in self._compiled.items():
            replacement = self._get_replacement(name)
            text, n     = compiled.subn(replacement, text)

            if (n > 0):
                was_redacted = True

                patterns_triggered.append(name)
                logger.warning(f"{name.upper()} redacted", count = n)

        return PIIRedactionResult(sanitized_text     = text,
                                  was_redacted       = was_redacted,
                                  patterns_triggered = patterns_triggered,
                                 )


    def redact_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply redact() to every string value in a result row dict: fixes the gap where query result rows (List[Dict]) were not being scanned

        Usage (in orchestrator, after DB query returns): clean_rows = [pii_redactor.redact_record(row) for row in raw_rows]
        """
        cleaned = dict()

        for key, value in record.items():
            if isinstance(value, str):
                cleaned[key] = self.redact(value).sanitized_text
            
            else:
                cleaned[key] = value

        return cleaned


    def redact_records(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convenience wrapper — applies redact_record() across a full result set

        Usage: clean_data = pii_redactor.redact_records(raw_data)
        """
        return [self.redact_record(row) for row in records]


    def _get_replacement(self, pattern_name: str):
        """
        Return the replacement string or callable for a given pattern name
        
        - Email uses a callable to preserve TLD context; all others use static strings
        - Falls back to '[REDACTED]' for any pattern not in _REPLACEMENTS
        """
        if (pattern_name == "email"):
            return self._redact_email

        return self._REPLACEMENTS.get(pattern_name, "[REDACTED]")


    @staticmethod
    def _redact_email(match: re.Match) -> str:
        """
        Preserve TLD for readability (e.g. ***@***.com) while masking identity
        """
        parts = match.group(0).split("@")

        if (len(parts) == 2):
            tld_parts = parts[1].split(".")
            
            if (len(tld_parts) >= 2):
                return f"***@***.{tld_parts[-1]}"

        return "***@***.com"


# GLOBAL INSTANCE
pii_redactor = PIIRedactor()