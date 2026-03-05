# DEPENDENCIES
import structlog
import numpy  as np
import pandas as pd
from typing import Any
from typing import Dict
from typing import List
from scipy import stats
from typing import Optional
from config.constants import ANALYSIS_STRONG_CORR_POS
from config.constants import ANALYSIS_STRONG_CORR_NEG
from config.constants import ANALYSIS_MAX_VALUE_COUNTS
from config.constants import ANALYSIS_CORRELATION_TOP_N
from config.constants import ANALYSIS_MAX_OUTLIER_VALUES


logger = structlog.get_logger()

# Priority-ordered name patterns used when auto-selecting the y-axis column for time-series analysis in generate_comprehensive_report().
# Columns whose names contain any of these substrings (case-insensitive) are preferred over the first-found numeric column, which could be a zip code or age.
_TS_VALUE_COL_PRIORITY : tuple = ("amount", "revenue", "value", "cost", "price", "total", "count", "sales", "income", "profit")

# Minimum absolute slope threshold used as a floor when mean is near zero, preventing every tiny fluctuation from being classified as trending.
_TREND_MIN_ABS_SLOPE   : float = 0.001


class DataAnalyzer:
    """
    Perform statistical analysis on query results with strict safety rails: all methods are pure functions: accept a DataFrame, return plain dicts or strings
    — no file I/O, no side effects.
    """
    def _safe_float(self, val: Any) -> Optional[float]:
        """
        Convert to float safely, returning None for Inf / NaN / non-numeric
        """
        try:
            f = float(val)

            if np.isinf(f) or np.isnan(f):
                return None

            return f

        except (TypeError, ValueError):
            return None


    def _is_id_column(self, col: str) -> bool:
        """
        Heuristic: skip surrogate-key columns from numerical summaries: columns named 'id', or ending in '_id' / '_key', are almost never meaningful BI metrics
        """
        lower = col.lower()
        return ((lower == "id") or lower.endswith("_id") or lower.endswith("_key"))


    def generate_summary_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Returns a structured summary covering shape, types, missing values, per-column numerical stats (mean/median/std/min/max/q1/q3/iqr),
        and categorical distributions; ID-like columns are excluded from numerical stats
        """
        if (df is None or df.empty):
            return {"error"         : "Empty DataFrame",
                    "total_rows"    : 0,
                    "total_columns" : 0,
                   }

        summary : Dict[str, Any] = {"total_rows"      : len(df),
                                    "total_columns"    : len(df.columns),
                                    "columns"          : list(df.columns),
                                    "dtypes"           : df.dtypes.astype(str).to_dict(),
                                    "missing_values"   : df.isnull().sum().to_dict(),
                                    "memory_usage_mb"  : round(df.memory_usage(deep=True).sum() / (1024 * 1024), 2),
                                   }

        # Numerical (skip surrogate keys)
        num_cols = [c for c in df.select_dtypes(include=["number"]).columns if not self._is_id_column(c)]

        if num_cols:
            summary["numerical_summary"] = dict()

            for col in num_cols:
                try:
                    series                            = df[col].dropna()
                    q1                                = self._safe_float(series.quantile(0.25))
                    q3                                = self._safe_float(series.quantile(0.75))
                    iqr                               = round(q3 - q1, 4) if (q1 is not None and q3 is not None) else None

                    summary["numerical_summary"][col] = {"mean"   : self._safe_float(series.mean()),
                                                         "median" : self._safe_float(series.median()),
                                                         "std"    : self._safe_float(series.std()),
                                                         "min"    : self._safe_float(series.min()),
                                                         "max"    : self._safe_float(series.max()),
                                                         "q1"     : q1,
                                                         "q3"     : q3,
                                                         "iqr"    : iqr,
                                                        }
                except Exception as e:
                    logger.warning("Failed to compute numerical stats",
                                   column = col,
                                   error  = str(e),
                                  )

        # Categorical Analysis
        cat_cols = df.select_dtypes(include=["object", "category", "string"]).columns

        if (len(cat_cols) > 0):
            summary["categorical_summary"] = dict()

            for col in cat_cols:
                try:
                    value_counts                        = df[col].value_counts(dropna=True).head(ANALYSIS_MAX_VALUE_COUNTS)
                    mode_val                            = df[col].mode(dropna=True)

                    summary["categorical_summary"][col] = {"unique_values" : int(df[col].nunique()),
                                                           "most_common"   : value_counts.to_dict(),
                                                           "mode"          : str(mode_val.iloc[0]) if not mode_val.empty else None,
                                                          }
                except Exception as e:
                    logger.warning("Failed to compute categorical stats",
                                   column = col,
                                   error  = str(e),
                                  )

        return summary


    def generate_correlation_analysis(self, df: pd.DataFrame, method: str = "pearson") -> Dict[str, Any]:
        """
        Pairwise correlations for non-ID numeric columns, ranked by absolute strength

        - method: 'pearson' (default, linear relationships) or 'spearman' (rank-based, more robust for skewed financial/insurance data with outliers or non-normal
                   distributions — e.g. claim amounts, transaction values).

        - For BI datasets with heavy right-skew, consider passing method='spearman'
        """
        num_cols = [c for c in df.select_dtypes(include=["number"]).columns if not self._is_id_column(c)]

        if (len(num_cols) < 2):
            return {"error": "Need at least 2 non-ID numerical columns for correlation"}

        if (method not in ("pearson", "spearman")):
            return {"error": f"Unknown method '{method}'. Use 'pearson' or 'spearman'."}

        try:
            corr_matrix                         = df[num_cols].corr(method = method)
            correlations : List[Dict[str, Any]] = list()

            for i in range(len(corr_matrix.columns)):
                for j in range(i + 1, len(corr_matrix.columns)):
                    val = corr_matrix.iloc[i, j]

                    if not pd.isna(val) and not np.isinf(val):
                        correlations.append({"column1"     : corr_matrix.columns[i],
                                             "column2"     : corr_matrix.columns[j],
                                             "correlation" : round(float(val), 4),
                                            })

            correlations.sort(key = lambda x: abs(x["correlation"]), reverse = True)

            return {"method"           : method,
                    "top_correlations" : correlations[:ANALYSIS_CORRELATION_TOP_N],
                    "strong_positive"  : [c for c in correlations if c["correlation"] >  ANALYSIS_STRONG_CORR_POS][:5],
                    "strong_negative"  : [c for c in correlations if c["correlation"] <  ANALYSIS_STRONG_CORR_NEG][:5],
                   }

        except Exception as e:
            logger.error("Correlation analysis failed", error = str(e))
            return {"error": str(e)}


    def detect_outliers(self, df: pd.DataFrame, column: str, method: str = "iqr",
                        iqr_sensitivity: float = 1.5) -> Dict[str, Any]:
        """
        Detect outliers in a single numeric column: 

        Method: 
        -------
        - 'iqr' (Tukey fence, default) or 
        - 'zscore' (3-sigma rule)

        Returns total_outliers, outlier_percentage, up to 20 outlier_values, and the bounds used
        """
        if (column not in df.columns):
            return {"error" : f"Column '{column}' not found in DataFrame"}

        series = df[column].dropna()

        if series.empty:
            return {"error" : f"Column '{column}' has no non-null values"}

        if (iqr_sensitivity <= 0):
            return {"error" : "iqr_sensitivity must be > 0"}

        try:
            if (method == "iqr"):
                q1           = series.quantile(0.25)
                q3           = series.quantile(0.75)
                iqr          = q3 - q1
                lower_bound  = q1 - iqr_sensitivity * iqr
                upper_bound  = q3 + iqr_sensitivity * iqr
                outlier_mask = (series < lower_bound) | (series > upper_bound)

            elif (method == "zscore"):
                z_scores     = np.abs(stats.zscore(series))
                outlier_mask = z_scores > 3
                lower_bound  = float(series.mean() - 3 * series.std())
                upper_bound  = float(series.mean() + 3 * series.std())

            else:
                return {"error" : f"Unknown method '{method}'. Use 'iqr' or 'zscore'"}

            return {"column"             : column,
                    "method"             : method,
                    "iqr_sensitivity"    : iqr_sensitivity if method == "iqr" else None,
                    "total_outliers"     : int(outlier_mask.sum()),
                    "outlier_percentage" : round(float(outlier_mask.sum()) / len(series) * 100, 2),
                    "outlier_values"     : series[outlier_mask].tolist()[:ANALYSIS_MAX_OUTLIER_VALUES],
                    "bounds"             : {"lower" : self._safe_float(lower_bound),
                                            "upper" : self._safe_float(upper_bound),
                                           },
                   }

        except Exception as e:
            logger.error("Outlier detection failed",
                         column = column,
                         method = method,
                         error  = str(e),
                        )

            return {"error" : str(e)}


    def generate_time_series_analysis(self, df: pd.DataFrame, date_col: str, value_col: str) -> Dict[str, Any]:
        """
        Characterise a time series: date range, trend direction (OLS linear regression slope),
        volatility (coefficient of variation), and recent vs earlier period comparison: all returned values are JSON-safe

        Trend classification uses a normalised threshold: abs(slope) > max(0.01 * abs(mean), _TREND_MIN_ABS_SLOPE)

        This prevents near-zero mean values (e.g. pct-change columns, difference series) from collapsing the threshold to near-zero 
        and misclassifying trivial noise as trends. The _TREND_MIN_ABS_SLOPE floor (0.001) handles the edge case where mean == 0
        """
        if ((date_col not in df.columns) or (value_col not in df.columns)):
            return {"error" : f"Columns '{date_col}' or '{value_col}' not found"}

        try:
            ts           = df[[date_col, value_col]].dropna().copy()
            ts[date_col] = pd.to_datetime(ts[date_col], errors="coerce")
            ts           = ts.dropna(subset=[date_col]).sort_values(date_col)

            if (len(ts) < 2):
                return {"error" : "Need at least 2 data points for time-series analysis"}

            values                  = ts[value_col].astype(float).values
            x                       = np.arange(len(values))
            slope, _, r_value, _, _ = stats.linregress(x, values)

            mean_val                = float(values.mean())
            std_val                 = float(values.std())
            trend_threshold         = max(0.01 * abs(mean_val), _TREND_MIN_ABS_SLOPE)

            if (slope >  trend_threshold):
                trend = "upward"

            elif (slope < -trend_threshold):
                trend = "downward"

            else:
                trend = "flat"

            cov                     = round(std_val / mean_val * 100, 2) if (mean_val != 0) else None
            mid                     = len(ts) // 2
            earlier                 = float(values[:mid].mean()) if (mid > 0) else None
            recent                  = float(values[mid:].mean()) if mid < len(ts) else None
            pct_delta               = None

            if (earlier and (earlier != 0) and (recent is not None)):
                pct_delta = round((recent - earlier) / abs(earlier) * 100, 2)

            return {"date_range"    : {"start" : str(ts[date_col].iloc[0].date()),
                                       "end"   : str(ts[date_col].iloc[-1].date()),
                                      },
                    "data_points"   : len(ts),
                    "overall_trend" : trend,
                    "trend_slope"   : self._safe_float(slope),
                    "r_squared"     : self._safe_float(r_value ** 2),
                    "volatility"    : {"std"                          : self._safe_float(std_val),
                                       "coefficient_of_variation_pct" : cov,
                                      },
                    "recent_trend"  : {"earlier_period_avg" : self._safe_float(earlier),
                                       "recent_period_avg"  : self._safe_float(recent),
                                       "change_pct"         : pct_delta,
                                      },
                   }

        except Exception as e:
            logger.error("Time-series analysis failed",
                         date_col  = date_col,
                         value_col = value_col,
                         error     = str(e),
                        )

            return {"error" : str(e)}


    def _pick_ts_value_col(self, num_cols: List[str]) -> str:
        """
        Choose the most meaningful numeric column for time-series y-axis.

        Priority order:
          1. First column whose name contains a known business-metric keyword (amount, revenue, value, cost, etc.) — defined in _TS_VALUE_COL_PRIORITY
          2. Column with the highest variance (most signal, least likely a code/ID)
          3. First available column as fallback

        This prevents auto-selecting zip codes, age bands, or status codes as the y-axis just because they happen to be the first numeric column in the result
        """
        for keyword in _TS_VALUE_COL_PRIORITY:
            for col in num_cols:
                if keyword in col.lower():
                    return col

        # Highest-variance column as secondary heuristic
        try:
            variances = {col: float(np.var([float(v) for v in [col] if isinstance(v, (int, float))])) for col in num_cols}
        
        except Exception:
            variances = {}

        if variances:
            return max(variances, key=variances.get)

        return num_cols[0]


    def generate_comprehensive_report(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Combine all available analyses into one report dict:

        Auto-detection improvements:
        - Time-series y-axis: uses _pick_ts_value_col() to prefer business-metric columns (amount, revenue, etc.) over arbitrary first-found numerics like zip codes
        - Correlation: uses Pearson by default; callers can invoke generate_correlation_analysis() directly with method='spearman' for skewed data
        """
        if (df is None or df.empty):
            return {"timestamp"          : pd.Timestamp.now().isoformat(),
                    "summary_statistics" : {"error": "Empty DataFrame", "total_rows": 0, "total_columns": 0},
                   }

        report : Dict[str, Any] = {"timestamp"          : pd.Timestamp.now().isoformat(),
                                   "summary_statistics" : self.generate_summary_statistics(df),
                                  }

        num_cols                = [c for c in df.select_dtypes(include=["number"]).columns if not self._is_id_column(c)]

        if (len(num_cols) >= 2):
            report["correlation_analysis"] = self.generate_correlation_analysis(df)

        # Auto-detect datetime col for time-series
        dt_cols  = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]

        if dt_cols and num_cols:
            value_col                      = self._pick_ts_value_col(num_cols)
            report["time_series_analysis"] = self.generate_time_series_analysis(df, dt_cols[0], value_col)

        return report


    def generate_text_report(self, analysis: Dict[str, Any]) -> str:
        """
        Render the comprehensive report dict as human-readable plain text suitable for .txt export
        """
        SEP               = "=" * 80
        DASH              = "-" * 80
        lines : List[str] = [SEP, "  LOCAL GEN BI — DATA ANALYSIS REPORT", SEP, ""]

        ts                = analysis.get("timestamp", "")

        if ts:
            lines += [f"Generated : {ts}", ""]

        # Summary statistics
        if ("summary_statistics" in analysis):
            s = analysis["summary_statistics"]
            lines += [DASH, "SUMMARY STATISTICS", DASH]
            lines.append(f"Rows          : {s.get('total_rows', 0):,}")
            lines.append(f"Columns       : {s.get('total_columns', 0)}")
            lines.append(f"Memory        : {s.get('memory_usage_mb', 0)} MB")
            mv = {k: v for k, v in s.get("missing_values", {}).items() if v > 0}

            if mv:
                lines.append(f"Missing data  : {mv}")
            lines.append("")

            if ("numerical_summary" in s):
                lines += ["Numerical Columns", DASH]
                for col, cs in s["numerical_summary"].items():
                    lines.append(f"  {col}:")

                    for key, val in cs.items():
                        if val is not None:
                            try:
                                lines.append(f"    {key.replace('_',' ').title():12}: {val:,.4f}")

                            except (TypeError, ValueError):
                                lines.append(f"    {key.replace('_',' ').title():12}: {val}")

                lines.append("")

            if ("categorical_summary" in s):
                lines += ["Categorical Columns", DASH]

                for col, cs in s["categorical_summary"].items():
                    lines.append(f"  {col}:")
                    lines.append(f"    Unique values : {cs.get('unique_values', 0)}")
                    lines.append(f"    Mode          : {cs.get('mode', 'N/A')}")
                    top = list(cs.get("most_common", {}).items())[:5]

                    if top:
                        lines.append("    Top values    :")

                        for v, cnt in top:
                            lines.append(f"      {str(v)[:30]:32}: {cnt:,}")

                lines.append("")

        # Correlation
        if ("correlation_analysis" in analysis):
            ca = analysis["correlation_analysis"]

            if ("error" not in ca):
                method_label = ca.get("method", "pearson").capitalize()
                lines += [DASH, f"CORRELATION ANALYSIS ({method_label})", DASH]

                for item in ca.get("top_correlations", [])[:5]:
                    lines.append(f"  {item['column1']} x {item['column2']:30}  r = {item['correlation']:+.4f}")

                lines.append("")

        # Time-series
        if ("time_series_analysis" in analysis):
            ta = analysis["time_series_analysis"]

            if "error" not in ta:
                lines += [DASH, "TIME-SERIES ANALYSIS", DASH]
                dr     = ta.get("date_range", {})

                lines.append(f"Date range  : {dr.get('start','')} to {dr.get('end','')}")
                lines.append(f"Data points : {ta.get('data_points', 0)}")
                lines.append(f"Trend       : {ta.get('overall_trend', 'N/A').upper()}")
                lines.append(f"Slope       : {ta.get('trend_slope', 'N/A')}")
                lines.append(f"R-squared   : {ta.get('r_squared', 'N/A')}")
                rt = ta.get("recent_trend", {})

                if rt.get("change_pct") is not None:
                    lines.append(f"Period delta: {rt['change_pct']:+.2f}%")

                lines.append("")

        lines.append(SEP)
        return "\n".join(lines)


# GLOBAL INSTANCE
data_analyzer = DataAnalyzer()