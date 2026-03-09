# DEPENDENCIES
import re
import sys
import json
import time
import httpx
import asyncio
import argparse
import structlog
import sentry_sdk
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple
from pathlib import Path
from typing import Optional
from deepeval.test_case import LLMTestCase
from deepeval.metrics import FaithfulnessMetric
from deepeval.metrics import AnswerRelevancyMetric
from deepeval.models.base_model import DeepEvalBaseLLM

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import settings


# Setup Logging
logger = structlog.get_logger()


# init with no DSN disables all telemetry
sentry_sdk.init() 


# Ollama evaluator model (used by DeepEval answer quality metrics only): this is separate from the inference model (llama3:8b) used by the backend for SQL generation
# Deliberately uses a different model (mistral:7b by default) to avoid self-consistency bias
class OllamaEvaluator(DeepEvalBaseLLM):
    def __init__(self, model: str, host: str = "http://localhost:11434"):
        self.model        = model
        self.host         = host
        self._client      = None
        self._sync_client = None

    
    @staticmethod
    def _extract_json(text: str) -> str:
        """
        Extract first valid JSON object from text, handling markdown/code blocks and nested structures;
        and returns the original text if no valid JSON is found
        """
        text = text.strip()
        
        # Try direct parse first
        try:
            json.loads(text)
            return text

        except (json.JSONDecodeError, TypeError):
            pass
        
        # Strip markdown code fences
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()
        
        # Try parsing after stripping markdown
        try:
            json.loads(text)
            return text

        except (json.JSONDecodeError, TypeError):
            pass
        
        # Find JSON object using brace-matching (handles nested braces)
        def find_json_object(s: str) -> Optional[str]:
            """
            Find first complete {...} or [...] block using brace counting
            """
            for start_idx, char in enumerate(s):
                if char in ('{', '['):
                    end_char    = '}' if char == '{' else ']'
                    depth       = 0
                    in_string   = False
                    escape_next = False
                    
                    for idx in range(start_idx, len(s)):
                        c = s[idx]
                        
                        if escape_next:
                            escape_next = False
                            continue

                        if (c == '\\'):
                            escape_next = True
                            continue

                        if ((c == '"') and not escape_next):
                            in_string = not in_string
                            continue

                        if in_string:
                            continue
                        
                        if (c == char):
                            depth += 1

                        elif (c == end_char):
                            depth -= 1

                            if (depth == 0):
                                candidate = s[start_idx:idx+1]
                                try:
                                    json.loads(candidate)
                                    return candidate

                                except (json.JSONDecodeError, TypeError):
                                    # Keep searching
                                    pass
                    
                    # Only check first opening brace/bracket
                    break  

            return None
        
        # Try object extraction
        extracted = find_json_object(text)
        if extracted:
            return extracted
        
        # Last resort: return original text and let DeepEval handle the error
        return text

    
    @staticmethod
    def _wrap_prompt_for_json(prompt: str) -> str:
        """
        Wrap DeepEval's prompt to enforce JSON-only output from Evaluation LLM
        """
        return (f"You are an industry expert and strict evaluation assistant who only gives response in JSON structure and within token budget.\n"
                f"Output ONLY a valid and complete JSON object. No markdown, no prose, no explanations, no code fences, no trucation.\n"
                f"Your entire response must be strictly and directly parseable by json.loads() as-is.\n\n"
                f"Now complete this evaluation and output only the JSON:\n\n"
                f"{prompt}"
               )


    def _get_sync_client(self):
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout = httpx.Timeout(1000.0),
                                             limits  = httpx.Limits(max_keepalive_connections = 5,
                                                                    max_connections           = 10,
                                                                    keepalive_expiry          = 120.0,
                                                                   )
                                            )

        return self._sync_client


    def _get_async_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout = httpx.Timeout(1000.0),
                                             limits  = httpx.Limits(max_keepalive_connections = 5,
                                                                    max_connections           = 10,
                                                                    keepalive_expiry          = 120.0,
                                                                   )
                                            )

        return self._client


    def load_model(self):
        return self.model


    def generate(self, prompt: str) -> str:
        """
        Synchronous generate with retry logic: never returns empty string and raises on all failures so DeepEval
        can handle errors correctly rather than receiving an empty string it tries to JSON-parse
        """
        last_error = None

        for attempt in range(5):
            try:
                client         = self._get_sync_client()
                wrapped_prompt = self._wrap_prompt_for_json(prompt)
                response       = client.post(f"{self.host}/api/generate",
                                             json = {"model"   : self.model,
                                                     "prompt"  : wrapped_prompt,
                                                     "stream"  : False,
                                                     "options" : {"num_predict" : 1536,
                                                                  "temperature" : 0.2,
                                                                 },
                                                    }
                                            )

                response.raise_for_status()

                result         = response.json().get("response", "")
                result         = self._extract_json(text = result)

                if result:
                    return result

                # Empty response from Ollama — retry
                last_error = ValueError("Ollama returned empty response")

                logger.warning("Empty response from Ollama evaluator",
                               attempt = attempt + 1,
                               model   = self.model,
                              )

            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_error = e

                logger.warning("Timeout in evaluator generate",
                               attempt = attempt + 1,
                               error   = str(e),
                              )

            except Exception as e:
                # Non-timeout errors: log and raise immediately, don't retry
                logger.error("Unexpected error in evaluator generate",
                             error = str(e),
                            )
                raise

            if (attempt < 4):
                time.sleep(2 ** attempt)

        raise RuntimeError(f"Evaluator generate failed after 5 attempts: {last_error}")


    async def a_generate(self, prompt: str) -> str:
        """
        Asynchronous generate with retry logic, never returns empty string — raises on all failures so DeepEval
        can handle errors correctly rather than receiving an empty string it tries to JSON-parse
        """
        last_error = None

        for attempt in range(5):
            try:
                client   = self._get_async_client()
                response = await client.post(f"{self.host}/api/generate",
                                             json = {"model"   : self.model,
                                                     "prompt"  : prompt,
                                                     "stream"  : False,
                                                     "options" : {"num_predict" : 1536,
                                                                  "temperature" : 0.2,
                                                                 },
                                                    }
                                            )

                response.raise_for_status()
                result = response.json().get("response", "")
                result = self._extract_json(text = result)

                if result:
                    return result

                # Empty response — retry
                last_error = ValueError("Ollama returned empty response")
                logger.warning("Empty response from Ollama evaluator",
                               attempt = attempt + 1,
                               model   = self.model,
                              )

            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_error = e
                logger.warning("Timeout in evaluator a_generate",
                               attempt = attempt + 1,
                               error   = str(e),
                              )

            except Exception as e:
                # Non-timeout errors: log and raise immediately
                logger.error("Unexpected error in evaluator a_generate",
                             error = str(e),
                            )
                raise

            if (attempt < 4):
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Evaluator a_generate failed after 5 attempts: {last_error}")


    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

        if self._sync_client:
            self._sync_client.close()
            self._sync_client = None


    def get_model_name(self) -> str:
        return self.model


