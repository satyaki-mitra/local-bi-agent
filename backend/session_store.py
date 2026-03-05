# DEPENDENCIES
import asyncio
import structlog
from typing  import Dict
from typing  import List
from typing  import Optional
from datetime import datetime, timezone


# Setup Logging
logger = structlog.get_logger()


class SessionStore:
    """
    Thread-safe in-memory session history store

    Each session keeps a list of compact HistoryEntry dicts:
        {
            "turn"       : int,
            "timestamp"  : str   (ISO-8601 UTC),
            "query"      : str,
            "answer"     : str   (truncated to 1000 chars),
            "sql"        : str,
            "domain"     : str,
            "row_count"  : int,
        }

    Design notes:
    - One asyncio.Lock PER session → concurrent sessions never block each other.
    - Answers are truncated to 600 chars before storage so history payload stays small inside LLM prompts (~2 000 tokens for 5 turns).
    - Swap for Redis/SQLite without touching orchestrator.py — just re-implement the four public methods below.
    """
    _ANSWER_MAX_CHARS : int = 1000


    def __init__(self):
        self._store       : Dict[str, List[dict]]   = dict()
        self._locks       : Dict[str, asyncio.Lock] = dict()
        self._global_lock                           = asyncio.Lock()


    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """
        Return (creating if necessary) a per-session lock: the global lock is held only during dict mutation — not during data ops
        """
        async with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
                self._store[session_id] = list()

            return self._locks[session_id]


    async def append(self, session_id: str, query: str, answer: str, sql: str = "", domain: str = "unknown", row_count: int = 0, max_turns: int = 20) -> None:
        """
        Append one turn to the session.  Older turns are dropped when len > max_turns
        """
        lock         = await self._get_lock(session_id)

        # Truncate answer so history never bloats the LLM context window
        answer_short = answer[:self._ANSWER_MAX_CHARS]

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

            # Trim to rolling window
            if (len(self._store[session_id]) > max_turns):
                self._store[session_id] = self._store[session_id][-max_turns:]

        logger.debug("Session turn appended",
                     session_id = session_id,
                     turn       = entry["turn"],
                     domain     = domain,
                    )


    async def get(self, session_id: str, last_n: Optional[int] = None) -> List[dict]:
        """
        Return the full history (or last_n turns if specified): returns an empty list for unknown session IDs
        """
        async with self._global_lock:
            if session_id not in self._store:
                return []

        lock = await self._get_lock(session_id)

        async with lock:
            history = list(self._store.get(session_id, []))

        if last_n is not None and last_n > 0:
            history = history[-last_n:]

        return history


    async def clear(self, session_id: str) -> None:
        """
        Delete all history for a session
        """
        lock = await self._get_lock(session_id)

        async with lock:
            self._store[session_id] = []

        logger.info("Session history cleared", session_id = session_id)


    async def all_sessions(self) -> List[str]:
        """
        Return a list of all known session IDs
        """
        async with self._global_lock:
            return list(self._store.keys())


# Module-level singleton — shared across all FastAPI requests in the same process
session_store = SessionStore()