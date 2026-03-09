# DEPENDENCIES
import asyncio
import structlog
from typing import Dict
from typing import List
from typing import Tuple
from typing import Optional
from datetime import datetime
from datetime import timezone


# Setup Logging
logger = structlog.get_logger()


class SessionStore:
    """
    Thread-safe in-memory session history store with short-term and long-term memory: each session keeps a list of all turns;
    short-term is the last N turns, long-term is a summarized version of older turns
    """
    _ANSWER_MAX_CHARS: int = 1000

    def __init__(self):
        self._store: Dict[str, List[dict]]   = dict()
        self._locks: Dict[str, asyncio.Lock] = dict()
        self._global_lock                    = asyncio.Lock()


    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """
        Return (creating if necessary) a per-session lock
        """
        async with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
                self._store[session_id] = list()

            return self._locks[session_id]


    async def append(self, session_id: str, query: str, answer: str, sql: str = "", domain: str = "unknown", row_count: int = 0, max_turns: int = 20,) -> None:
        """
        Append one turn to the session. Older turns are kept for long-term summarization,
        but the full list is retained (no rolling deletion) to allow summary generation
        """
        lock         = await self._get_lock(session_id)

        # Truncate answer for storage
        answer_short = answer[: self._ANSWER_MAX_CHARS]
        
        if (len(answer) > self._ANSWER_MAX_CHARS):
            answer_short += "…"

        entry        = {"turn"      : len(self._store[session_id]) + 1,
                        "timestamp" : datetime.now(timezone.utc).isoformat(),
                        "query"     : query,
                        "answer"    : answer_short,
                        "sql"       : sql,
                        "domain"    : domain,
                        "row_count" : row_count,
                       }

        async with lock:
            self._store[session_id].append(entry)

        logger.debug("Session turn appended",
                     session_id = session_id,
                     turn       = entry["turn"],
                     domain     = domain,
                    )


    async def get(self, session_id: str, last_n: Optional[int] = None) -> List[dict]:
        """
        Return the full history (or last_n turns)
        """
        async with self._global_lock:
            if session_id not in self._store:
                return []

        lock = await self._get_lock(session_id)

        async with lock:
            history = list(self._store.get(session_id, []))

        if ((last_n is not None) and (last_n > 0)):
            history = history[-last_n:]

        return history


    async def clear(self, session_id: str) -> None:
        """
        Delete all history for a session
        """
        lock = await self._get_lock(session_id)

        async with lock:
            self._store[session_id] = []
        
        logger.info("Session history cleared", 
                    session_id = session_id,
                   )


    async def get_stats(self, session_id: str) -> Dict:
        """
        Return aggregate statistics for a session
        """
        history = await self.get(session_id)

        if not history:
            return {"session_id"      : session_id,
                    "total_turns"     : 0,
                    "domains_queried" : {},
                    "total_rows"      : 0,
                    "avg_rows"        : 0.0,
                    "first_query_at"  : None,
                    "last_query_at"   : None,
                   }

        domains = dict()

        for entry in history:
            d          = entry.get("domain", "unknown")
            domains[d] = domains.get(d, 0) + 1

        total_rows = sum(entry.get("row_count", 0) for entry in history)
        avg_rows   = round(total_rows / len(history), 2) if history else 0.0

        return {"session_id"      : session_id,
                "total_turns"     : len(history),
                "domains_queried" : domains,
                "total_rows"      : total_rows,
                "avg_rows"        : avg_rows,
                "first_query_at"  : history[0].get("timestamp") if history else None,
                "last_query_at"   : history[-1].get("timestamp") if history else None,
               }


    async def all_sessions(self) -> List[str]:
        """
        Return a list of all known session IDs
        """
        async with self._global_lock:
            return list(self._store.keys())


    async def get_for_prompt(self, session_id: str, short_term_turns: int = 5, max_summary_chars: int = 2000) -> Tuple[List[dict], str]:
        """
        Retrieve history split into:
        - a short-term (recent turns) and 
        - a long-term summary of older turns 
        
        Short-term turns are returned as a list of dicts and long-term summary is a condensed string
        """
        full_history = await self.get(session_id)

        if not full_history:
            return [], ""

        # Split: recent are last short_term_turns, older are the rest
        if (len(full_history) <= short_term_turns):
            short_history = full_history
            long_summary  = ""
       
        else:
            short_history = full_history[-short_term_turns:]
            older         = full_history[:-short_term_turns]

            # Build summary from older turns
            summary_parts = list()

            for turn in older:
                # Format: "Q: ... A: ..." (maybe add timestamp)
                summary_parts.append(f"Q: {turn['query']}\nA: {turn['answer']}\n")

            long_summary = "".join(summary_parts)

            # Truncate to max length
            if (len(long_summary) > max_summary_chars):
                long_summary = long_summary[:max_summary_chars] + "…"

        return short_history, long_summary


# GLOBAL INSTANCE
session_store = SessionStore()