# Constants
DEFAULT_DATASET_PATH   = Path(__file__).parent / "golden_dataset.json"
DEFAULT_BACKEND_URL    = "http://localhost:8001"
REQUEST_TIMEOUT        = 1000.0                  # seconds — LLM inference is slow on CPU hardware
BATCH_DELAY            = 2.0                     # seconds between requests to avoid thundering herd on Ollama

RELEVANCY_THRESHOLD    = 0.7
FAITHFULNESS_THRESHOLD = 0.7

DOMAINS                = {"health", "finance", "sales", "iot", "cross_db_routing"}


# Dataset loader
def load_golden_dataset(path: Path = DEFAULT_DATASET_PATH) -> List[Dict[str, Any]]:
    """
    Load and validate the golden dataset JSON

    - Raises FileNotFoundError if the file is missing
    - Raises ValueError if the JSON structure is invalid or required fields are absent
    """
    if not path.exists():
        raise FileNotFoundError(f"Golden dataset not found at: {path}\n"
                                "Run from the project root or pass --dataset <path>"
                               )

    with open(path, "r") as f:
        raw = json.load(f)

    test_cases = raw.get("test_cases")

    if (not isinstance(test_cases, list) or (len(test_cases) == 0)):
        raise ValueError("golden_dataset.json must have a non-empty 'test_cases' list")

    # Validate required fields on every case
    required = {"id", "input", "expected_database", "expected_sql_keywords", "context"}

    for tc in test_cases:
        missing = required - tc.keys()

        if missing:
            raise ValueError(f"Test case {tc.get('id', '?')} is missing fields: {missing}")

    logger.info("Golden dataset loaded",
                total = len(test_cases),
                path  = str(path),
               )

    return test_cases


# Backend connectivity check
async def _check_backend_reachable(backend_url: str) -> bool:
    """
    Lightweight liveness probe against /health before starting a full evaluation run: without this guard, a backend outage causes all 108 cases to each wait
    REQUEST_TIMEOUT seconds before returning — a multi-hour hang with no output
    """
    try:
        async with httpx.AsyncClient(timeout = 60.0) as client:
            r = await client.get(f"{backend_url}/health")
            return (r.status_code == 200)

    except Exception:
        return False


