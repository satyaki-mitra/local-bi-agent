# DEPENDENCIES
import io
import base64
import structlog
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

from typing import Any
from typing import Dict
from typing import List
from typing import Tuple
from typing import Optional
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
import matplotlib.ticker as mticker
from config.settings import settings
from matplotlib.figure import Figure
from config.constants import ChartType
import matplotlib.gridspec as gridspec
from config.constants import VIZ_PALETTE
from config.constants import VIZ_FONT_MAIN
from config.constants import VIZ_TITLE_MAX_LEN
from config.constants import VIZ_HISTOGRAM_MAX_BINS
from config.constants import VIZ_BAR_MAX_CATEGORIES
from config.constants import VIZ_HBAR_MAX_CATEGORIES
from config.constants import VIZ_BAR_XTICK_THRESHOLD
from config.constants import VIZ_LINE_XTICK_THRESHOLD
from config.constants import VIZ_HBAR_LABEL_LEN_THRESHOLD


# Setup Logging
logger = structlog.get_logger()


# Global Constants
_PALETTE                            = VIZ_PALETTE
_FONT_MAIN                          = VIZ_FONT_MAIN

# Max points in a scatter before sampling to avoid overplotting
_SCATTER_MAX_POINTS         : int   = 2000

# Donut: only use when unique category count falls in this range
_DONUT_MIN_CATS             : int   = 2
_DONUT_MAX_CATS             : int   = 8

# Multi-line: max numeric series rendered on a single chart
_MULTI_LINE_MAX_SERIES      : int   = 5

# Minimum rows to consider a visualization worthwhile
_MIN_ROWS_FOR_VISUALIZATION : int   = 2

# Threshold for constant/near-constant numeric columns (variance < 1e-6)
_VAR_MIN_THRESHOLD          : float = 1e-6


# Utility
def _short_number(value: float) -> str:
    """
    Format a number with K/M suffix for chart annotations
    """
    try:
        v = float(value)

        if (abs(v) >= 1_000_000):
            return f"{v/1_000_000:.1f}M"

        if (abs(v) >= 1_000):
            return f"{v/1_000:.1f}K"

        if (v == int(v)):
            return f"{int(v):,}"

        return f"{v:,.2f}"

    except (TypeError, ValueError):
        return str(value)


# Theme
def _apply_bi_theme(fig: Figure, ax: Axes) -> None:
    """
    Apply consistent dark BI styling to a figure/axes pair
    """
    fig.patch.set_facecolor(_PALETTE["bg_figure"])
    ax.set_facecolor(_PALETTE["bg_axes"])

    for spine in ax.spines.values():
        spine.set_edgecolor(_PALETTE["grid"])

    ax.tick_params(colors    = _PALETTE["text_muted"],
                   labelsize = 9,
                  )

    ax.xaxis.label.set_color(_PALETTE["text_muted"])
    ax.yaxis.label.set_color(_PALETTE["text_muted"])
    ax.title.set_color(_PALETTE["text_main"])

    ax.grid(True,
            color     = _PALETTE["grid"],
            linewidth = 0.6,
            linestyle = "--",
            alpha     = 0.7,
           )

    ax.set_axisbelow(True)


def _format_axis_values(ax: Axes, axis: str = "y") -> None:
    """
    Format large axis numbers with K/M suffixes for readability
    """
    def _formatter(x, _):
        if (abs(x) >= 1_000_000):
            return f"{x/1_000_000:.1f}M"

        if (abs(x) >= 1_000):
            return f"{x/1_000:.0f}K"

        return f"{x:,.0f}"

    target = ax.yaxis if (axis == "y") else ax.xaxis
    target.set_major_formatter(mticker.FuncFormatter(_formatter))


def _set_title(ax: Axes, title: str, fontsize: int = 13) -> None:
    """
    Apply a consistently styled chart title
    """
    ax.set_title(label      = title[:VIZ_TITLE_MAX_LEN],
                 fontsize   = fontsize,
                 fontweight = "semibold",
                 fontfamily = _FONT_MAIN,
                 pad        = 12,
                )


def _bar_gradient_colors(n: int) -> List[str]:
    """
    Return a list of n colors using the palette series list, cycling if necessary
    """
    series = _PALETTE.get("series", [_PALETTE["accent"]])

    return [series[i % len(series)] for i in range(n)]


