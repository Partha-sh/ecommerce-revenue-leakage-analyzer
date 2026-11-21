"""Consistent Plotly figures used by dashboard pages."""

from __future__ import annotations

from typing import Sequence

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

COLORS = ["#44D7B6", "#78A9FF", "#F4B860", "#F07C9C", "#A78BFA", "#4CC9F0"]
LAYOUT = {
    "template": "plotly_dark",
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": "Inter, Segoe UI, sans-serif", "color": "#E7ECF3"},
    "margin": {"l": 24, "r": 24, "t": 48, "b": 24},
    "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
}


def empty_figure(message: str = "No records for this filter") -> go.Figure:
    figure = go.Figure()
    figure.update_layout(**LAYOUT, height=320)
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper",
                          showarrow=False, font={"size": 14, "color": "#98A4B5"})
    return figure


def line_chart(frame: pd.DataFrame, x: str, y_columns: Sequence[str], labels: dict[str, str],
               *, title: str, currency: bool = True) -> go.Figure:
    if frame.empty or x not in frame:
        return empty_figure()
    available = [column for column in y_columns if column in frame and frame[column].notna().any()]
    if not available:
        return empty_figure("No complete values for this filter")
    long = frame.melt(id_vars=[x], value_vars=available, var_name="metric", value_name="value")
    figure = px.line(long, x=x, y="value", color="metric", markers=True,
                     color_discrete_sequence=COLORS, labels={"value": "₹ Amount" if currency else "Value", "metric": ""},
                     title=title)
    figure.update_layout(**LAYOUT, height=360, yaxis_tickprefix="₹" if currency else "", yaxis_tickformat="~s")
    figure.update_traces(line={"width": 3})
    figure.for_each_trace(lambda trace: trace.update(name=labels.get(trace.name, trace.name)))
    return figure


def bar_chart(frame: pd.DataFrame, x: str, y: str, *, title: str,
              color: str | None = None, horizontal: bool = False,
              currency: bool = True, text: str | None = None) -> go.Figure:
    if frame.empty or x not in frame or y not in frame:
        return empty_figure()
    values = frame[y].notna()
    if not values.any():
        return empty_figure("No complete values for this filter")
    figure = px.bar(frame.loc[values], x=y if horizontal else x, y=x if horizontal else y,
                    color=color, text=text, title=title, color_discrete_sequence=COLORS,
                    orientation="h" if horizontal else "v")
    figure.update_layout(**LAYOUT, height=360,
                         xaxis_tickprefix="₹" if currency and not horizontal else "",
                         yaxis_tickprefix="₹" if currency and horizontal else "",
                         xaxis_tickformat="~s", yaxis_tickformat="~s")
    if horizontal:
        figure.update_layout(yaxis={"categoryorder": "total ascending"})
    return figure


def grouped_bar(frame: pd.DataFrame, x: str, y_columns: Sequence[str], labels: dict[str, str],
                *, title: str) -> go.Figure:
    if frame.empty or x not in frame:
        return empty_figure()
    available = [column for column in y_columns if column in frame]
    if not available:
        return empty_figure()
    long = frame.melt(id_vars=[x], value_vars=available, var_name="metric", value_name="value")
    figure = px.bar(long, x=x, y="value", color="metric", barmode="group",
                    color_discrete_sequence=COLORS, title=title,
                    labels={"value": "₹ Amount", "metric": ""})
    figure.update_layout(**LAYOUT, height=360, yaxis_tickprefix="₹", yaxis_tickformat="~s")
    figure.for_each_trace(lambda trace: trace.update(name=labels.get(trace.name, trace.name)))
    return figure


def pie_chart(frame: pd.DataFrame, names: str, values: str, *, title: str) -> go.Figure:
    if frame.empty or names not in frame or values not in frame:
        return empty_figure()
    clean = frame.loc[frame[values].notna() & frame[values].gt(0)]
    if clean.empty:
        return empty_figure()
    figure = px.pie(clean, names=names, values=values, hole=0.68, title=title,
                    color_discrete_sequence=COLORS)
    figure.update_layout(**LAYOUT, height=340)
    return figure
