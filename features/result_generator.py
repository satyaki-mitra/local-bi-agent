# DEPENDENCIES
import io
import csv
import json
import structlog
import pandas as pd
from typing import Any
from typing import List
from typing import Dict
from typing import Optional
from datetime import datetime
from config.constants import RESULT_BRANDING_NAME
from config.constants import RESULT_BRANDING_TAGLINE


logger = structlog.get_logger()

# HTML template constants (avoids re-building on every call) 
_HTML_STYLE = """
    <style>
      :root {
        --bg:      #0D1117;
        --surface: #161B22;
        --border:  #21262D;
        --accent:  #00D4AA;
        --text:    #E6EDF3;
        --muted:   #8B949E;
        --font:    'Segoe UI', system-ui, sans-serif;
        --mono:    'Consolas', monospace;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      body {
        background : var(--bg);
        color      : var(--text);
        font-family: var(--font);
        font-size  : 14px;
        padding    : 32px;
      }
      header {
        display        : flex;
        align-items    : center;
        gap            : 12px;
        margin-bottom  : 24px;
        padding-bottom : 16px;
        border-bottom  : 1px solid var(--border);
      }
      header .logo {
        font-size   : 1.2rem;
        font-weight : 700;
        color       : var(--accent);
        letter-spacing: -0.03em;
      }
      header .meta { color: var(--muted); font-size: 0.82rem; }
      h2 {
        font-size    : 1rem;
        font-weight  : 600;
        margin-bottom: 16px;
        color        : var(--text);
      }
      .query-box {
        background   : var(--surface);
        border       : 1px solid var(--border);
        border-left  : 3px solid var(--accent);
        border-radius: 8px;
        padding      : 12px 16px;
        margin-bottom: 24px;
        font-style   : italic;
        color        : var(--muted);
      }
      table {
        width          : 100%;
        border-collapse: collapse;
        background     : var(--surface);
        border-radius  : 8px;
        overflow       : hidden;
        border         : 1px solid var(--border);
      }
      thead th {
        background  : var(--accent);
        color       : #0D1117;
        font-weight : 600;
        padding     : 10px 14px;
        text-align  : left;
        font-size   : 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }
      tbody tr { border-bottom: 1px solid var(--border); }
      tbody tr:last-child { border-bottom: none; }
      tbody tr:hover { background: rgba(0, 212, 170, 0.04); }
      tbody td {
        padding   : 9px 14px;
        color     : var(--text);
        font-size : 0.88rem;
        font-family: var(--mono);
      }
      footer {
        margin-top  : 24px;
        color       : var(--muted);
        font-size   : 0.78rem;
        border-top  : 1px solid var(--border);
        padding-top : 12px;
        display     : flex;
        justify-content: space-between;
      }
    </style>
"""


class ResultGenerator:
    """
    Generate downloadable outputs in various formats: Pure rendering functions — no file I/O (that is ExportManager's job)
    """
    def generate_simple_text(self, query: str, answer: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        SEP   = "=" * 80
        DASH  = "-" * 80
        
        lines = [SEP,
                 f"  {RESULT_BRANDING_NAME} —  Query Result",
                 SEP,
                 f"\nTimestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                 f"Query     : {query}",
                 DASH,
                 f"\nAnswer:\n{answer}",
                ]

        if metadata:
            lines.append(f"\n{DASH}\nMetadata:")
            for key, value in metadata.items():
                lines.append(f"  {key}: {value}")

        lines.append(f"\n{SEP}")

        return "\n".join(lines)


    def generate_json(self, query: str, answer: str, data: Optional[List[Dict[str, Any]]] = None, sql_queries: Optional[List[str]] = None, 
                      reasoning_trace : Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None) -> str:
        
        result = {"timestamp"       : datetime.now().isoformat(),
                  "query"           : query,
                  "answer"          : answer,
                  "sql_queries"     : sql_queries     or [],
                  "reasoning_trace" : reasoning_trace or [],
                  "data"            : data            or [],
                  "metadata"        : metadata        or {},
                 }

        return json.dumps(obj     = result, 
                          indent  = 4, 
                          default = str,
                         )


    def generate_csv_from_dataframe(self, df: pd.DataFrame) -> str:
        output = io.StringIO()
        
        df.to_csv(output, index = False)
        
        return output.getvalue()


    def generate_csv_from_data(self, data: List[Dict[str, Any]]) -> str:
        if not data:
            return ""

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
        
        return output.getvalue()


    def generate_html_table(self, data: List[Dict[str, Any]], title: str = "Query Results",) -> str:
        
        if not data:
            return "<p style='font-family:sans-serif;color:#8B949E'>No data available</p>"

        df         = pd.DataFrame(data)

        # escape=True prevents stored XSS from malicious DB entries
        html_table = df.to_html(index   = False,
                                classes = "bi-table",
                                border  = 0,
                                escape  = True,
                               )

        ts         = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row_count  = len(data)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  {_HTML_STYLE}
</head>
<body>
  <header>
    <span class="logo">{RESULT_BRANDING_NAME}</span>
    <span class="meta">{RESULT_BRANDING_TAGLINE}</span>
  </header>

  <h2>{title}</h2>

  {html_table}

  <footer>
    <span>Generated: {ts}</span>
    <span>{row_count:,} records</span>
  </footer>
</body>
</html>"""


    def generate_markdown_table(self, data: List[Dict[str, Any]], title: str = "Query Results") -> str:
        if not data:
            return "No data available"

        df = pd.DataFrame(data)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        md = f"# {title}\n_Generated: {ts}_\n\n"

        try:
            md += df.to_markdown(index = False)
        
        except ImportError:
            # Graceful fallback if tabulate is not installed
            md += df.to_csv(index = False, sep = "|")

        md += f"\n\n**Total Records:** {len(data):,}"
        
        return md


# GLOBAL INSTANCE
result_generator = ResultGenerator()