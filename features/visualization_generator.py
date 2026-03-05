# DEPENDENCIES
import io
import base64
import structlog
import matplotlib
import pandas as pd

matplotlib.use("Agg")   

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.figure import Figure
from config.settings import settings
from config.constants import ChartType
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
logger     = structlog.get_logger()


_PALETTE                  = VIZ_PALETTE
_FONT_MAIN                = VIZ_FONT_MAIN

# Max points rendered in a scatter plot before sampling
_SCATTER_MAX_POINTS : int = 2000


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


# Theme helpers
def _apply_bi_theme(fig: Figure, ax) -> None:
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


def _format_axis_values(ax, axis: str = "y") -> None:
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


# Visualization Generator
class VisualizationGenerator:
    """
    Generate professional dark-theme BI charts: all public methods return Optional[Figure] and never raise
    """
    def __init__(self):
        self.fig_size = (settings.viz_figure_width, settings.viz_figure_height)
        self.dpi      = settings.viz_dpi


    def _detect_chart_type(self, df: pd.DataFrame) -> ChartType:
        """
        Select the most appropriate chart type based on column types: priority: datetime → bar(cat+num) → scatter(2+num) → histogram(1 num)
        """
        has_datetime = any(pd.api.types.is_datetime64_any_dtype(df[c]) for c in df.columns)

        if has_datetime:
            return ChartType.LINE

        num_cols     = df.select_dtypes(include = ["number"]).columns
        cat_cols     = df.select_dtypes(include = ["object", "category", "string"]).columns

        if ((len(cat_cols) > 0) and (len(num_cols) > 0)):
            return ChartType.BAR

        if (len(num_cols) >= 2):
            return ChartType.SCATTER

        if (len(num_cols) == 1):
            return ChartType.HISTOGRAM

        return ChartType.BAR


    def create_bar_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Bar Chart") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset = [x_col, y_col]).copy()

            if plot_df.empty:
                return None

            if (len(plot_df) > 20):
                plot_df = plot_df.nlargest(VIZ_BAR_MAX_CATEGORIES, y_col)

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            bars = ax.bar(plot_df[x_col].astype(str),
                          plot_df[y_col],
                          color     = _PALETTE["series"],
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

            ax.set_title(label      = title[:VIZ_TITLE_MAX_LEN],
                         fontsize   = 13,
                         fontweight = "semibold",
                         fontfamily = _FONT_MAIN,
                         pad        = 12,
                        )

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


    def create_line_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Line Chart") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset=[x_col, y_col]).copy()

            if plot_df.empty:
                return None

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

            ax.set_title(label      = title[:VIZ_TITLE_MAX_LEN],
                         fontsize   = 13,
                         fontweight = "semibold",
                         fontfamily = _FONT_MAIN,
                         pad        = 12,
                        )

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


    def create_scatter_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Scatter Plot") -> Optional[Figure]:
        """
        Note: Scatter plots with thousands of points become overplotted and slow: if the DataFrame exceeds _SCATTER_MAX_POINTS (2000), a random sample is drawn
        and a note is appended to the title so the user knows they're seeing a sample
        """
        try:
            plot_df = df.dropna(subset=[x_col, y_col]).copy()

            if plot_df.empty:
                return None

            sampled = False

            if (len(plot_df) > _SCATTER_MAX_POINTS):
                plot_df = plot_df.sample(n       = _SCATTER_MAX_POINTS,
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
                       alpha     = 0.7,
                       edgecolor = _PALETTE["bg_figure"],
                       linewidth = 0.4,
                       s         = 50,
                      )

            display_title = title[:VIZ_TITLE_MAX_LEN]

            if sampled:
                display_title = f"{display_title} (sample {_SCATTER_MAX_POINTS:,})"

            ax.set_title(label      = display_title,
                         fontsize   = 13,
                         fontweight = "semibold",
                         fontfamily = _FONT_MAIN,
                         pad        = 12,
                        )

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

            ax.set_title(label      = title[:VIZ_TITLE_MAX_LEN],
                         fontsize   = 13,
                         fontweight = "semibold",
                         fontfamily = _FONT_MAIN,
                         pad        = 12,
                        )

            ax.set_xlabel(col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel("Frequency", fontsize = 10)
            _format_axis_values(ax, "x")

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Histogram failed", error = str(e))
            return None


    def create_horizontal_bar_chart(self, df: pd.DataFrame, x_col: str, y_col: str, title: str = "Ranking") -> Optional[Figure]:
        try:
            plot_df = df.dropna(subset=[x_col, y_col]).copy()

            if plot_df.empty:
                return None

            if (len(plot_df) > 15):
                plot_df = plot_df.nlargest(VIZ_HBAR_MAX_CATEGORIES, y_col)

            plot_df = plot_df.sort_values(by        = y_col,
                                          ascending = True,
                                         )

            fig, ax = plt.subplots(figsize = self.fig_size,
                                   dpi     = self.dpi,
                                  )

            _apply_bi_theme(fig, ax)

            colors = [_PALETTE["accent"] if (i == len(plot_df) - 1) else _PALETTE["accent_2"]
                      for i in range(len(plot_df))]

            ax.barh(plot_df[x_col].astype(str),
                    plot_df[y_col],
                    color     = colors,
                    edgecolor = _PALETTE["bg_figure"],
                    linewidth = 0.4,
                   )

            _format_axis_values(ax, "x")

            ax.set_title(label      = title[:VIZ_TITLE_MAX_LEN],
                         fontsize   = 13,
                         fontweight = "semibold",
                         fontfamily = _FONT_MAIN,
                         pad        = 12,
                        )

            ax.set_xlabel(y_col.replace("_", " ").title(), fontsize = 10)
            ax.set_ylabel(x_col.replace("_", " ").title(), fontsize = 10)

            plt.tight_layout()

            return fig

        except Exception as e:
            logger.error("Horizontal bar chart failed", error = str(e))
            return None


    def auto_visualize(self, df: pd.DataFrame, title: str = "Data Visualization") -> Optional[Figure]:
        if (df is None or df.empty):
            logger.warning("Empty DataFrame — skipping visualization")
            return None

        chart_type = self._detect_chart_type(df)
        num_cols   = df.select_dtypes(include=["number"]).columns.tolist()
        cat_cols   = df.select_dtypes(include=["object", "category", "string"]).columns.tolist()
        dt_cols    = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]

        if (chart_type == ChartType.LINE):
            x_col = dt_cols[0] if dt_cols else df.columns[0]
            y_col = num_cols[0] if num_cols else None

            if y_col:
                return self.create_line_chart(df    = df,
                                              x_col = x_col,
                                              y_col = y_col,
                                              title = title,
                                             )

        if ((chart_type == ChartType.BAR) and cat_cols and num_cols):
            avg_label_len = sum(len(str(v)) for v in df[cat_cols[0]]) / max(1, len(df))

            if ((avg_label_len > VIZ_HBAR_LABEL_LEN_THRESHOLD) and (len(df) <= 15)):
                return self.create_horizontal_bar_chart(df    = df,
                                                        x_col = cat_cols[0],
                                                        y_col = num_cols[0],
                                                        title = title,
                                                       )

            return self.create_bar_chart(df    = df,
                                         x_col = cat_cols[0],
                                         y_col = num_cols[0],
                                         title = title,
                                        )

        if ((chart_type == ChartType.SCATTER) and (len(num_cols) >= 2)):
            return self.create_scatter_chart(df    = df,
                                             x_col = num_cols[0],
                                             y_col = num_cols[1],
                                             title = title,
                                            )

        if ((chart_type == ChartType.HISTOGRAM) and num_cols):
            return self.create_histogram(df    = df,
                                         col   = num_cols[0],
                                         title = title,
                                        )

        if ((len(df.columns) >= 2) and num_cols):
            return self.create_bar_chart(df    = df,
                                         x_col = df.columns[0],
                                         y_col = num_cols[0],
                                         title = title,
                                        )

        logger.warning("Could not determine suitable chart type",
                       columns = list(df.columns),
                      )
        return None


    def figure_to_base64(self, fig: Figure) -> str:
        """
        Encode figure as a data-URI base64 PNG: always closes the figure in finally block to prevent memory leaks
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
        Return raw PNG bytes (useful for direct HTTP responses)
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