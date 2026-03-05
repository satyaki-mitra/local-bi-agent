# DEPENDENCIES
import json
import time
import httpx
import asyncio
import argparse
import structlog
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple
from pathlib import Path
from typing import Optional
from deepeval import evaluate
from deepeval.test_case import LLMTestCase
from deepeval.metrics import FaithfulnessMetric
from deepeval.metrics import AnswerRelevancyMetric


# Setup Logging
logger = structlog.get_logger()


# Constants
DEFAULT_DATASET_PATH   = Path(__file__).parent / "golden_dataset.json"
DEFAULT_BACKEND_URL    = "http://localhost:8001"
REQUEST_TIMEOUT        = 300.0                   # seconds — LLM inference is slow on CPU hardware
BATCH_DELAY            = 1.0                     # seconds between requests to avoid thundering herd on Ollama

RELEVANCY_THRESHOLD    = 0.7
FAITHFULNESS_THRESHOLD = 0.7

DOMAINS                = {"health", "finance", "sales", "iot", "cross_db_routing"}


# Dataset loader
def load_golden_dataset(path: Path = DEFAULT_DATASET_PATH) -> List[Dict[str, Any]]:
    """
    Load and validate the golden dataset JSON and 
    
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
    Lightweight liveness probe against /health before starting a full evaluation run
    
    Without this guard, a backend outage causes all 108 cases to each wait
    REQUEST_TIMEOUT (300 s) before returning — a ~9-hour hang with no output
    """
    try:
        async with httpx.AsyncClient(timeout = 10.0) as client:
            r = await client.get(f"{backend_url}/health")
            return (r.status_code == 200)

    except Exception:
        return False


# Backend caller
async def call_agent(query: str, backend_url: str, session_id: str) -> Dict[str, Any]:
    """
    POST a single query to /api/query and return the full JSON response dict and
    returns a synthetic error dict on any failure so the evaluation loop can continue
    """
    payload = {"query"      : query,
               "session_id" : session_id,
              }

    try:
        async with httpx.AsyncClient(timeout = REQUEST_TIMEOUT) as client:
            response = await client.post(f"{backend_url}/api/query",
                                         json = payload,
                                        )
            response.raise_for_status()
            return response.json()

    except httpx.TimeoutException:
        return {"error"        : "timeout",
                "answer"       : "",
                "sql_executed" : [],
                "data"         : [],
               }

    except httpx.HTTPStatusError as e:
        return {"error"        : f"http_{e.response.status_code}",
                "answer"       : "",
                "sql_executed" : [],
                "data"         : [],
               }

    except Exception as e:
        return {"error"        : str(e),
                "answer"       : "",
                "sql_executed" : [],
                "data"         : [],
               }


# Routing accuracy
def _infer_database_from_response(response: Dict[str, Any]) -> Optional[str]:
    """
    Heuristic: infer which database was actually queried from the SQL executed and uses table-name fingerprints
    because the API response does not expose the supervisor routing decision directly

    Returns the domain string, or None if no SQL was executed
    """
    sql_list = response.get("sql_executed", [])

    if not sql_list:
        return None

    sql_upper    = " ".join(sql_list).upper()

    fingerprints = {"health"  : ["PATIENT_HISTORY", "CLAIMS", "PROCEDURES", "DIAGNOSIS_CODE"],
                    "finance" : ["TRANSACTIONS", "SUBSCRIPTIONS", "PAYMENT_FAILURES", "MONTHLY_FEE"],
                    "sales"   : ["LEADS", "OPPORTUNITIES", "SALES_REPS", "OPPORTUNITY_VALUE"],
                    "iot"     : ["DAILY_STEPS", "HEART_RATE_AVG", "SLEEP_HOURS", "STEP_COUNT"],
                   }

    matched      = set()

    for domain, markers in fingerprints.items():
        if any(m in sql_upper for m in markers):
            matched.add(domain)

    if not matched:
        return None

    # Return the single matched domain for single-domain queries: for cross-DB cases (_infer_databases_from_response is used instead)
    return next(iter(matched)) if (len(matched) == 1) else "cross_db"