def _is_id_column(col: str) -> bool:
    """
    Heuristic to detect ID-like columns (should not be plotted as main value)
    """
    lower = col.lower()
    return ((lower == "id") or (lower.endswith("_id")) or (lower.endswith("_key")))


def _is_date_column(series: pd.Series) -> bool:
    """
    Check if series can be parsed as datetime
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return True

    # Try coercing object/string to datetime
    try:
        pd.to_datetime(series, errors = 'raise')
        return True

    except:
        return False


# Visualization Generator
class VisualizationGenerator:
    """
    Generate professional dark-theme BI charts: all public methods return Optional[Figure] and 
    never raise — exceptions are caught and logged, returning None so the pipeline can degrade gracefully
    """
    def __init__(self):
        self.fig_size = (settings.viz_figure_width, 
                         settings.viz_figure_height,
                        )

        self.dpi      = settings.viz_dpi


    def _should_visualize(self, df: pd.DataFrame) -> bool:
        """
        Decide whether a visualization is worthwhile for this DataFrame

        Returns False if:
          - DataFrame has fewer than _MIN_ROWS_FOR_VISUALIZATION rows
          - All numeric columns are IDs (names ending in _id, _key) and no other numeric columns exist
          - The only numeric column has near-zero variance (constant)
        """
        if ((df is None) or df.empty or (len(df) < _MIN_ROWS_FOR_VISUALIZATION)):
            logger.debug("Skipping viz: insufficient rows", 
                         rows = len(df) if df is not None else 0,
                        )

            return False

        num_cols = [c for c in df.select_dtypes(include=["number"]).columns if not _is_id_column(c)]
        
        if not num_cols:
            # No meaningful numeric columns -> skip (maybe we could still do a bar chart of counts?)
            logger.debug("Skipping viz: no meaningful numeric columns")
            return False

        # If the first meaningful numeric column has near-zero variance, skip
        col = num_cols[0]

        if (df[col].var() < _VAR_MIN_THRESHOLD):
            logger.debug("Skipping viz: numeric column has near-zero variance", 
                         col = col,
                        )

            return False

        return True


    def _detect_chart_type(self, df: pd.DataFrame) -> ChartType:
        """
        Select the most appropriate chart type based on DataFrame column composition and column name hints

        Priority order (with refinements):
          1. Datetime column present → LINE (or MULTI_LINE if multiple numeric series)
          2. Single categorical column with few unique values and numeric column → DONUT if names suggest distribution
          3. Categorical + numeric columns present → BAR (or HBAR if labels are long)
          4. Two or more numeric columns → SCATTER (or HEATMAP if >3 numeric columns)
          5. Single numeric column → HISTOGRAM
        """
        has_datetime = any(_is_date_column(df[c]) for c in df.columns)

        if has_datetime:
            return ChartType.LINE

        num_cols     = [c for c in df.select_dtypes(include = ["number"]).columns if not _is_id_column(c)]
        cat_cols     = [c for c in df.select_dtypes(include = ["object", "category", "string"]).columns]

        # Donut opportunity: single category with few unique values + a numeric column
        if ((len(cat_cols) == 1) and (len(num_cols) >= 1)):
            n_unique = df[cat_cols[0]].nunique()

            if (_DONUT_MIN_CATS <= n_unique <= _DONUT_MAX_CATS):
                # Check column name for distribution keywords
                if any(kw in cat_cols[0].lower() for kw in ("status", "type", "category", "gender", "stage", "tier", "method", "plan")):
                    # signal donut (resolved later)
                    return ChartType.BAR 
                
                # If not, fall back to bar

        if cat_cols and num_cols:
            # For bar charts, we might prefer horizontal if labels are long
            avg_label_len = df[cat_cols[0]].astype(str).str.len().mean()
            
            if ((avg_label_len > VIZ_HBAR_LABEL_LEN_THRESHOLD) and (len(df) <= VIZ_HBAR_MAX_CATEGORIES)):
                # signals horizontal bar later
                return ChartType.BAR  

            return ChartType.BAR

        if (len(num_cols) >= 2):
            # Many numeric columns -> heatmap shows relationships better
            if len(num_cols) >= 4:
                return ChartType.HEATMAP
            
            return ChartType.SCATTER

        if len(num_cols) == 1:
            return ChartType.HISTOGRAM

        return ChartType.BAR


    def _is_donut_candidate(self, df: pd.DataFrame, cat_col: str) -> bool:
        """
        Return True if the categorical column is a good donut candidate: 2-8 unique values and the column name suggests a status/type/category field
        """
        n_unique     = df[cat_col].nunique()
        col_lower    = cat_col.lower()
        name_signals = ("status", "type", "category", "gender", "stage", "tier", "method", "plan")

        return ((_DONUT_MIN_CATS <= n_unique <= _DONUT_MAX_CATS) and
                any(s in col_lower for s in name_signals)
               )


    def create_bar_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Bar Chart") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [x_col, y_col]).copy()

            if plot_df.empty:
                return None

            if (len(plot_df) > VIZ_BAR_MAX_CATEGORIES):
                plot_df = plot_df.nlargest(VIZ_BAR_MAX_CATEGORIES, y_col)

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            colors  = _bar_gradient_colors(len(plot_df))

            bars    = ax.bar(plot_df[x_col].astype(str),
                             plot_df[y_col],
                             color     = colors,
                             edgecolor = _PALETTE["bg_figure"],
                             linewidth = 0.5,
                            )

            for bar in bars:
                h = bar.get_height()

                if (h > 0):
                    ax.text(x          = bar.get_x() + bar.get_width() / 2,
                            y          = h * 1.01,
                            s          = _short_number(h),
                            ha         = "center",
                            va         = "bottom",
                            fontsize   = 8,
                            color      = _PALETTE["text_muted"],
                            fontfamily = _FONT_MAIN,
                           )

            _set_title(ax, title)
            ax.set_xlabel(x_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel(y_col.replace("_", " ").title(), fontsize = 10)

            _format_axis_values(ax, "y")

            if (len(plot_df) > VIZ_BAR_XTICK_THRESHOLD):
                plt.xticks(rotation = 40,
                           ha       = "right",
                           fontsize = 8,
                          )

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Bar chart failed", error = str(e))
            return None


    def create_horizontal_bar_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Ranking") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [x_col, y_col]).copy()

            if plot_df.empty:
                return None

            if (len(plot_df) > VIZ_HBAR_MAX_CATEGORIES):
                plot_df = plot_df.nlargest(VIZ_HBAR_MAX_CATEGORIES, y_col)

            plot_df = plot_df.sort_values(by        = y_col,
                                          ascending = True,
                                         )

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            # Highlight top bar with accent, rest with accent_2
            colors = [_PALETTE["accent"] if (i == len(plot_df) - 1) else _PALETTE["accent_2"]
                      for i in range(len(plot_df))]

            ax.barh(plot_df[x_col].astype(str),
                    plot_df[y_col],
                    color     = colors,
                    edgecolor = _PALETTE["bg_figure"],
                    linewidth = 0.4,
                   )

            _format_axis_values(ax, "x")
            _set_title(ax, title)
            ax.set_xlabel(y_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel(x_col.replace("_", " ").title(), fontsize = 10)

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Horizontal bar chart failed", error = str(e))
            return None


    def create_line_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Line Chart") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [x_col, y_col]).copy()

            if plot_df.empty:
                return None

            # Ensure x_col is datetime
            if not pd.api.types.is_datetime64_any_dtype(plot_df[x_col]):
                try:
                    plot_df[x_col] = pd.to_datetime(plot_df[x_col])
                
                except:
                    # If conversion fails, sort anyway
                    pass

            try:
                plot_df = plot_df.sort_values(x_col)
            
            except TypeError:
                pass

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            ax.plot(plot_df[x_col],
                    plot_df[y_col],
                    color           = _PALETTE["accent"],
                    linewidth       = 2.2,
                    marker          = "o",
                    markersize      = 5,
                    markerfacecolor = _PALETTE["bg_axes"],
                    markeredgecolor = _PALETTE["accent"],
                    markeredgewidth = 1.5,
                   )

            ax.fill_between(plot_df[x_col],
                            plot_df[y_col],
                            alpha = 0.08,
                            color = _PALETTE["accent"],
                           )

            _set_title(ax, title)
            ax.set_xlabel(x_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel(y_col.replace("_", " ").title(), fontsize = 10)

            _format_axis_values(ax, "y")

            if (len(plot_df) > VIZ_LINE_XTICK_THRESHOLD):
                plt.xticks(rotation = 35,
                           ha       = "right",
                           fontsize = 8,
                          )

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Line chart failed", error = str(e))
            return None


    def create_multi_line_chart(self, df: pd.DataFrame, x_col: str, y_cols: List[str], title: str = "Trend Comparison") -> Optional[Figure]:
        try:
            if not y_cols:
                return None

            # Cap series count
            y_cols  = y_cols[:_MULTI_LINE_MAX_SERIES]
            valid   = [c for c in y_cols if c in df.columns]

            if not valid:
                return None

            plot_df = df.dropna(subset = [x_col]).copy()

            # Ensure x_col is datetime
            if not pd.api.types.is_datetime64_any_dtype(plot_df[x_col]):
                try:
                    plot_df[x_col] = pd.to_datetime(plot_df[x_col])
                
                except:
                    pass

            try:
                plot_df = plot_df.sort_values(x_col)
            
            except TypeError:
                pass

            fig, ax       = plt.subplots(figsize = self.fig_size,
                                         dpi     = self.dpi,
                                        )

            _apply_bi_theme(fig, ax)

            series_colors = _PALETTE.get("series", [_PALETTE["accent"],
                                                     _PALETTE["accent_2"],
                                                     _PALETTE["accent_3"],
                                                    ])

            for idx, col in enumerate(valid):
                color     = series_colors[idx % len(series_colors)]
                col_data  = pd.to_numeric(plot_df[col], errors = "coerce")
                clean_df  = plot_df[[x_col]].assign(**{col: col_data}).dropna()

                ax.plot(clean_df[x_col],
                        clean_df[col],
                        label           = col.replace("_", " ").title(),
                        color           = color,
                        linewidth       = 2.0,
                        marker          = "o",
                        markersize      = 4,
                        markerfacecolor = _PALETTE["bg_axes"],
                        markeredgecolor = color,
                        markeredgewidth = 1.4,
                       )

                ax.fill_between(clean_df[x_col],
                                clean_df[col],
                                alpha = 0.05,
                                color = color,
                               )

            ax.legend(fontsize   = 8,
                      facecolor  = _PALETTE["bg_axes"],
                      edgecolor  = _PALETTE["grid"],
                      labelcolor = _PALETTE["text_muted"],
                     )

            _set_title(ax, title)
            ax.set_xlabel(x_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel("Value", fontsize = 10)

            _format_axis_values(ax, "y")

            if (len(plot_df) > VIZ_LINE_XTICK_THRESHOLD):
                plt.xticks(rotation = 35,
                           ha       = "right",
                           fontsize = 8,
                          )

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Multi-line chart failed", error = str(e))
            return None


    def create_donut_chart(self, df: pd.DataFrame, cat_col: str, val_col: str, title: str = "Distribution") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [cat_col, val_col]).copy()

            if plot_df.empty:
                return None

            agg     = plot_df.groupby(cat_col, sort = False)[val_col].sum().reset_index()
            agg     = agg.sort_values(val_col, ascending = False)

            if agg.empty:
                return None

            labels  = agg[cat_col].astype(str).tolist()
            values  = agg[val_col].tolist()
            total   = sum(values)

            colors  = _bar_gradient_colors(len(labels))

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            fig.patch.set_facecolor(_PALETTE["bg_figure"])
            ax.set_facecolor(_PALETTE["bg_figure"])

            wedges, texts, autotexts = ax.pie(values,
                                              labels         = labels,
                                              colors         = colors,
                                              autopct        = "%1.1f%%",
                                              startangle     = 90,
                                              pctdistance    = 0.82,
                                              wedgeprops     = {"width"    : 0.55,
                                                                "edgecolor": _PALETTE["bg_figure"],
                                                                "linewidth": 2,
                                                               },
                                             )

            for text in texts:
                text.set_color(_PALETTE["text_muted"])
                text.set_fontsize(8)
                text.set_fontfamily(_FONT_MAIN)

            for autotext in autotexts:
                autotext.set_color(_PALETTE["text_main"])
                autotext.set_fontsize(8)
                autotext.set_fontweight("semibold")
                autotext.set_fontfamily(_FONT_MAIN)

            # Centre label showing total
            ax.text(x          = 0,
                    y          = 0,
                    s          = f"{_short_number(total)}\ntotal",
                    ha         = "center",
                    va         = "center",
                    fontsize   = 11,
                    fontweight = "bold",
                    color      = _PALETTE["text_main"],
                    fontfamily = _FONT_MAIN,
                   )

            _set_title(ax, title)

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Donut chart failed", error = str(e))
            return None


    def create_stacked_bar_chart(self, df: pd.DataFrame, x_col: str, hue_col: str, y_col: str, title: str = "Stacked Comparison") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [x_col, hue_col, y_col]).copy()

            if plot_df.empty:
                return None

            # Pivot to wide format
            pivot = (plot_df.groupby([x_col, hue_col])[y_col].sum().unstack(fill_value = 0))

            if pivot.empty:
                return None

            # Cap to VIZ_BAR_MAX_CATEGORIES rows
            if (len(pivot) > VIZ_BAR_MAX_CATEGORIES):
                # Keep top rows by total
                pivot = pivot.loc[pivot.sum(axis = 1).nlargest(VIZ_BAR_MAX_CATEGORIES).index]

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            colors  = _bar_gradient_colors(len(pivot.columns))
            bottoms = np.zeros(len(pivot))

            for idx, col in enumerate(pivot.columns):
                ax.bar(pivot.index.astype(str),
                       pivot[col],
                       bottom    = bottoms,
                       color     = colors[idx],
                       edgecolor = _PALETTE["bg_figure"],
                       linewidth = 0.4,
                       label     = str(col),
                      )

                bottoms = bottoms + pivot[col].values

            ax.legend(fontsize   = 8,
                      facecolor  = _PALETTE["bg_axes"],
                      edgecolor  = _PALETTE["grid"],
                      labelcolor = _PALETTE["text_muted"],
                     )

            _set_title(ax, title)
            ax.set_xlabel(x_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel(y_col.replace("_", " ").title(), fontsize = 10)

            _format_axis_values(ax, "y")

            if (len(pivot) > VIZ_BAR_XTICK_THRESHOLD):
                plt.xticks(rotation = 40,
                           ha       = "right",
                           fontsize = 8,
                          )

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Stacked bar chart failed", error = str(e))
            return None


    def create_heatmap(self, df: pd.DataFrame, title: str = "Correlation Heatmap") -> Optional[Figure]:
        try:
            num_cols = [c for c in df.select_dtypes(include = ["number"]).columns if not _is_id_column(c)]

            if (len(num_cols) < 2):
                return None

            # Cap at 10 columns to keep cells readable
            num_cols = num_cols[:10]
            corr     = df[num_cols].corr()

            fig_w    = max(self.fig_size[0], len(num_cols) * 0.8)
            fig_h    = max(self.fig_size[1], len(num_cols) * 0.7)

            fig, ax  = plt.subplots(figsize = (fig_w, fig_h),
                                    dpi     = self.dpi,
                                   )

            fig.patch.set_facecolor(_PALETTE["bg_figure"])
            ax.set_facecolor(_PALETTE["bg_axes"])

            cmap     = plt.cm.RdYlGn

            im       = ax.imshow(corr.values,
                                 cmap   = cmap,
                                 vmin   = -1,
                                 vmax   = 1,
                                 aspect = "auto",
                                )

            col_labels = [c.replace("_", " ")[:14] for c in num_cols]

            ax.set_xticks(range(len(num_cols)))
            ax.set_yticks(range(len(num_cols)))
            ax.set_xticklabels(col_labels, rotation = 45, ha = "right",
                               fontsize = 8, color = _PALETTE["text_muted"])
            ax.set_yticklabels(col_labels, fontsize = 8, color = _PALETTE["text_muted"])

            # Annotate each cell
            for i in range(len(num_cols)):
                for j in range(len(num_cols)):
                    val  = corr.iloc[i, j]
                    text = ax.text(j, i,
                                   f"{val:.2f}",
                                   ha        = "center",
                                   va        = "center",
                                   fontsize  = 8,
                                   fontfamily= _FONT_MAIN,
                                   color     = "white" if (abs(val) > 0.6) else _PALETTE["text_muted"],
                                  )

            cbar = fig.colorbar(im, ax = ax, shrink = 0.8, pad = 0.02)
            cbar.ax.tick_params(labelsize = 8, colors = _PALETTE["text_muted"])
            cbar.ax.yaxis.label.set_color(_PALETTE["text_muted"])

            for spine in ax.spines.values():
                spine.set_visible(False)

            _set_title(ax, title)

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Heatmap failed", error = str(e))
            return None


    def create_scatter_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Scatter Plot") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [x_col, y_col]).copy()

            if plot_df.empty:
                return None

            sampled = False

            if (len(plot_df) > _SCATTER_MAX_POINTS):
                plot_df = plot_df.sample(n           = _SCATTER_MAX_POINTS,
                                         random_state = 42,
                                        )
                sampled = True

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            ax.scatter(plot_df[x_col],
                       plot_df[y_col],
                       color     = _PALETTE["accent"],
                       alpha     = 0.65,
                       edgecolor = _PALETTE["bg_figure"],
                       linewidth = 0.4,
                       s         = 50,
                      )

            # Add linear trend line
            try:
                x_vals = pd.to_numeric(plot_df[x_col], errors = "coerce").dropna()
                y_vals = pd.to_numeric(plot_df[y_col], errors = "coerce")
                y_vals = y_vals[x_vals.index].dropna()
                x_vals = x_vals[y_vals.index]

                if (len(x_vals) >= 3):
                    z    = np.polyfit(x_vals, y_vals, 1)
                    p    = np.poly1d(z)
                    xseq = np.linspace(x_vals.min(), x_vals.max(), 100)

                    ax.plot(xseq,
                            p(xseq),
                            color     = _PALETTE["accent_3"],
                            linewidth = 1.5,
                            linestyle = "--",
                            alpha     = 0.7,
                            label     = "Trend",
                           )

                    ax.legend(fontsize   = 8,
                              facecolor  = _PALETTE["bg_axes"],
                              edgecolor  = _PALETTE["grid"],
                              labelcolor = _PALETTE["text_muted"],
                             )

            except Exception:
                pass

            display_title = title[:VIZ_TITLE_MAX_LEN]

            if sampled:
                display_title = f"{display_title} (sample {_SCATTER_MAX_POINTS:,})"

            _set_title(ax, display_title)
            ax.set_xlabel(x_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel(y_col.replace("_", " ").title(), fontsize = 10)

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Scatter chart failed", error = str(e))
            return None


    def create_histogram(self, df: pd.DataFrame, col: str, title: str = "Distribution", bins: int = VIZ_HISTOGRAM_MAX_BINS) -> Optional[Figure]:
        try:
            plot_data = df[col].dropna()

            if plot_data.empty:
                return None

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            n_bins = min(bins, max(5, len(plot_data.unique())))

            ax.hist(plot_data,
                    bins      = n_bins,
                    color     = _PALETTE["accent"],
                    edgecolor = _PALETTE["bg_figure"],
                    linewidth = 0.4,
                    alpha     = 0.85,
                   )

            mean_val   = plot_data.mean()
            median_val = plot_data.median()

            ax.axvline(mean_val,
                       color     = _PALETTE["accent_3"],
                       linewidth = 1.5,
                       linestyle = "--",
                       label     = f"Mean: {_short_number(mean_val)}",
                      )

            ax.axvline(median_val,
                       color     = _PALETTE["accent_2"],
                       linewidth = 1.5,
                       linestyle = ":",
                       label     = f"Median: {_short_number(median_val)}",
                      )

            ax.legend(fontsize   = 8,
                      facecolor  = _PALETTE["bg_axes"],
                      edgecolor  = _PALETTE["grid"],
                      labelcolor = _PALETTE["text_muted"],
                     )

            _set_title(ax, title)
            ax.set_xlabel(col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel("Frequency", fontsize = 10)

            _format_axis_values(ax, "x")

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Histogram failed", error = str(e))
            return None


    def auto_visualize(self, df: pd.DataFrame, title: str = "Data Visualization") -> Optional[Tuple[Figure, str]]:
        """
        Auto-select and render the best chart for the given DataFrame: returns (Figure, chart_type_str) on success, or None if no suitable chart found
        """
        # Global kill switch
        if (not settings.enable_visualization):
            logger.debug("Visualization disabled by settings")
            return None

        # Check if visualization is worthwhile
        if (not self._should_visualize(df)):
            logger.debug("Skipping visualization: data too trivial or uninformative")
            return None

        # Determine chart type
        chart_type = self._detect_chart_type(df)

        # Now map to actual creation method
        num_cols   = [c for c in df.select_dtypes(include=["number"]).columns if not _is_id_column(c)]
        cat_cols   = [c for c in df.select_dtypes(include=["object", "category", "string"]).columns]
        dt_cols    = [c for c in df.columns if _is_date_column(df[c])]

        # LINE / MULTI_LINE
        if dt_cols and num_cols:
            x_col = dt_cols[0]
            
            if (len(num_cols) >= 2):
                fig = self.create_multi_line_chart(df, x_col, num_cols, title)
                
                if fig:
                    return (fig, "multi_line")
            
            # fallback to single line
            fig = self.create_line_chart(df, x_col, num_cols[0], title)
            
            return (fig, "line") if fig else None

        # DONUT
        if cat_cols and num_cols and self._is_donut_candidate(df, cat_cols[0]):
            fig = self.create_donut_chart(df, cat_cols[0], num_cols[0], title)
            
            if fig:
                return (fig, "donut")

        # STACKED BAR
        if ((len(cat_cols) >= 2) and num_cols):
            fig = self.create_stacked_bar_chart(df, cat_cols[0], cat_cols[1], num_cols[0], title)
            
            if fig:
                return (fig, "stacked")

        # HORIZONTAL BAR
        if cat_cols and num_cols:
            avg_label_len = df[cat_cols[0]].astype(str).str.len().mean()
            
            if avg_label_len > VIZ_HBAR_LABEL_LEN_THRESHOLD and len(df) <= VIZ_HBAR_MAX_CATEGORIES:
                fig = self.create_horizontal_bar_chart(df, cat_cols[0], num_cols[0], title)
                
                if fig:
                    return (fig, "hbar")

        # BAR (vertical)
        if cat_cols and num_cols:
            fig = self.create_bar_chart(df, cat_cols[0], num_cols[0], title)
            
            if fig:
                return (fig, "bar")

        # HEATMAP
        if (len(num_cols) >= 4):
            fig = self.create_heatmap(df, title)
            
            if fig:
                return (fig, "heatmap")

        # SCATTER
        if (len(num_cols) >= 2):
            fig = self.create_scatter_chart(df, num_cols[0], num_cols[1], title)
            
            if fig:
                return (fig, "scatter")

        # HISTOGRAM
        if (len(num_cols) == 1):
            fig = self.create_histogram(df, num_cols[0], title)
            
            if fig:
                return (fig, "histogram")

        # Fallback
        if ((len(df.columns) >= 2) and num_cols):
            # Use first column (could be categorical) and first numeric
            x_col = df.columns[0]
            y_col = num_cols[0]
            fig   = self.create_bar_chart(df, x_col, y_col, title)
           
            return (fig, "bar") if fig else None

        logger.warning("Could not determine suitable chart type",
                       columns = list(df.columns),
                      )

        return None


    def figure_to_base64(self, fig: Figure) -> str:
        """
        Encode figure as a data-URI base64 PNG: always closes the figure in the finally block to prevent memory leaks
        """
        if not fig:
            return ""

        try:
            buf = io.BytesIO()

            fig.savefig(fname       = buf,
                        format      = "png",
                        bbox_inches = "tight",
                        dpi         = self.dpi,
                        facecolor   = _PALETTE["bg_figure"],
                       )

            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode("utf-8")

            return f"data:image/png;base64,{b64}"

        finally:
            plt.close(fig)


    def figure_to_png_bytes(self, fig: Figure) -> bytes:
        """
        Return raw PNG bytes — useful for direct HTTP responses
        """
        if not fig:
            return b""

        try:
            buf = io.BytesIO()

            fig.savefig(fname       = buf,
                        format      = "png",
                        bbox_inches = "tight",
                        dpi         = self.dpi,
                        facecolor   = _PALETTE["bg_figure"],
                       )

            buf.seek(0)

            return buf.read()

        finally:
            plt.close(fig)


    def save_figure(self, fig: Figure, filepath: str) -> None:
        if not fig:
            return

        try:
            fig.savefig(fname       = filepath,
                        bbox_inches = "tight",
                        dpi         = self.dpi,
                        facecolor   = _PALETTE["bg_figure"],
                       )

            logger.info("Visualization saved", filepath = filepath)

        finally:
            plt.close(fig)


# GLOBAL INSTANCE
viz_generator = VisualizationGenerator()