# Backend caller
async def call_agent(query: str, backend_url: str, session_id: str) -> Dict[str, Any]:
    """
    POST a single query to /api/query and return the full JSON response dict: 
    - Injects inference_time_s — wall-clock seconds for the llama3:8b inference call
    - Returns a synthetic error dict on any failure so the evaluation loop can continue
    """
    payload = {"query"      : query,
               "session_id" : session_id,
              }

    t0      = time.time()

    try:
        async with httpx.AsyncClient(timeout = REQUEST_TIMEOUT) as client:
            response                   = await client.post(f"{backend_url}/api/query",
                                                           json = payload,
                                                          )

            response.raise_for_status()
            result                     = response.json()

            result["inference_time_s"] = round(time.time() - t0, 2)
            return result

    except httpx.TimeoutException:
        return {"error"            : "timeout",
                "answer"           : "",
                "sql_executed"     : [],
                "data"             : [],
                "inference_time_s" : round(time.time() - t0, 2),
               }

    except httpx.HTTPStatusError as e:
        return {"error"            : f"http_{e.response.status_code}",
                "answer"           : "",
                "sql_executed"     : [],
                "data"             : [],
                "inference_time_s" : round(time.time() - t0, 2),
               }

    except Exception as e:
        return {"error"            : str(e),
                "answer"           : "",
                "sql_executed"     : [],
                "data"             : [],
                "inference_time_s" : round(time.time() - t0, 2),
               }


# Routing accuracy
def _infer_database_from_response(response: Dict[str, Any]) -> Optional[str]:
    """
    Heuristic: infer which database was actually queried from the SQL executed using table-name fingerprints because the API response 
    does not expose the supervisor routing decision directly and finally returns the domain string, or None if no SQL was executed
    """
    sql_list     = response.get("sql_executed", [])

    if not sql_list:
        return None

    sql_upper    = " ".join(sql_list).upper()

    fingerprints = {"health"  : ["PATIENT_HISTORY", "CLAIMS", "PROCEDURES", "DIAGNOSIS_CODE"],
                    "finance" : ["TRANSACTIONS", "SUBSCRIPTIONS", "PAYMENT_FAILURES", "MONTHLY_FEE"],
                    "sales"   : ["LEADS", "OPPORTUNITIES", "SALES_REPS", "OPPORTUNITY_VALUE"],
                    "iot"     : ["DAILY_STEPS", "HEART_RATE_AVG", "SLEEP_HOURS", "STEP_COUNT"],
                   }

    matched = set()

    for domain, markers in fingerprints.items():
        if any(m in sql_upper for m in markers):
            matched.add(domain)

    if not matched:
        return None

    # Return the single matched domain for single-domain queries: for cross-DB cases (_infer_databases_from_response is used instead)
    return next(iter(matched)) if (len(matched) == 1) else "cross_db"


def _infer_databases_from_response(response: Dict[str, Any]) -> set:
    """
    Extended fingerprint — returns the full set of matched domains: used for cross-DB routing
    accuracy evaluation where two domains may both appear in the generated SQL
    """
    sql_list     = response.get("sql_executed", [])

    if not sql_list:
        return set()

    sql_upper    = " ".join(sql_list).upper()

    fingerprints = {"health"  : ["PATIENT_HISTORY", "CLAIMS", "PROCEDURES", "DIAGNOSIS_CODE"],
                    "finance" : ["TRANSACTIONS", "SUBSCRIPTIONS", "PAYMENT_FAILURES", "MONTHLY_FEE"],
                    "sales"   : ["LEADS", "OPPORTUNITIES", "SALES_REPS", "OPPORTUNITY_VALUE"],
                    "iot"     : ["DAILY_STEPS", "HEART_RATE_AVG", "SLEEP_HOURS", "STEP_COUNT"],
                   }

    return {domain for domain, markers in fingerprints.items() if any(m in sql_upper for m in markers)}


