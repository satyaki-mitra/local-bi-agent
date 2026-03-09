# DEPENDENCIES
import structlog
import numpy  as np
import pandas as pd
from typing import Any
from typing import Dict
from typing import List
from scipy import stats
from typing import Tuple
from typing import Optional
from config.constants import ANALYSIS_STRONG_CORR_POS
from config.constants import ANALYSIS_STRONG_CORR_NEG
from config.constants import ANALYSIS_MAX_VALUE_COUNTS
from config.constants import ANALYSIS_CORRELATION_TOP_N
from config.constants import ANALYSIS_MAX_OUTLIER_VALUES


# Setup Logging
logger = structlog.get_logger()

# Priority-ordered name patterns used when auto-selecting the y-axis column for time-series analysis: 
# Columns matching any of these substrings (case-insensitive) are preferred over the first-found numeric column, which could be a zip code or age
_TS_VALUE_COL_PRIORITY      : Tuple = ("amount", "revenue", "value", "cost", "price", "total", "count", "sales", "income", "profit")

# Minimum absolute slope threshold used as a floor when the series mean is near zero, preventing every tiny fluctuation from being classified as trending
_TREND_MIN_ABS_SLOPE        : float = 0.001

# Skewness thresholds — moderate: |skew| > 0.5, high: |skew| > 1.0
_SKEW_HIGH                  : float = 1.0
_SKEW_MOD                   : float = 0.5

# Kurtosis excess threshold for heavy-tailed distribution label
_KURT_HEAVY                 : float = 1.0

_MIN_ROWS_FOR_OUTLIERS      : int   = 10      # minimum rows to run outlier detection
_MIN_ROWS_FOR_CORRELATION   : int   = 3       # at least 3 rows to compute correlation
_MIN_UNIQUE_FOR_CORRELATION : int   = 2       # need at least 2 distinct values per column (implied by variance)
_MIN_ROWS_FOR_TIME_SERIES   : int   = 5       # at least 5 data points for trend analysis (more robust than 3)
_MIN_VARIANCE               : float = 1e-6    # minimum variance for a numeric column to be considered non‑constant


