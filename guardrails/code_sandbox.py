# DEPENDENCIES
import io
import ast
import time
import structlog
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple
import concurrent.futures
from typing import Optional
from config.settings import settings
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from config.schemas import CodeExecutionResult


# Setup Logging
logger = structlog.get_logger()


# Output variable names the LLM is expected to assign to: scanned in order — first match wins
_OUTPUT_VARIABLE_NAMES  : List[str] = ["result", "output", "fig", "chart", "df", "data"]

# Dunder keys that must never be injected via context (security hardening)
_FORBIDDEN_CONTEXT_KEYS : frozenset = frozenset(["__builtins__", "__import__", "__loader__", "__spec__", "__name__",     "__doc__",    "__package__"])

_DANGEROUS_NAMES        : frozenset = frozenset(["eval", "exec", "compile", "open", "__import__", "input", "dir", "globals", "locals", "getattr", "setattr", "delattr", "vars", "hasattr", "memoryview", "breakpoint"])
_DANGEROUS_ATTRS        : frozenset = frozenset(["__code__", "__globals__", "__builtins__", "__class__", "__bases__", "__subclasses__", "__dict__", "__init__", "__reduce__", "__reduce_ex__"])


class CodeSandbox:
    """
    Execute LLM-generated Python code in a restricted namespace

    Security model:
    - Static AST analysis (import whitelist + dangerous-name/attr scan) runs BEFORE exec()
    - exec() runs in a thread with a hard timeout via concurrent.futures
    - __builtins__ is replaced with a minimal safe subset
    - context dict is sanitised — dunder keys are stripped before namespace injection
    - ast.parse() is called ONCE per code string and the tree is reused across validators

    LIMITATION: 
    -----------
    Thread-based timeout does NOT forcibly kill the thread. Python threads cannot be preempted. 
    A thread that ignores cancellation will continue to run after the timeout returns to the caller. 
    For production isolation, run code in a subprocess or container (out of scope for this prototype).
    """
    def __init__(self):
        raw                  = settings.allowed_imports
        self.allowed_imports = {m.strip() for m in raw if m.strip()} if raw else set()
        self.timeout         = settings.code_execution_timeout

        logger.info("CodeSandbox initialised",
                    allowed_imports = sorted(self.allowed_imports),
                    timeout         = self.timeout,
                   )


    def execute(self, code: str, context: Optional[Dict[str, Any]] = None) -> CodeExecutionResult:
        """
        Validate and execute `code` in a restricted namespace and returns CodeExecutionResult — never raises
        """
        if context is None:
            context = {}

        # Sanitise context — strip any key that could overwrite security globals
        safe_context = {k: v for k, v in context.items() if k not in _FORBIDDEN_CONTEXT_KEYS}

        if (len(safe_context) != len(context)):
            stripped = set(context) - set(safe_context)
            logger.warning("Forbidden context keys stripped before exec", 
                           keys = list(stripped),
                          )

        start_time = time.time()

        # Parse ONCE, pass tree to both validators 
        try:
            tree = ast.parse(code)

        except SyntaxError as e:
            return CodeExecutionResult(success = False, 
                                       error = f"Syntax error in generated code: {e}")

        import_ok, import_err = self._validate_imports_from_tree(tree = tree)
        if not import_ok:
            return CodeExecutionResult(success = False,
                                       error   = import_err or ("Unauthorized import. Allowed: " + ", ".join(sorted(self.allowed_imports))),
                                      )

        safety_ok, safety_err = self._validate_code_safety_from_tree(tree = tree)

        if not safety_ok:
            return CodeExecutionResult(success = False,
                                       error   = safety_err or "Code contains prohibited operations",
                                      )
   
        restricted_globals = self._build_restricted_globals(context = safe_context)

        # Thread-pool timeout enforcement: cancel_futures=True only prevents unstarted futures — a running thread
        # cannot be forcibly killed in Python
        executor = concurrent.futures.ThreadPoolExecutor(max_workers = 1)
        future   = executor.submit(self._run_exec, code, restricted_globals)

        try:
            exec_result, stdout_val, stderr_val = future.result(timeout = self.timeout)
            executor.shutdown(wait = False)

            execution_time                      = int((time.time() - start_time) * 1000)

            # Scan for known output variables in priority order
            output: Any                         = None

            for name in _OUTPUT_VARIABLE_NAMES:
                if name in exec_result:
                    output = exec_result[name]
                    break

            logger.info("Code executed successfully", time_ms=execution_time)

            return CodeExecutionResult(success           = True,
                                       output            = output,
                                       stdout            = stdout_val,
                                       stderr            = stderr_val,
                                       execution_time_ms = execution_time,
                                      )

        except concurrent.futures.TimeoutError:
            executor.shutdown(wait           = False, 
                              cancel_futures = True,
                             )

            logger.error("Code execution timed out", 
                         timeout = self.timeout,
                        )

            return CodeExecutionResult(success = False,
                                       error   = f"Execution timed out after {self.timeout}s. Check for infinite loops or long-running operations.",
                                      )

        except Exception as e:
            executor.shutdown(wait = False)
            logger.error("Code execution failed", 
                         error = str(e),
                        )

            return CodeExecutionResult(success = False,
                                       error   = f"{type(e).__name__}: {str(e)}",
                                      )


    def _run_exec(self,code: str, restricted_globals: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
        stdout_capture              = io.StringIO()
        stderr_capture              = io.StringIO()
        exec_result: Dict[str, Any] = dict()

        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, restricted_globals, exec_result)

        return exec_result, stdout_capture.getvalue(), stderr_capture.getvalue()


    def _build_restricted_globals(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a minimal safe namespace for exec(): __builtins__ is replaced with an explicit whitelist — anything not listed
        here is unavailable to executed code
        """
        restricted_builtins                = {"abs"       : abs,
                                              "all"       : all,
                                              "any"       : any,
                                              "bool"      : bool,
                                              "dict"      : dict,
                                              "float"     : float,
                                              "int"       : int,
                                              "len"       : len,
                                              "list"      : list,
                                              "max"       : max,
                                              "min"       : min,
                                              "print"     : print,
                                              "range"     : range,
                                              "round"     : round,
                                              "set"       : set,
                                              "str"       : str,
                                              "sum"       : sum,
                                              "tuple"     : tuple,
                                              "type"      : type,
                                              "zip"       : zip,
                                              "enumerate" : enumerate,
                                              "sorted"    : sorted,
                                              "reversed"  : reversed,
                                             }

        restricted_globals: Dict[str, Any] = {"__builtins__" : restricted_builtins,
                                              "__name__"     : "__main__",
                                              "__doc__"      : None,
                                             }

        if ("pandas" in self.allowed_imports):
            try:
                import pandas as pd
                restricted_globals["pd"] = pd

            except ImportError:
                pass

        if ("numpy" in self.allowed_imports):
            try:
                import numpy as np
                restricted_globals["np"] = np

            except ImportError:
                pass

        if ("matplotlib" in self.allowed_imports):
            try:
                import matplotlib
                if (matplotlib.get_backend() != "Agg"):
                    matplotlib.use("Agg")

                import matplotlib.pyplot as plt
                
                restricted_globals["plt"] = plt

            except ImportError:
                pass

        if ("seaborn" in self.allowed_imports):
            try:
                import seaborn as sns
                restricted_globals["sns"] = sns

            except ImportError:
                pass

        # Context is already sanitised before this call — safe to update
        restricted_globals.update(context)

        return restricted_globals


    def _validate_imports_from_tree(self, tree: ast.AST) -> Tuple[bool, Optional[str]]:
        """
        Check that all import statements reference only allowed modules: receives a pre-parsed AST tree — no re-parsing.
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]

                    if (top not in self.allowed_imports):
                        return False, f"Unauthorized import: '{alias.name}'"

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    
                    if (top not in self.allowed_imports):
                        return False, f"Unauthorized import from: '{node.module}'"

        return True, None


    def _validate_code_safety_from_tree(self, tree: ast.AST) -> Tuple[bool, Optional[str]]:
        """
        AST-walk for dangerous built-in calls and attribute access: receives a pre-parsed AST tree — no re-parsing
        """
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                if node.func.id in _DANGEROUS_NAMES:
                    return False, f"Prohibited function call: '{node.func.id}'"

            if (isinstance(node, ast.Attribute)):
                if node.attr in _DANGEROUS_ATTRS:
                    return False, f"Prohibited attribute access: '{node.attr}'"

        return True, None


# GLOBAL INSTANCE
code_sandbox = CodeSandbox()