def evaluate_routing(test_cases: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compare expected_database against the database inferred from SQL output

    Returns:
        overall_routing_accuracy  : float
        correct                   : int
        total                     : int
        cross_db_routing_accuracy : float
        routing_failures          : List[dict]   — only failed/errored cases
        all_cases                 : List[dict]   — every case with pass/fail detail
    """
    correct       = 0
    failures      = list()
    all_cases     = list()
    cross_total   = 0
    cross_correct = 0

    for tc, resp in zip(test_cases, responses):
        expected    = tc["expected_database"]
        is_cross    = (tc.get("query_type") == "cross_db_routing")
        case_record = {"id"         : tc["id"],
                       "query_type" : tc.get("query_type", ""),
                       "difficulty" : tc.get("difficulty", ""),
                       "input"      : tc["input"],
                       "expected"   : expected,
                       "actual"     : None,
                       "correct"    : False,
                       "is_cross"   : is_cross,
                       "error"      : None,
                      }

        if is_cross:
            cross_total += 1

        if resp.get("error"):
            case_record["actual"] = "error"
            case_record["error"]  = resp["error"]

            all_cases.append(case_record)
            failures.append({k: case_record[k] for k in ("id", "input", "expected", "actual", "error", "is_cross")})
            continue

        if is_cross:
            matched_domains = _infer_databases_from_response(resp)

            if not matched_domains:
                case_record["actual"] = "no_sql_executed"

                all_cases.append(case_record)
                failures.append({k: case_record[k] for k in ("id", "input", "expected", "actual", "is_cross")})
                continue

            case_record["actual"] = sorted(matched_domains)

            if expected in matched_domains:
                correct               += 1
                cross_correct         += 1
                case_record["correct"] = True

            else:
                case_record["sql"] = resp.get("sql_executed", [])
                failures.append({k: case_record[k] for k in ("id", "input", "expected", "actual", "sql", "is_cross")})

        else:
            inferred = _infer_database_from_response(resp)

            if inferred is None:
                case_record["actual"] = "no_sql_executed"
                all_cases.append(case_record)
                failures.append({k: case_record[k] for k in ("id", "input", "expected", "actual", "is_cross")})
                continue

            case_record["actual"] = inferred

            if (inferred == expected):
                correct               += 1
                case_record["correct"] = True

            else:
                case_record["sql"] = resp.get("sql_executed", [])
                failures.append({k: case_record[k] for k in ("id", "input", "expected", "actual", "sql", "is_cross")})

        all_cases.append(case_record)

    total     = len(test_cases)
    accuracy  = (correct / total) if (total > 0) else 0.0
    cross_acc = (cross_correct / cross_total) if (cross_total > 0) else 0.0

    return {"overall_routing_accuracy"  : round(accuracy,  4),
            "correct"                   : correct,
            "total"                     : total,
            "cross_db_routing_accuracy" : round(cross_acc, 4),
            "cross_correct"             : cross_correct,
            "cross_total"               : cross_total,
            "routing_failures"          : failures,
            "all_cases"                 : all_cases,
           }


# SQL keyword coverage
def evaluate_sql_coverage(test_cases: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Check that expected_sql_keywords are present (case-insensitive) in the generated SQL
    and returns per-case coverage ratios, overall average, and low-coverage case list
    """
    results = list()

    for tc, resp in zip(test_cases, responses):
        base = {"id"         : tc["id"],
                "query_type" : tc.get("query_type", ""),
                "difficulty" : tc.get("difficulty", ""),
                "input"      : tc["input"],
               }

        if (resp.get("error") or not resp.get("sql_executed")):
            results.append({**base,
                            "coverage" : 0.0,
                            "found"    : [],
                            "missing"  : tc["expected_sql_keywords"],
                            "sql"      : resp.get("sql_executed", []),
                            "error"    : resp.get("error"),
                          })
            continue

        sql_upper = " ".join(resp["sql_executed"]).upper()
        keywords  = [kw.upper() for kw in tc.get("expected_sql_keywords", [])]

        if not keywords:
            results.append({**base,
                            "coverage" : 1.0,
                            "found"    : [],
                            "missing"  : [],
                            "sql"      : resp.get("sql_executed", []),
                          })
            continue

        found    = [kw for kw in keywords if kw in sql_upper]
        missing  = [kw for kw in keywords if kw not in sql_upper]
        coverage = len(found) / len(keywords)

        results.append({**base,
                        "coverage" : round(coverage, 4),
                        "found"    : found,
                        "missing"  : missing,
                        "sql"      : resp.get("sql_executed", []),
                      })

    coverages    = [r["coverage"] for r in results]
    avg_coverage = (sum(coverages) / len(coverages)) if coverages else 0.0
    low_coverage = [r for r in results if (r["coverage"] < 0.5)]

    return {"avg_sql_coverage"   : round(avg_coverage, 4),
            "cases"              : results,
            "low_coverage_cases" : low_coverage,
           }


# DeepEval answer quality
def build_deepeval_test_cases(golden: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> List[Tuple]:
    """
    Build evaluation tuples from agent responses:
    - Each tuple: (golden_case, LLMTestCase, sql_executed, inference_time_s)
    - Skips cases where the agent returned an error or produced no answer
    """
    pairs = list()

    for tc, resp in zip(golden, responses):
        if resp.get("error") or not resp.get("answer"):
            continue

        # Truncate long tabular answers — GROUP BY queries returning one row per code can be thousands of chars; Mistral:7b 
        # runs out of num_predict budget trying to write a verdict over the full text, producing "invalid JSON"
        answer_for_eval = resp["answer"][:1500]

        if (len(resp["answer"]) > 1500):
            answer_for_eval += "... [truncated for evaluation]"

        lltc = LLMTestCase(input             = tc["input"],
                           actual_output     = answer_for_eval,
                           expected_output   = ", ".join(tc.get("expected_answer_contains", [])),
                           context           = [tc.get("context", "")],
                           retrieval_context = [" ".join(resp.get("sql_executed", []))],
                          )

        pairs.append((tc, lltc, resp.get("sql_executed", []), resp.get("inference_time_s", None)))

    return pairs


def run_deepeval(pairs: List[Tuple[Dict[str, Any], LLMTestCase]]) -> Dict[str, Any]:
    """
    Run AnswerRelevancy and Faithfulness metrics via DeepEval and returns aggregated scores, per-case scores for all evaluated cases, and a list of failed cases

    - Uses metric.measure(tc) directly instead of evaluate([tc], metrics)
    - This is more reliable across deepeval versions because it gives direct access to metric.score without requiring _extract_metric_score() heuristics
    - Evaluates one case at a time so a single bad case (e.g. a very long answer that causes the evaluator to output malformed JSON) cannot abort the
      entire run and wipe out all previously scored cases
    """
    if not pairs:
        return {"error"            : "No valid test cases to evaluate",
                "avg_relevancy"    : 0.0,
                "avg_faithfulness" : 0.0,
               }

    model_name      = settings.deepeval_evaluator_model
    evaluator_model = OllamaEvaluator(model = model_name,
                                      host  = settings.ollama_host,
                                     )

    logger.info("Starting DeepEval quality scoring",
                evaluator_model = model_name,
                total_cases     = len(pairs),
               )

    relevancy_scores    = list()
    faithfulness_scores = list()
    all_cases           = list()
    failed_cases        = list()
    skipped             = 0

    try:
        for i, (golden_tc, lltc, sql_executed, inference_time_s) in enumerate(pairs):
            rel_score       = None
            faith_score     = None
            last_error      = None
            deepeval_time_s = None

            # Retry once before skipping — a single malformed JSON response from the evaluator model is often transient
            # (model still loading, or think-tag on a long answer). A second attempt usually succeeds
            for attempt in range(5):
                try:
                    # async_mode=False forces DeepEval to use sync generate() which correctly applies _wrap_prompt_for_json
                    # and suppresses think-tags and enforces JSON-only output from the evaluator
                    rel_metric      = AnswerRelevancyMetric(threshold  = RELEVANCY_THRESHOLD,
                                                            model      = evaluator_model,
                                                            async_mode = False,
                                                           )

                    faith_metric    = FaithfulnessMetric(threshold  = FAITHFULNESS_THRESHOLD,
                                                         model      = evaluator_model,
                                                         async_mode = False,
                                                        )

                    # measure() directly: avoids evaluate() wrapper and gives direct .score access
                    eval_t0         = time.time()

                    rel_metric.measure(lltc)
                    faith_metric.measure(lltc)

                    deepeval_time_s = round(time.time() - eval_t0, 2)

                    rel_score       = float(rel_metric.score) if (rel_metric.score is not None) else 0.0
                    faith_score     = float(faith_metric.score) if (faith_metric.score is not None) else 0.0
                    break

                except Exception as e:
                    last_error = e
                    logger.warning("DeepEval case scoring failed, retrying",
                                   case    = golden_tc["id"],
                                   attempt = attempt + 1,
                                   error   = str(e),
                                  )

                    if (attempt == 0):
                        time.sleep(10)

                    else:
                        logger.warning("DeepEval skipping case after 5 failed attempts",
                                       case  = golden_tc["id"],
                                       error = str(last_error),
                                      )
                        skipped += 1

            if (rel_score is None) or (faith_score is None):
                # All attempts failed — record as skipped
                all_cases.append({"id"                       : golden_tc["id"],
                                  "query_type"               : golden_tc.get("query_type", ""),
                                  "difficulty"               : golden_tc.get("difficulty", ""),
                                  "tags"                     : golden_tc.get("tags", []),
                                  "input"                    : golden_tc["input"],
                                  "expected_database"        : golden_tc.get("expected_database", ""),
                                  "expected_sql_keywords"    : golden_tc.get("expected_sql_keywords", []),
                                  "expected_answer_contains" : golden_tc.get("expected_answer_contains", []),
                                  "context"                  : golden_tc.get("context", ""),
                                  "answer"                   : lltc.actual_output,
                                  "sql_executed"             : sql_executed,
                                  "inference_time_s"         : inference_time_s,
                                  "deepeval_time_s"          : None,
                                  "relevancy_score"          : None,
                                  "faithfulness_score"       : None,
                                  "relevancy_pass"           : False,
                                  "faithfulness_pass"        : False,
                                  "skipped"                  : True,
                                  "skip_reason"              : str(last_error),
                                }) 
                continue

            relevancy_scores.append(rel_score)
            faithfulness_scores.append(faith_score)

            rel_pass    = (rel_score >= RELEVANCY_THRESHOLD)
            faith_pass  = (faith_score >= FAITHFULNESS_THRESHOLD)

            case_record = {"id"                       : golden_tc["id"],
                           "query_type"               : golden_tc.get("query_type", ""),
                           "difficulty"               : golden_tc.get("difficulty", ""),
                           "tags"                     : golden_tc.get("tags", []),
                           "input"                    : golden_tc["input"],
                           "expected_database"        : golden_tc.get("expected_database", ""),
                           "expected_sql_keywords"    : golden_tc.get("expected_sql_keywords", []),
                           "expected_answer_contains" : golden_tc.get("expected_answer_contains", []),
                           "context"                  : golden_tc.get("context", ""),
                           "answer"                   : lltc.actual_output,
                           "sql_executed"             : sql_executed,
                           "inference_time_s"         : inference_time_s,
                           "deepeval_time_s"          : deepeval_time_s,
                           "relevancy_score"          : round(rel_score, 4),
                           "faithfulness_score"       : round(faith_score, 4),
                           "relevancy_pass"           : rel_pass,
                           "faithfulness_pass"        : faith_pass,
                           "skipped"                  : False,
                          }

            all_cases.append(case_record)

            if (not rel_pass) or (not faith_pass):
                failed_cases.append(case_record)

            logger.info("DeepEval case scored",
                        case             = golden_tc["id"],
                        progress         = f"{i + 1}/{len(pairs)}",
                        relevancy        = round(rel_score, 4),
                        faithfulness     = round(faith_score, 4),
                        deepeval_time_s  = deepeval_time_s,
                        inference_time_s = inference_time_s,
                       )

    finally:
        # Clean up async client regardless of whether evaluation completed or raised
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(evaluator_model.close())

            else:
                loop.run_until_complete(evaluator_model.close())
        
        except Exception as e:
            logger.warning("Failed to close evaluator client",
                           error = str(e),
                          )

    n = len(relevancy_scores)

    if (skipped > 0):
        logger.warning("DeepEval skipped cases due to evaluation errors",
                       skipped   = skipped,
                       evaluated = n,
                      )

    return {"avg_relevancy"    : round(sum(relevancy_scores) / n, 4) if n else 0.0,
            "avg_faithfulness" : round(sum(faithfulness_scores) / n, 4) if n else 0.0,
            "evaluated_cases"  : n,
            "skipped_cases"    : skipped,
            "all_cases"        : all_cases,
            "failed_cases"     : failed_cases,
           }


# Per-domain summary
def _compute_per_domain_summary(golden: List[Dict[str, Any]], routing_report: Dict[str, Any], sql_report: Dict[str, Any], quality_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute per-domain breakdown of routing accuracy, SQL coverage, and answer quality and returned as a dict keyed by
    domain name for inclusion in the top-level results
    """
    routing_by_id = {c["id"]: c for c in routing_report.get("all_cases", [])}
    sql_by_id     = {c["id"]: c for c in sql_report.get("cases", [])}
    quality_by_id = {c["id"]: c for c in quality_report.get("all_cases", [])}

    per_domain    = dict()

    for domain in sorted(DOMAINS):
        domain_cases       = [tc for tc in golden if (tc.get("query_type") == domain)]

        if not domain_cases:
            continue

        total              = len(domain_cases)

        # Routing
        routing_correct    = sum(1 for tc in domain_cases if routing_by_id.get(tc["id"], {}).get("correct", False))

        # SQL coverage
        sql_coverages      = [sql_by_id[tc["id"]]["coverage"] for tc in domain_cases if tc["id"] in sql_by_id]
        avg_sql            = (sum(sql_coverages) / len(sql_coverages)) if sql_coverages else 0.0

        # Answer quality — only cases that were evaluated (not skipped)
        qual_cases         = [quality_by_id[tc["id"]] for tc in domain_cases if tc["id"] in quality_by_id and not quality_by_id[tc["id"]].get("skipped", True)]
        avg_rel            = (sum(c["relevancy_score"]    for c in qual_cases) / len(qual_cases)) if qual_cases else None
        avg_faith          = (sum(c["faithfulness_score"] for c in qual_cases) / len(qual_cases)) if qual_cases else None

        per_domain[domain] = {"total"                   : total,
                              "routing_correct"         : routing_correct,
                              "routing_accuracy"        : round(routing_correct / total, 4),
                              "avg_sql_coverage"        : round(avg_sql,4),
                              "quality_evaluated_cases" : len(qual_cases),
                              "avg_relevancy"           : round(avg_rel, 4) if (avg_rel is not None) else None,
                              "avg_faithfulness"        : round(avg_faith, 4) if (avg_faith is not None) else None,
                             }

    return per_domain


# Main evaluation runner
async def run_evaluation(golden: List[Dict[str, Any]], backend_url: str, dry_run: bool = False, no_deepeval: bool = False) -> Dict[str, Any]:
    """
    Run the full evaluation pipeline:
      1. Connectivity check (fail fast if backend is unreachable)
      2. Call agent for every test case (unless dry_run)
      3. Routing accuracy report — all_cases + routing_failures
      4. SQL keyword coverage report — all_cases + low_coverage_cases
      5. DeepEval answer quality report — all_cases + failed_cases (skipped if no_deepeval=True)
      6. Per-domain summary breakdown

    Returns a structured results dict suitable for JSON serialisation
    """
    if dry_run:
        logger.info("DRY RUN — printing test cases without calling backend")

        for i, tc in enumerate(golden, 1):
            print(f"[{i:03d}] [{tc['query_type'].upper():20}] [{tc['difficulty'].upper():6}] {tc['input']}")

        return {"dry_run"     : True,
                "total_cases" : len(golden),
               }

    logger.info("Checking backend connectivity", 
                backend = backend_url,
               )

    if not await _check_backend_reachable(backend_url):
        logger.error("Backend not reachable — aborting evaluation",
                     backend = backend_url,
                    )

        return {"error"           : f"Backend not reachable at {backend_url}. Start the FastAPI server and retry.",
                "total_cases"     : len(golden),
                "elapsed_seconds" : 0,
               }

    logger.info("Starting evaluation",
                total       = len(golden),
                backend     = backend_url,
                no_deepeval = no_deepeval,
               )

    start_time = time.time()
    responses  = list()

    for i, tc in enumerate(golden, 1):
        logger.info("Querying agent",
                    case     = tc["id"],
                    progress = f"{i}/{len(golden)}",
                   )

        session_id = f"eval_{tc['id']}"

        resp = await call_agent(tc["input"], backend_url, session_id)
        responses.append(resp)

        if resp.get("error"):
            logger.warning("Agent returned error",
                           case  = tc["id"],
                           error = resp["error"],
                          )

        if (i < len(golden)):
            await asyncio.sleep(BATCH_DELAY)

    elapsed = round(time.time() - start_time, 1)
    logger.info("Agent calls complete",
                elapsed_s = elapsed,
               )

    # Build all report layers
    routing_report = evaluate_routing(golden, responses)
    sql_report     = evaluate_sql_coverage(golden, responses)

    if no_deepeval:
        logger.info("Skipping DeepEval quality scoring (--no-deepeval flag set)")
        quality_report = {"avg_relevancy"    : None,
                          "avg_faithfulness" : None,
                          "evaluated_cases"  : 0,
                          "skipped_cases"    : len(golden),
                          "all_cases"        : [],
                          "failed_cases"     : [],
                          "note"             : "DeepEval skipped — run without --no-deepeval to include answer quality scores",
                         }
    else:
        deepeval_pairs = build_deepeval_test_cases(golden, responses)
        quality_report = run_deepeval(deepeval_pairs)

    per_domain = _compute_per_domain_summary(golden, routing_report, sql_report, quality_report)

    results    = {"summary"    : {"total_cases"               : len(golden),
                                  "elapsed_seconds"           : elapsed,
                                  "routing_accuracy"          : routing_report["overall_routing_accuracy"],
                                  "cross_db_routing_accuracy" : routing_report["cross_db_routing_accuracy"],
                                  "avg_sql_keyword_coverage"  : sql_report["avg_sql_coverage"],
                                  "avg_answer_relevancy"      : quality_report.get("avg_relevancy"),
                                  "avg_faithfulness"          : quality_report.get("avg_faithfulness"),
                                  "deepeval_evaluated_cases"  : quality_report.get("evaluated_cases", 0),
                                  "deepeval_skipped_cases"    : quality_report.get("skipped_cases",  0),
                                  "inference_model"           : settings.ollama_model,
                                  "evaluator_model"           : settings.deepeval_evaluator_model if not no_deepeval else "skipped",
                                 },
                  "per_domain" : per_domain,
                  "routing"    : routing_report,
                  "sql"        : sql_report,
                  "quality"    : quality_report,
                 }

    _print_summary(results["summary"], per_domain)

    return results


def _print_summary(s: Dict[str, Any], per_domain: Optional[Dict[str, Any]] = None) -> None:
    print("\n" + "=" * 80)
    print("  LocalGenBI-Agent Evaluation Summary")
    print("=" * 80)
    print(f"  Inference model            : {s.get('inference_model', 'unknown')}")
    print(f"  Evaluator model            : {s.get('evaluator_model', 'unknown')}")
    print(f"  Total test cases           : {s['total_cases']}")
    print(f"  Elapsed                    : {s['elapsed_seconds']}s")
    print(f"  Routing accuracy           : {s['routing_accuracy']:.1%}")
    print(f"  Cross-DB routing accuracy  : {s['cross_db_routing_accuracy']:.1%}")
    print(f"  SQL keyword coverage       : {s['avg_sql_keyword_coverage']:.1%}")

    avg_rel   = s.get("avg_answer_relevancy")
    avg_faith = s.get("avg_faithfulness")

    print(f"  Answer relevancy (DeepEval): {f'{avg_rel:.1%}' if avg_rel is not None else 'skipped'}")
    print(f"  Faithfulness (DeepEval)    : {f'{avg_faith:.1%}' if avg_faith is not None else 'skipped'}")
    print(f"  DeepEval evaluated / skip  : {s.get('deepeval_evaluated_cases', 0)} / {s.get('deepeval_skipped_cases', 0)}")

    if per_domain:
        print()
        print("  Per-domain breakdown:")
        print(f"  {'Domain':<22} {'Cases':>6} {'Routing':>9} {'SQL Cov':>9} {'Relevancy':>11} {'Faithful':>10}")
        print("  " + "-" * 75)

        for domain, d in sorted(per_domain.items()):
            rel_str   = f"{d['avg_relevancy']:.1%}" if (d.get("avg_relevancy") is not None) else "  n/a"
            faith_str = f"{d['avg_faithfulness']:.1%}" if (d.get("avg_faithfulness") is not None) else "  n/a"
            print(f"  {domain:<22} {d['total']:>6} {d['routing_accuracy']:>8.1%} "
                  f"{d['avg_sql_coverage']:>8.1%} {rel_str:>11} {faith_str:>10}")

    print("=" * 80 + "\n")


# CLI
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description = "LocalGenBI-Agent evaluation harness")

    p.add_argument("--backend", default = DEFAULT_BACKEND_URL, help = "FastAPI backend URL")
    p.add_argument("--dataset", default = str(DEFAULT_DATASET_PATH), help = "Path to golden_dataset.json")
    p.add_argument("--domain", choices = sorted(DOMAINS | {"all"}), default = "all", help = "Filter to one domain")
    p.add_argument("--dry-run", action  = "store_true", help = "Print test cases without calling backend")
    p.add_argument("--no-deepeval", action  = "store_true", help = "Skip DeepEval quality scoring (routing + SQL only)")
    p.add_argument("--output", default = None, help = "Write JSON results to this file")
    p.add_argument("--limit", type = int, default = None, help = "Cap number of test cases (for smoke tests)")

    return p.parse_args()


async def main() -> None:
    args   = parse_args()
    golden = load_golden_dataset(Path(args.dataset))

    if (args.domain != "all"):
        golden = [tc for tc in golden if (tc["query_type"] == args.domain)]
        logger.info("Domain filter applied",
                    domain    = args.domain,
                    remaining = len(golden),
                   )

    if args.limit:
        golden = golden[:args.limit]
        logger.info("Case limit applied", limit = args.limit)

    results = await run_evaluation(golden      = golden,
                                   backend_url = args.backend,
                                   dry_run     = args.dry_run,
                                   no_deepeval = args.no_deepeval,
                                  )

    if args.output and not args.dry_run:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents  = True, 
                              exist_ok = True,
                             )

        with open(out_path, "w") as f:
            json.dump(obj     = results,
                      fp      = f,
                      indent  = 4,
                      default = str,
                     )

        logger.info("Results written", path = str(out_path))


# Singleton for import usage (CI / notebook)
class AgentEvaluator:
    """
    Convenience wrapper for programmatic use

    Example:
        from evaluation.agent_evaluator import AgentEvaluator
        evaluator = AgentEvaluator()
        results   = asyncio.run(evaluator.run(backend_url="http://localhost:8001"))
    """
    def __init__(self, dataset_path: Optional[str] = None):
        self.dataset_path = Path(dataset_path) if dataset_path else DEFAULT_DATASET_PATH


    def load(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        cases = load_golden_dataset(self.dataset_path)

        if domain:
            cases = [tc for tc in cases if (tc["query_type"] == domain)]

        return cases


    async def run(self, backend_url: str = DEFAULT_BACKEND_URL, domain: Optional[str] = None, limit: Optional[int] = None, no_deepeval: bool = False) -> Dict[str, Any]:
        golden = self.load(domain)

        if limit:
            golden = golden[:limit]

        return await run_evaluation(golden, backend_url, no_deepeval = no_deepeval)


evaluator = AgentEvaluator()


if __name__ == "__main__":
    asyncio.run(main())