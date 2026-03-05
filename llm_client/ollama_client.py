# DEPENDENCIES
import re
import json
import httpx
import structlog
from typing import Any
from typing import List
from typing import Dict
from tenacity import retry
from typing import Optional
from typing import AsyncGenerator
from config.settings  import settings
from tenacity import wait_exponential
from tenacity import stop_after_attempt
from tenacity import retry_if_exception_type
from config.constants import THINK_TAG_PATTERN


logger = structlog.get_logger()


class OllamaClient:
    """
    Async HTTP client for Ollama-hosted models (DeepSeek-R1, Llama 3, Mistral, etc.)

    Features:
    - temperature uses 'is not None' guard on all public methods
    - num_predict passed consistently on complete, complete_with_tools, stream_complete
    - DeepSeek-R1 <think> tags stripped automatically from all non-streaming responses
    - per-call temperature override on complete_with_tools (consistent with complete)
    - tenacity retry on transient httpx errors (3 attempts, exponential backoff)
    - ping() for lightweight Ollama health checks
    - close() is idempotent and safe to call multiple times
    """
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None, temperature : Optional[float] = None, timeout: Optional[float] = None):
        self.base_url    = base_url    or settings.ollama_host
        self.model       = model       or settings.ollama_model
        self.temperature = temperature if temperature is not None else settings.ollama_temperature
        self._client     = httpx.AsyncClient(timeout = timeout or settings.db_query_timeout_seconds)
        self._closed     = False


    # Context manager
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


    # Internal helpers

    @retry(retry = retry_if_exception_type(httpx.HTTPError), stop = stop_after_attempt(3), wait = wait_exponential(multiplier = 1, min = 1, max = 10), reraise = True)
    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST to /api/chat with automatic retry on transient HTTP errors: exponential back-off: 1s → 2s → 4s (capped at 10s)
        """
        response = await self._client.post(f"{self.base_url}/api/chat", 
                                           json = payload,
                                          )
        response.raise_for_status()
        return response.json()


    def _make_messages(self, prompt: str, system_prompt: Optional[str] = None) -> List[Dict[str, str]]:
        messages = list()

        if system_prompt:
            messages.append({"role"    : "system", 
                             "content" : system_prompt,
                           })

        messages.append({"role"    : "user", 
                         "content" : prompt,
                       })

        return messages


    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """
        Remove <think>...</think> reasoning traces from LLM output: uses THINK_TAG_PATTERN from config.constants — single source of truth
        """
        return re.sub(THINK_TAG_PATTERN, "", text, flags = re.DOTALL).strip()


    # Public API
    async def complete(self, prompt: str, system_prompt: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
        """
        Non-streaming completion. Returns assistant text with <think> tags stripped
        """
        resolved_temp   = temperature if temperature is not None else self.temperature
        resolved_tokens = max_tokens  if max_tokens  is not None else settings.ollama_max_tokens

        payload         = {"model"    : self.model,
                           "messages" : self._make_messages(prompt, system_prompt),
                           "stream"   : False,
                           "options"  : {"temperature" : resolved_temp,
                                         "num_predict" : resolved_tokens,
                                        },
                          } 

        try:
            result       = await self._post(payload)
            content      = result["message"]["content"]
            # Strip reasoning traces before returning
            final_result = self._strip_think_tags(content)

            return final_result 

        except httpx.HTTPError as e:
            logger.error("Ollama completion failed", 
                         error = str(e), 
                         model = self.model,
                        )
            raise


    async def complete_with_tools(self, prompt: str, tools: List[Dict[str, Any]], system_prompt: Optional[str] = None, temperature: Optional[float] = None) -> Dict[str, Any]:
        """
        Tool-use completion. Returns {content, tool_calls} with <think> tags stripped from content
        """
        resolved_temp = temperature if temperature is not None else self.temperature

        payload       = {"model"    : self.model,
                         "messages" : self._make_messages(prompt, system_prompt),
                         "tools"    : tools,
                         "stream"   : False,
                         "options"  : {"temperature" : resolved_temp,
                                       "num_predict" : settings.ollama_max_tokens,
                                      },
                        }

        try:
            result        = await self._post(payload)
            assistant     = result["message"]
            content       = assistant.get("content", "")
            final_content = self._strip_think_tags(content)

            return {"content"    : final_content,
                    "tool_calls" : assistant.get("tool_calls", []),
                   }

        except httpx.HTTPError as e:
            logger.error("Ollama tool completion failed", 
                         error = str(e), 
                         model = self.model,
                        )
            raise


    async def stream_complete(self, prompt: str, system_prompt: Optional[str] = None, max_tokens: Optional[int] = None) -> AsyncGenerator[str, None]:
        """
        Streaming completion — yields token chunks as they arrive

        - Streaming responses are NOT retried on failure — httpx streaming connections cannot be transparently replayed once partially consumed
        - The caller is responsible for retry logic at the application level
        - <think> tag stripping is NOT applied here — think tokens arrive as partial chunks and cannot be reliably stripped mid-stream
        - Filter at the consumer level if needed (buffer the full response, then strip)
        """
        resolved_tokens = max_tokens if max_tokens is not None else settings.ollama_max_tokens

        payload         = {"model"    : self.model,
                           "messages" : self._make_messages(prompt, system_prompt),
                           "stream"   : True,
                           "options"  : {"temperature" : self.temperature,
                                         "num_predict" : resolved_tokens,
                                        },
                          }

        try:
            async with self._client.stream("POST", f"{self.base_url}/api/chat", json = payload) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line:
                        data = json.loads(line)

                        if (("message" in data) and ("content" in data["message"])):
                            yield data["message"]["content"]

        except httpx.HTTPError as e:
            logger.error("Ollama streaming failed", 
                         error = str(e), 
                         model = self.model,
                        )
            raise


    # Light-weight health check that doesn't require a full completion
    async def ping(self) -> bool:
        """
        Check if Ollama is reachable by calling /api/tags (lists loaded models)
        
        - Returns True if reachable, False otherwise. 
        - Never raises.
        - Used by the FastAPI /health endpoint and the orchestrator pre-flight check
        """
        try:
            response = await self._client.get(f"{self.base_url}/api/tags")
            return response.status_code == 200

        except Exception as e:
            logger.warning("Ollama ping failed", error=str(e))
            return False


    # Lifecycle
    async def close(self) -> None:
        """
        Close the underlying HTTP client. Idempotent — safe to call twice
        """
        if not self._closed:
            await self._client.aclose()
            self._closed = True