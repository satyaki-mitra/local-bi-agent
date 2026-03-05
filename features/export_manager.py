# DEPENDENCIES
import re
import json
import time
import asyncio
import structlog
import pandas as pd
from typing import Any
from typing import List
from typing import Dict
from pathlib import Path
from typing import Optional
from datetime import datetime
from config.settings import settings
from features.data_analyzer import data_analyzer
from config.constants import EXPORT_MAX_FILENAME_LEN
from features.result_generator import result_generator
from features.visualization_generator import viz_generator


logger = structlog.get_logger()


class ExportManager:
    """
    Unified export interface with strict path-traversal protection and automatic cleanup scheduling
    """
    def __init__(self, export_dir: str = "temp/exports"):
        self.export_dir = Path(export_dir or settings.export_dir).resolve()

        self.export_dir.mkdir(parents  = True,
                              exist_ok = True,
                             )

        logger.info("ExportManager initialised",
                    export_dir = str(self.export_dir),
                   )


    def _sanitize_filename(self, base_name: str, extension: str) -> str:
        """
        Strip all characters that could be used for path traversal or shell injection, then append a timestamp for uniqueness
        """
        safe_name = re.sub(r"[^\w\s-]", "", base_name).strip().lower()
        safe_name = re.sub(r"[-\s]+", "_", safe_name)[:EXPORT_MAX_FILENAME_LEN] or "export"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        return f"{safe_name}_{timestamp}.{extension}"


    def _safe_path(self, filename: str) -> Path:
        """
        Resolve the final path and assert it stays inside export_dir and raises ValueError on any traversal attempt
        """
        candidate = (self.export_dir / filename).resolve()

        if not candidate.is_relative_to(self.export_dir):
            raise ValueError(f"Path traversal attempt detected: {filename}")

        return candidate


    def _write(self, content: str, filename: str) -> str:
        """
        Centralised write helper — all export methods funnel through here and returns the absolute filepath string for the caller
        """
        filepath = self._safe_path(filename)

        with open(filepath, "w", encoding = "utf-8") as f:
            f.write(content)

        logger.info("Export written", filename = filename)

        return str(filepath)


    def export_simple_text(self, query: str, answer: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        content  = result_generator.generate_simple_text(query, answer, metadata)
        filename = self._sanitize_filename("query_result", "txt")
        return self._write(content, filename)


    def export_json(self, query: str, answer: str, data: Optional[List[Dict[str, Any]]] = None, sql_queries: Optional[List[str]] = None, 
                    reasoning_trace: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None) -> str:

        content  = result_generator.generate_json(query, answer, data, sql_queries, reasoning_trace, metadata)
        filename = self._sanitize_filename("query_result", "json")

        return self._write(content, filename)


    def export_csv_from_dataframe(self, df: pd.DataFrame, base_name: str = "data") -> str:
        content  = result_generator.generate_csv_from_dataframe(df)
        filename = self._sanitize_filename(base_name, "csv")

        return self._write(content, filename)


    def export_html_table(self, data: List[Dict[str, Any]], title: str = "Query Results") -> str:
        content  = result_generator.generate_html_table(data, title)
        filename = self._sanitize_filename(title, "html")

        return self._write(content, filename)


    def export_visualization(self, df: pd.DataFrame, title: str = "Visualization") -> Optional[str]:
        fig = viz_generator.auto_visualize(df, title)

        if fig is None:
            logger.warning("Visualization generation returned None", title = title)
            return None

        filename = self._sanitize_filename(title, "png")
        filepath = self._safe_path(filename)

        viz_generator.save_figure(fig, str(filepath))

        logger.info("Visualization exported", filename = filename)

        return str(filepath)


    def export_analysis_report(self, df: pd.DataFrame, output_format: str = "txt") -> str:
        """
        Generate and write a full statistical analysis report and output_format is 'txt' (default) or 'json'
        """
        analysis = data_analyzer.generate_comprehensive_report(df)

        if (output_format == "json"):
            content = json.dumps(analysis, indent = 4, default = str)

        else:
            content = data_analyzer.generate_text_report(analysis)

        filename = self._sanitize_filename("analysis", output_format)

        return self._write(content, filename)


    def export_complete_package(self, query: str, answer: str, df: pd.DataFrame, sql_queries: Optional[List[str]] = None, reasoning_trace: Optional[List[str]] = None) -> Dict[str, Optional[str]]:
        """
        Export all available formats in one call: returns a dict mapping format name to filepath (None if generation failed)
        """
        df_available                       = (df is not None and not df.empty)
        data                               = df.to_dict(orient = "records") if df_available else []

        results : Dict[str, Optional[str]] = dict()

        results["txt"]                     = self.export_simple_text(query, answer)
        results["json"]                    = self.export_json(query, answer, data, sql_queries, reasoning_trace)

        if data:
            results["csv"]  = self.export_csv_from_dataframe(df)
            results["html"] = self.export_html_table(data, title = query[:50])

        else:
            results["csv"]  = None
            results["html"] = None

        results["png"]      = self.export_visualization(df, title = query[:50]) if df_available else None
        results["analysis"] = self.export_analysis_report(df)                   if df_available else None

        logger.info("Complete package exported",
                    formats = [k for k, v in results.items() if v is not None],
                   )

        return results


    def get_export_info(self, filepath: str) -> Dict[str, Any]:
        """
        Return metadata about an existing export file
        """
        p = Path(filepath)

        if not p.exists() or not p.is_file():
            return {"error": f"File not found: {filepath}"}

        stat = p.stat()

        return {"filename"   : p.name,
                "size_bytes" : stat.st_size,
                "size_kb"    : round(stat.st_size / 1024, 2),
                "created"    : datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "extension"  : p.suffix.lstrip("."),
               }


    def cleanup_old_exports(self, days: int = 7) -> int:
        """
        Delete exports older than `days` days, skipping directories: safe to call from a background task or APScheduler job
        """
        cutoff  = time.time() - (days * 86400)
        deleted = 0

        for filepath in self.export_dir.glob("*"):
            try:
                if (filepath.is_file() and (filepath.stat().st_mtime < cutoff)):
                    filepath.unlink()
                    deleted += 1

            except Exception as e:
                logger.error("Failed to delete export file",
                             file  = str(filepath),
                             error = str(e),
                            )

        logger.info("Export cleanup complete",
                    deleted = deleted,
                    days    = days,
                   )

        return deleted


    async def schedule_cleanup(self, interval_hours: int = None, days: int = None) -> None:
        """
        Long-running async task — run via asyncio.create_task() in the FastAPI lifespan to automatically clean exports every `interval_hours`
        """
        _interval = interval_hours or settings.export_cleanup_interval_hours
        _days     = days           or settings.export_cleanup_days

        while True:
            await asyncio.sleep(_interval * 3600)

            try:
                self.cleanup_old_exports(days = _days)

            except Exception as e:
                logger.error("Scheduled cleanup failed", error = str(e))


# GLOBAL INSTANCE
export_manager = ExportManager()