class DataAnalyzer:
    """
    Perform statistical analysis on query results with strict safety rails: all methods are pure functions — accept a DataFrame, return plain dicts or strings
    """
    # Guards & Utilities
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
        Heuristic: skip surrogate-key columns from numerical summaries: columns named 'id' 
        or ending in '_id' / '_key' are almost never useful BI metrics
        """
        lower = col.lower()
        return ((lower == "id") or lower.endswith("_id") or lower.endswith("_key"))


    def _distribution_shape(self, series: pd.Series) -> str:
        """
        Classify a numeric series into a distribution shape label based on skewness and kurtosis:

        Returns one of: 'normal' · 'right-skewed' · 'left-skewed' · 'heavy-tailed' · 'uniform-like'
        """
        try:
            skew = float(series.skew())

            # Excess of kurtosis (normal = 0)
            kurt = float(series.kurtosis())    

            if (abs(skew) <= _SKEW_MOD):
                return "heavy-tailed" if (kurt > _KURT_HEAVY) else "normal"

            if (skew > _SKEW_HIGH):
                return "right-skewed"

            if (skew < -_SKEW_HIGH):
                return "left-skewed"

            return "right-skewed" if (skew > 0) else "left-skewed"

        except Exception:
            return "unknown"


    # Check if a numeric column has meaningful variation
    def _has_variance(self, series: pd.Series) -> bool:
        """
        Return True if the series has at least 2 unique values and variance > threshold
        """
        if (series.nunique() < 2):
            return False

        try:
            return series.var() > _MIN_VARIANCE
        
        except:
            return False


    # Summary statistics 
    def generate_summary_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Structured summary covering:
        - Shape, types, missing values, memory usage

        - Per-column numerical stats: mean · median · std · min · max · q1 · q3 · iqr
          + percentiles (p10 · p25 · p50 · p75 · p90 · p95 · p99)
          + skewness · kurtosis · distribution shape
        
        - Categorical distributions (unique count, mode, top values)

        ID-like columns (id, *_id, *_key) are excluded from numerical stats: this method is always run because it's cheap.
        """
        if (df is None or df.empty):
            return {"error"         : "Empty DataFrame",
                    "total_rows"    : 0,
                    "total_columns" : 0,
                   }

        summary : Dict[str, Any] = {"total_rows"     : len(df),
                                    "total_columns"  : len(df.columns),
                                    "columns"        : list(df.columns),
                                    "dtypes"         : df.dtypes.astype(str).to_dict(),
                                    "missing_values" : df.isnull().sum().to_dict(),
                                    "memory_usage_mb": round(df.memory_usage(deep = True).sum() / (1024 * 1024), 2),
                                   }

        # Numerical analysis (skip surrogate keys)
        num_cols                 = [c for c in df.select_dtypes(include = ["number"]).columns if not self._is_id_column(c)]

        if num_cols:
            summary["numerical_summary"] = dict()

            for col in num_cols:
                try:
                    series                            = df[col].dropna()

                    if series.empty:
                        continue

                    q1                                = self._safe_float(series.quantile(0.25))
                    q3                                = self._safe_float(series.quantile(0.75))
                    iqr                               = round(q3 - q1, 4) if (q1 is not None and q3 is not None) else None

                    summary["numerical_summary"][col] = {"mean"      : self._safe_float(series.mean()),
                                                         "median"    : self._safe_float(series.median()),
                                                         "std"       : self._safe_float(series.std()),
                                                         "min"       : self._safe_float(series.min()),
                                                         "max"       : self._safe_float(series.max()),
                                                         "q1"        : q1,
                                                         "q3"        : q3,
                                                         "iqr"       : iqr,
                                                         "p10"       : self._safe_float(series.quantile(0.10)),
                                                         "p90"       : self._safe_float(series.quantile(0.90)),
                                                         "p95"       : self._safe_float(series.quantile(0.95)),
                                                         "p99"       : self._safe_float(series.quantile(0.99)),
                                                         "skewness"  : self._safe_float(series.skew()),
                                                         "kurtosis"  : self._safe_float(series.kurtosis()),
                                                         "shape"     : self._distribution_shape(series),
                                                         "non_null"  : int(series.count()),
                                                        }

                except Exception as e:
                    logger.warning("Failed to compute numerical stats",
                                   column = col,
                                   error  = str(e),
                                  )

        # Categorical analysis
        cat_cols = df.select_dtypes(include = ["object", "category", "string"]).columns

        if (len(cat_cols) > 0):
            summary["categorical_summary"] = dict()

            for col in cat_cols:
                try:
                    value_counts                        = df[col].value_counts(dropna = True).head(ANALYSIS_MAX_VALUE_COUNTS)
                    mode_val                            = df[col].mode(dropna = True)

                    summary["categorical_summary"][col] = {"unique_values" : int(df[col].nunique()),
                                                           "most_common"   : value_counts.to_dict(),
                                                           "mode"          : str(mode_val.iloc[0]) if not mode_val.empty else None,
                                                           "null_count"    : int(df[col].isnull().sum()),
                                                          }

                except Exception as e:
                    logger.warning("Failed to compute categorical stats",
                                   column = col,
                                   error  = str(e),
                                  )

        return summary


    # Correlation analysis (enhanced with variance check)
    def generate_correlation_analysis(self, df: pd.DataFrame, method: str = "pearson") -> Dict[str, Any]:
        """
        Pairwise correlations for non-ID numeric columns, ranked by absolute strength;
        returns an error dict if data is insufficient or columns are constant
        """
        num_cols = [c for c in df.select_dtypes(include = ["number"]).columns if not self._is_id_column(c)]

        # Require at least 2 columns, enough rows, and non‑constant data
        if (len(num_cols) < 2):
            return {"error": "Need at least 2 non-ID numerical columns for correlation"}

        if (len(df) < _MIN_ROWS_FOR_CORRELATION):
            return {"error": f"Insufficient rows ({len(df)}) for meaningful correlation (need ≥{_MIN_ROWS_FOR_CORRELATION})"}

        # Check each column for variance
        valid_cols = list()

        for col in num_cols:
            if self._has_variance(df[col].dropna()):
                valid_cols.append(col)

        if (len(valid_cols) < 2):
            return {"error": "Numerical columns have near‑zero variance – correlation not meaningful"}

        if (method not in ("pearson", "spearman")):
            return {"error": f"Unknown method '{method}'. Use 'pearson' or 'spearman'."}

        try:
            corr_matrix                         = df[valid_cols].corr(method = method)
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
            logger.error("Correlation analysis failed", 
                         error = str(e),
                        )

            return {"error": str(e)}


    # Outlier detection (enhanced with row count check)
    def detect_outliers(self, df: pd.DataFrame, column: str, method: str = "iqr", iqr_sensitivity: float = 1.5) -> Dict[str, Any]:
        """
        Detect outliers in a single numeric column: and returns an error if too few rows or column is constant
        """
        if (column not in df.columns):
            return {"error": f"Column '{column}' not found in DataFrame"}

        series = df[column].dropna()

        if series.empty:
            return {"error": f"Column '{column}' has no non-null values"}

        if (len(series) < _MIN_ROWS_FOR_OUTLIERS):
            return {"error": f"Too few non‑null rows ({len(series)}) for outlier detection (need ≥{_MIN_ROWS_FOR_OUTLIERS})"}

        if (not self._has_variance(series)):
            return {"error": "Column has near‑zero variance – no meaningful outliers"}

        if (iqr_sensitivity <= 0):
            return {"error": "iqr_sensitivity must be > 0"}

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
                return {"error": f"Unknown method '{method}'. Use 'iqr' or 'zscore'"}

            return {"column"             : column,
                    "method"             : method,
                    "iqr_sensitivity"    : iqr_sensitivity if (method == "iqr") else None,
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
                         error  = str(e),
                        )
            return {"error": str(e)}


    def detect_all_outliers(self, df: pd.DataFrame, method: str = "iqr", iqr_sensitivity: float = 1.5) -> Dict[str, Any]:
        """
        Run detect_outliers() for every non-ID numeric column and return a summary dict: skips columns that don't pass the row/variance checks
        """
        num_cols                 = [c for c in df.select_dtypes(include = ["number"]).columns if not self._is_id_column(c)]

        if not num_cols:
            return {"method"  : method, 
                    "columns" : {}, 
                    "flagged" : [],
                   }

        columns : Dict[str, Any] = dict()
        flagged : List[str]      = list()

        for col in num_cols:
            result = self.detect_outliers(df, col, method, iqr_sensitivity)

            if ("error" not in result):
                columns[col] = {"total_outliers"     : result["total_outliers"],
                                "outlier_percentage" : result["outlier_percentage"],
                                "bounds"             : result["bounds"],
                               }

                if (result["outlier_percentage"] > 5.0):
                    flagged.append(col)

        return {"method"  : method,
                "columns" : columns,
                "flagged" : flagged,
               }


    # Time-series analysis (enhanced with row count and variance checks)
    def generate_time_series_analysis(self, df: pd.DataFrame, date_col: str, value_col: str) -> Dict[str, Any]:
        """
        Trend direction, slope, R², volatility (CV), and period-over-period delta and returns error if data is insufficient
        """
        if (date_col not in df.columns or value_col not in df.columns):
            return {"error": f"Columns '{date_col}' or '{value_col}' not found"}

        # Check for sufficient rows
        if (len(df) < _MIN_ROWS_FOR_TIME_SERIES):
            return {"error": f"Need at least {_MIN_ROWS_FOR_TIME_SERIES} rows for time‑series analysis (got {len(df)})"}

        # Check value column variance
        if (not self._has_variance(df[value_col].dropna())):
            return {"error": "Value column has near‑zero variance – trend not meaningful"}

        try:
            ts = (df[[date_col, value_col]]
                  .dropna()
                  .sort_values(date_col)
                  .reset_index(drop = True)
                 )

            if (len(ts) < 3):
                return {"error": "Need at least 3 data points for trend analysis"}

            values                                      = pd.to_numeric(ts[value_col], errors = "coerce").dropna()
            ts                                          = ts.loc[values.index]
            values                                      = values.values

            x                                           = np.arange(len(values), dtype = float)
            slope, intercept, r_value, p_value, std_err = stats.linregress(x, values)

            mean_val                                    = float(np.mean(values))
            std_val                                     = float(np.std(values))

            # Trend direction: use relative slope (normalised by mean) to be scale-invariant
            if (mean_val != 0):
                relative_slope  = abs(slope) / abs(mean_val)
                trend_threshold = 0.01    # 1% of mean per step

            else:
                relative_slope  = abs(slope)
                trend_threshold = _TREND_MIN_ABS_SLOPE

            if ((slope > 0) and (relative_slope > trend_threshold or slope > _TREND_MIN_ABS_SLOPE)):
                trend = "upward"

            elif ((slope < 0) and (relative_slope > trend_threshold or abs(slope) > _TREND_MIN_ABS_SLOPE)):
                trend = "downward"

            else:
                trend = "flat"

            cov       = round(std_val / mean_val * 100, 2) if (mean_val != 0) else None
            mid       = len(ts) // 2
            earlier   = float(values[:mid].mean()) if (mid > 0) else None
            recent    = float(values[mid:].mean()) if (mid < len(ts)) else None

            pct_delta = None

            if (earlier and (earlier != 0) and (recent is not None)):
                pct_delta = round((recent - earlier) / abs(earlier) * 100, 2)

            return {"date_range"    : {"start" : str(ts[date_col].iloc[0]),
                                       "end"   : str(ts[date_col].iloc[-1]),
                                      },
                    "data_points"   : len(ts),
                    "overall_trend" : trend,
                    "trend_slope"   : self._safe_float(slope),
                    "r_squared"     : self._safe_float(r_value ** 2),
                    "p_value"       : self._safe_float(p_value),
                    "volatility"    : {"std"                         : self._safe_float(std_val),
                                       "coefficient_of_variation_pct": cov,
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
            return {"error": str(e)}


    def _pick_ts_value_col(self, df: pd.DataFrame, num_cols: List[str]) -> str:
        """
        Choose the most meaningful numeric column for time-series y-axis

        Priority order:
          1. First column whose name contains a known business-metric keyword (amount, revenue, value, cost, etc.) — defined in _TS_VALUE_COL_PRIORITY
          2. Column with the highest variance across actual data values (and not ID-like)
          3. First available column as fallback
        """
        # Priority 1: keyword match, but exclude ID-like columns
        for keyword in _TS_VALUE_COL_PRIORITY:
            for col in num_cols:
                if keyword in col.lower() and not self._is_id_column(col):
                    return col

        # Priority 2: highest data variance (most informative series for trending)
        try:
            variances = dict()

            for col in num_cols:
                if self._is_id_column(col):
                    continue

                var = float(df[col].dropna().var())

                if (not np.isnan(var) and (var > _MIN_VARIANCE)):
                    variances[col] = var

            if variances:
                return max(variances, key = variances.get)

        except Exception:
            pass

        # Fallback: first non-ID column
        for col in num_cols:
            if not self._is_id_column(col):
                return col

        # If all are IDs, just return the first
        return num_cols[0]


    # Comprehensive report (enhanced with intelligence)
    def generate_comprehensive_report(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Combine all available analyses into a single report dict: skips expensive analyses when data is too trivial
        """
        if (df is None or df.empty):
            return {"timestamp"          : pd.Timestamp.now().isoformat(),
                    "summary_statistics" : {"error"         : "Empty DataFrame",
                                            "total_rows"    : 0,
                                            "total_columns" : 0,
                                           },
                   }

        # Always include summary statistics (cheap)
        report   : Dict[str, Any] = {"timestamp"          : pd.Timestamp.now().isoformat(),
                                     "summary_statistics" : self.generate_summary_statistics(df),
                                    }

        # Prepare numeric columns (non-ID)
        num_cols                  = [c for c in df.select_dtypes(include = ["number"]).columns if not self._is_id_column(c)]

        # Outlier summary – only if enough rows and there are numeric columns
        if (num_cols and (len(df) >= _MIN_ROWS_FOR_OUTLIERS)):
            outlier_res = self.detect_all_outliers(df)
            
            # Only include if at least one column had a successful result
            if outlier_res.get("columns"):
                report["outlier_summary"] = outlier_res

        # Correlation analysis – require at least 2 numeric columns and enough rows
        if ((len(num_cols) >= 2) and (len(df) >= _MIN_ROWS_FOR_CORRELATION)):
            corr_res = self.generate_correlation_analysis(df)

            if ("error" not in corr_res):
                report["correlation_analysis"] = corr_res

        # Time-series analysis – auto-detect datetime column and pick value column
        dt_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
        
        if (dt_cols and num_cols and (len(df) >= _MIN_ROWS_FOR_TIME_SERIES)):
            # For each datetime column, try to find a suitable value column
            for dt_col in dt_cols:
                value_col = self._pick_ts_value_col(df, num_cols)
                ts_res    = self.generate_time_series_analysis(df, dt_col, value_col)
                
                if ("error" not in ts_res):
                    report["time_series_analysis"] = ts_res
                    # take the first successful one
                    break  

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
            s      = analysis["summary_statistics"]

            lines += [DASH, "SUMMARY STATISTICS", DASH]

            lines.append(f"Rows          : {s.get('total_rows', 0):,}")
            lines.append(f"Columns       : {s.get('total_columns', 0)}")
            lines.append(f"Memory        : {s.get('memory_usage_mb', 0)} MB")

            mv     = {k: v for k, v in s.get("missing_values", {}).items() if v > 0}

            if mv:
                lines.append(f"Missing data  : {mv}")

            lines.append("")

            if ("numerical_summary" in s):
                lines += ["Numerical Columns", DASH]

                for col, cs in s["numerical_summary"].items():
                    shape      = cs.get("shape", "")
                    lines.append(f"  {col}  [{shape}]:")

                    stat_pairs = [("mean", "mean"),
                                  ("median", "median"),
                                  ("std", "std"),
                                  ("min", "min"),
                                  ("max", "max"),
                                  ("p10", "p10"),
                                  ("p90", "p90"),
                                  ("p99", "p99"),
                                  ("skewness", "skewness"),
                                  ("kurtosis", "kurtosis"),
                                 ]

                    for label, key in stat_pairs:
                        val = cs.get(key)

                        if val is not None:
                            try:
                                lines.append(f"    {label.title():12}: {val:,.4f}")

                            except (TypeError, ValueError):
                                lines.append(f"    {label.title():12}: {val}")

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

        # Outlier summary
        if ("outlier_summary" in analysis):
            os_     = analysis["outlier_summary"]
            flagged = os_.get("flagged", [])

            if flagged:
                lines += [DASH, "OUTLIER SUMMARY (>5% flagged)", DASH]

                for col in flagged:
                    info = os_["columns"].get(col, {})
                    pct  = info.get("outlier_percentage", 0)
                    cnt  = info.get("total_outliers", 0)
                    lines.append(f"  {col}: {cnt} outliers ({pct:.1f}%)")

                lines.append("")

        # Correlation
        if ("correlation_analysis" in analysis):
            ca = analysis["correlation_analysis"]

            if ("error" not in ca):
                method_label = ca.get("method", "pearson").capitalize()
                lines       += [DASH, f"CORRELATION ANALYSIS ({method_label})", DASH]

                for item in ca.get("top_correlations", [])[:5]:
                    lines.append(f"  {item['column1']} × {item['column2']:30}  r = {item['correlation']:+.4f}")

                lines.append("")

        # Time-series
        if ("time_series_analysis" in analysis):
            ta = analysis["time_series_analysis"]

            if ("error" not in ta):
                lines += [DASH, "TIME-SERIES ANALYSIS", DASH]
                dr     = ta.get("date_range", {})

                lines.append(f"Date range  : {dr.get('start', '')} to {dr.get('end', '')}")
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