def _infer_databases_from_response(response: Dict[str, Any]) -> set:
    """
    Extended fingerprint — returns the full set of matched domains: used for cross-DB routing accuracy evaluation where two domains
    may both appear in the generated SQL
    """
    sql_list = response.get("sql_executed", [])

    if not sql_list:
        return set()

    sql_upper    = " ".join(sql_list).upper()

    fingerprints = {"health"  : ["PATIENT_HISTORY", "CLAIMS", "PROCEDURES", "DIAGNOSIS_CODE"],
                    "finance" : ["TRANSACTIONS", "SUBSCRIPTIONS", "PAYMENT_FAILURES", "MONTHLY_FEE"],
                    "sales"   : ["LEADS", "OPPORTUNITIES", "SALES_REPS", "OPPORTUNITY_VALUE"],
                    "iot"     : ["DAILY_STEPS", "HEART_RATE_AVG", "SLEEP_HOURS", "STEP_COUNT"],
                   }

    return {domain for domain, markers in fingerprints.items()
            if any(m in sql_upper for m in markers)}


def evaluate_routing(test_cases: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compare expected_database against the database inferred from SQL output

    Returns:
        overall_routing_accuracy  : float
        correct                   : int
        total                     : int
        cross_db_routing_accuracy : float
        routing_failures          : List[dict]
    """
    correct       = 0
    failures      = list()
    cross_total   = 0
    cross_correct = 0

    for tc, resp in zip(test_cases, responses):
        expected  = tc["expected_database"]
        is_cross  = (tc.get("query_type") == "cross_db_routing")

        if is_cross:
            cross_total += 1

        if resp.get("error"):
            failures.append({"id"       : tc["id"],
                             "input"    : tc["input"],
                             "expected" : expected,
                             "actual"   : "error",
                             "error"    : resp["error"],
                             "is_cross" : is_cross,
                            })
            continue

        if is_cross:
            matched_domains = _infer_databases_from_response(resp)

            if not matched_domains:
                failures.append({"id"       : tc["id"],
                                 "input"    : tc["input"],
                                 "expected" : expected,
                                 "actual"   : "no_sql_executed",
                                 "is_cross" : True,
                                })
                continue

            if expected in matched_domains:
                correct += 1
                cross_correct += 1

            else:
                failures.append({"id"       : tc["id"],
                                 "input"    : tc["input"],
                                 "expected" : expected,
                                 "actual"   : sorted(matched_domains),
                                 "sql"      : resp.get("sql_executed", []),
                                 "is_cross" : True,
                                })

        else:
            inferred = _infer_database_from_response(resp)

            if inferred is None:
                failures.append({"id"       : tc["id"],
                                 "input"    : tc["input"],
                                 "expected" : expected,
                                 "actual"   : "no_sql_executed",
                                 "is_cross" : False,
                                })
                continue

            if (inferred == expected):
                correct += 1

            else:
                failures.append({"id"       : tc["id"],
                                 "input"    : tc["input"],
                                 "expected" : expected,
                                 "actual"   : inferred,
                                 "sql"      : resp.get("sql_executed", []),
                                 "is_cross" : False,
                                })

    total     = len(test_cases)
    accuracy  = (correct / total)           if (total > 0)       else 0.0
    cross_acc = (cross_correct / cross_total) if (cross_total > 0) else 0.0

    return {"overall_routing_accuracy"  : round(accuracy,  4),
            "correct"                   : correct,
            "total"                     : total,
            "cross_db_routing_accuracy" : round(cross_acc, 4),
            "cross_correct"             : cross_correct,
            "cross_total"               : cross_total,
            "routing_failures"          : failures,
           }


# SQL keyword coverage
def evaluate_sql_coverage(test_cases: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Check that expected_sql_keywords are present (case-insensitive) in the generated SQL and returns per-case coverage ratios and an overall average
    """
    results = list()

    for tc, resp in zip(test_cases, responses):
        if resp.get("error") or not resp.get("sql_executed"):
            results.append({"id"       : tc["id"],
                            "coverage" : 0.0,
                            "missing"  : tc["expected_sql_keywords"],
                           })
            continue

        sql_upper = " ".join(resp["sql_executed"]).upper()
        keywords  = [kw.upper() for kw in tc.get("expected_sql_keywords", [])]

        if not keywords:
            results.append({"id"       : tc["id"],
                            "coverage" : 1.0,
                            "missing"  : [],
                           })
            continue

        found    = [kw for kw in keywords if kw in sql_upper]
        missing  = [kw for kw in keywords if kw not in sql_upper]
        coverage = len(found) / len(keywords)

        results.append({"id"       : tc["id"],
                        "coverage" : round(coverage, 4),
                        "found"    : found,
                        "missing"  : missing,
                       })

    coverages    = [r["coverage"] for r in results]
    avg_coverage = (sum(coverages) / len(coverages)) if coverages else 0.0
    low_coverage = [r for r in results if r["coverage"] < 0.5]

    return {"avg_sql_coverage"   : round(avg_coverage, 4),
            "cases"              : results,
            "low_coverage_cases" : low_coverage,
           }


# DeepEval answer quality
def build_deepeval_test_cases(golden: List[Dict[str, Any]], responses: List[Dict[str, Any]]) -> List[LLMTestCase]:
    """
    Build DeepEval LLMTestCase objects from agent responses: skips cases where the agent returned an error or produced no answer
    """
    test_cases = list()

    for tc, resp in zip(golden, responses):
        if resp.get("error") or not resp.get("answer"):
            continue

        test_cases.append(LLMTestCase(
            input             = tc["input"],
            actual_output     = resp["answer"],
            expected_output   = ", ".join(tc.get("expected_answer_contains", [])),
            context           = [tc.get("context", "")],
            retrieval_context = [" ".join(resp.get("sql_executed", []))],
        ))

    return test_cases


def _extract_metric_score(test_result: Any, metric_name: str) -> float:
    """
    Safely extract a named metric score from a DeepEval TestResult: it iterates metrics_data and matches 
    by metric class name, which is stable across recent deepeval versions
    """
    try:
        # deepeval >= 0.21: scores in metrics_data list
        if hasattr(test_result, "metrics_data"):
            for md in (test_result.metrics_data or []):
                if metric_name.lower() in md.name.lower():
                    return float(md.score) if md.score is not None else 0.0

        # Fallback: flat attribute (older deepeval versions)
        attr_score = getattr(test_result, f"{metric_name}_score", None)
        if attr_score is not None:
            return float(attr_score)

    except (TypeError, ValueError, AttributeError):
        pass

    return 0.0


def run_deepeval(test_cases: List[LLMTestCase]) -> Dict[str, Any]:
    """
    Run AnswerRelevancy and Faithfulness metrics via DeepEval and returns aggregated scores and per-case failures
    """
    if not test_cases:
        return {"error"        : "No valid test cases to evaluate",
                "relevancy"    : 0.0,
                "faithfulness" : 0.0,
               }

    metrics             = [AnswerRelevancyMetric(threshold = RELEVANCY_THRESHOLD),
                           FaithfulnessMetric(threshold    = FAITHFULNESS_THRESHOLD),
                          ]

    results             = evaluate(test_cases, metrics)

    relevancy_scores    = list()
    faithfulness_scores = list()
    failures            = list()

    # Sanity-check the first result so we catch API changes immediately
    if results:
        first_rel = _extract_metric_score(results[0], "answer_relevancy")
        if first_rel == 0.0:
            logger.warning("DeepEval relevancy score is 0.0 on first case — verify deepeval version and metric attribute names")

    for tc, result in zip(test_cases, results):
        rel   = _extract_metric_score(result, "answer_relevancy")
        faith = _extract_metric_score(result, "faithfulness")

        relevancy_scores.append(rel)
        faithfulness_scores.append(faith)

        if ((rel < RELEVANCY_THRESHOLD) or (faith < FAITHFULNESS_THRESHOLD)):
            failures.append({"input"              : tc.input,
                             "answer"             : tc.actual_output[:200],
                             "relevancy_score"    : round(rel,   4),
                             "faithfulness_score" : round(faith, 4),
                            })

    n = len(relevancy_scores)

    return {"avg_relevancy"    : round(sum(relevancy_scores)    / n, 4) if n else 0.0,
            "avg_faithfulness" : round(sum(faithfulness_scores) / n, 4) if n else 0.0,
            "evaluated_cases"  : n,
            "failed_cases"     : failures,
           }


# Main evaluation runner
async def run_evaluation(golden: List[Dict[str, Any]], backend_url: str, dry_run: bool = False) -> Dict[str, Any]:
    """
    Run the full evaluation pipeline:
      1. Connectivity check (fail fast if backend is unreachable)
      2. Call agent for every test case (unless dry_run)
      3. Routing accuracy report
      4. SQL keyword coverage report
      5. DeepEval answer quality report

    Returns a structured results dict suitable for JSON serialisation.
    """
    if dry_run:
        logger.info("DRY RUN — printing test cases without calling backend")

        for i, tc in enumerate(golden, 1):
            print(f"[{i:03d}] [{tc['query_type'].upper():20}] [{tc['difficulty'].upper():6}] {tc['input']}")

        return {"dry_run"     : True,
                "total_cases" : len(golden),
               }

    logger.info("Checking backend connectivity", backend = backend_url)

    if not await _check_backend_reachable(backend_url):
        logger.error("Backend not reachable — aborting evaluation",
                     backend = backend_url,
                    )

        return {"error"        : f"Backend not reachable at {backend_url}. Start the FastAPI server and retry.",
                "total_cases"  : len(golden),
                "elapsed_seconds" : 0,
               }

    logger.info("Starting evaluation",
                total   = len(golden),
                backend = backend_url,
               )

    start_time = time.time()
    responses  = list()

    for i, tc in enumerate(golden, 1):
        logger.info("Querying agent",
                    case     = tc["id"],
                    progress = f"{i}/{len(golden)}",
                   )

        session_id = f"eval_{tc['id']}"

        resp       = await call_agent(tc["input"], backend_url, session_id)
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

    # Build all three report layers
    routing_report = evaluate_routing(golden, responses)
    sql_report     = evaluate_sql_coverage(golden, responses)
    deepeval_cases = build_deepeval_test_cases(golden, responses)
    quality_report = run_deepeval(deepeval_cases)

    results        = {"summary" : {"total_cases"               : len(golden),
                                   "elapsed_seconds"           : elapsed,
                                   "routing_accuracy"          : routing_report["overall_routing_accuracy"],
                                   "cross_db_routing_accuracy" : routing_report["cross_db_routing_accuracy"],
                                   "avg_sql_keyword_coverage"  : sql_report["avg_sql_coverage"],
                                   "avg_answer_relevancy"      : quality_report.get("avg_relevancy",    0.0),
                                   "avg_faithfulness"          : quality_report.get("avg_faithfulness", 0.0),
                                  },
                      "routing" : routing_report,
                      "sql"     : sql_report,
                      "quality" : quality_report,
                     }

    _print_summary(results["summary"])

    return results


def _print_summary(s: Dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("  LocalGenBI-Agent Evaluation Summary")
    print("=" * 70)
    print(f"  Total test cases           : {s['total_cases']}")
    print(f"  Elapsed                    : {s['elapsed_seconds']}s")
    print(f"  Routing accuracy           : {s['routing_accuracy']:.1%}")
    print(f"  Cross-DB routing accuracy  : {s['cross_db_routing_accuracy']:.1%}")
    print(f"  SQL keyword coverage       : {s['avg_sql_keyword_coverage']:.1%}")
    print(f"  Answer relevancy (DeepEval): {s['avg_answer_relevancy']:.1%}")
    print(f"  Faithfulness (DeepEval)    : {s['avg_faithfulness']:.1%}")
    print("=" * 70 + "\n")


# CLI
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description = "LocalGenBI-Agent evaluation harness")

    p.add_argument("--backend", default = DEFAULT_BACKEND_URL,       help = "FastAPI backend URL")
    p.add_argument("--dataset", default = str(DEFAULT_DATASET_PATH), help = "Path to golden_dataset.json")
    p.add_argument("--domain",  choices = sorted(DOMAINS | {"all"}), default = "all", help = "Filter to one domain")
    p.add_argument("--dry-run", action  = "store_true",              help = "Print test cases without calling backend")
    p.add_argument("--output",  default = None,                      help = "Write JSON results to this file")
    p.add_argument("--limit",   type    = int, default = None,       help = "Cap number of test cases (for smoke tests)")

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
                                  )

    if args.output and not args.dry_run:
        out_path = Path(args.output)

        with open(out_path, "w") as f:
            json.dump(obj     = results,
                      fp      = f,
                      indent  = 2,
                      default = str,
                     )

        logger.info("Results written", path = str(out_path))


# Singleton for import usage (CI / notebook)
class AgentEvaluator:
    """
    Convenience wrapper for programmatic use.

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


    async def run(self, backend_url: str = DEFAULT_BACKEND_URL, domain: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        golden = self.load(domain)

        if limit:
            golden = golden[:limit]

        return await run_evaluation(golden, backend_url)


evaluator = AgentEvaluator()


if __name__ == "__main__":
    asyncio.run(main())