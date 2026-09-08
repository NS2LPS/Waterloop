"""Standalone oxygen display: run with ``python o2_level_app.py``.

Uses the existing WATERLOOP_DB_* settings and .env file. Historical data is
written by sensor_poll.py and the monitor API; this app only reads it.
Optional settings: WATERLOOP_O2_POLL_PERIOD_SECONDS (10),
WATERLOOP_O2_PLOT_PERIOD_SECONDS (30), WATERLOOP_O2_PORT (8081).
"""

import logging
import math
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import mysql.connector
import plotly.graph_objects as go
from nicegui import run, ui
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sensor_poll import read_o2_helium


LOCAL_TZ = ZoneInfo("Europe/Paris")
SENSOR_NAME = "o2_helium"
MAX_PLOT_POINTS = 2000
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Use the same database defaults and environment precedence as waterloop_app."""

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "waterloop"
    db_password: str = ""
    db_name: str = "waterloop"
    o2_poll_period_seconds: float = Field(default=10, gt=0)
    o2_plot_period_seconds: float = Field(default=30, gt=0)
    o2_port: int = Field(default=8081, ge=1, le=65535)

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().with_name(".env"),
        env_prefix="WATERLOOP_",
        extra="ignore",
    )


settings = Settings()


def history_bucket_seconds(start_timestamp: int, end_timestamp: int) -> int:
    """Choose SQL averaging buckets, with a strict limit including missing points."""
    if end_timestamp < start_timestamp:
        raise ValueError("The end time must be after the start time.")
    required = math.ceil((end_timestamp - start_timestamp + 1) / MAX_PLOT_POINTS)
    for seconds in (60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800):
        if seconds >= required:
            return seconds
    return required


def read_history(start_timestamp: int, end_timestamp: int) -> tuple[list, list, int]:
    """Combine live/archive rows and aggregate in MySQL before transferring data."""
    bucket_seconds = history_bucket_seconds(start_timestamp, end_timestamp)
    with closing(mysql.connector.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        connection_timeout=5,
    )) as connection:
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                SELECT FLOOR((`timestamp` - %s) / %s) AS bucket,
                       AVG(CAST(value AS DECIMAL(20, 6))) AS value
                FROM (
                    SELECT `timestamp`, value FROM monitored_data
                    WHERE sensor = %s AND `timestamp` BETWEEN %s AND %s
                    UNION ALL
                    SELECT `timestamp`, value FROM monitored_data_archive
                    WHERE sensor = %s AND `timestamp` BETWEEN %s AND %s
                ) AS rows_for_plot
                GROUP BY bucket
                ORDER BY bucket
                """,
                (start_timestamp, bucket_seconds,
                 SENSOR_NAME, start_timestamp, end_timestamp,
                 SENSOR_NAME, start_timestamp, end_timestamp),
            )
            rows = cursor.fetchall()

    values = {}
    for bucket, raw_value in rows:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values[int(bucket)] = value

    # Empty buckets break the line so a sensor outage is not hidden.
    count = (end_timestamp - start_timestamp) // bucket_seconds + 1
    timestamps = [
        datetime.fromtimestamp(start_timestamp + index * bucket_seconds, tz=LOCAL_TZ)
        for index in range(count)
    ]
    return timestamps, [values.get(index) for index in range(count)], bucket_seconds


def make_figure(timestamps: list, values: list, start: int, end: int) -> go.Figure:
    figure = go.Figure(go.Scatter(
        x=timestamps, y=values, mode="lines+markers", connectgaps=False,
        line={"color": "#0891b2", "width": 2}, marker={"size": 3},
        hovertemplate="%{x|%d %b %Y %H:%M}<br>%{y:.2f} ppm<extra></extra>",
    ))
    figure.update_layout(
        template="plotly_white", height=400,
        margin={"l": 65, "r": 20, "t": 20, "b": 60}, showlegend=False,
        xaxis={
            "title": "Time (Europe/Paris)", "tickformat": "%d %b<br>%H:%M",
            "range": [datetime.fromtimestamp(start, tz=LOCAL_TZ),
                      datetime.fromtimestamp(end, tz=LOCAL_TZ)],
        },
        yaxis={"title": "Oxygen level (ppm)", "rangemode": "tozero"},
    )
    return figure


async def update_history(plot, status, start: int, end: int) -> None:
    status.set_text("Loading readings…")
    try:
        timestamps, values, bucket = await run.io_bound(read_history, start, end)
    except Exception:
        logger.exception("Could not read oxygen history")
        status.set_text("Unable to load database readings. Any displayed plot is from the previous refresh.")
        return
    plot.figure = make_figure(timestamps, values, start, end)
    plot.update()
    if not any(value is not None for value in values):
        status.set_text("No oxygen readings stored for this time span.")
    else:
        status.set_text(
            f"Updated {datetime.now(LOCAL_TZ):%H:%M:%S}"
        )


@ui.page("/")
def main_page() -> None:
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-6"):
        ui.label("Oxygen level in the helium line").classes("text-3xl font-semibold")
        with ui.card().classes("w-full items-center p-8"):
            ui.label("LIVE OXYGEN LEVEL").classes("text-sm text-slate-500 tracking-widest")
            with ui.row().classes("items-baseline justify-center gap-3"):
                value_label = ui.label("—").style(
                    "font-size: clamp(64px, 12vw, 120px); font-weight: 700; "
                    "line-height: 1.2; color: #0891b2; font-variant-numeric: tabular-nums"
                )
                ui.label("ppm").classes("text-3xl text-slate-500")
            live_status = ui.label("Waiting for the first reading…").classes("text-slate-500")
            ui.label(f"Refreshes every {settings.o2_poll_period_seconds:g} seconds").classes(
                "text-sm text-slate-400"
            )

        with ui.card().classes("w-full p-4"):
            ui.label("Last 24 hours").classes("text-xl font-semibold")
            now = int(datetime.now(LOCAL_TZ).timestamp())
            plot = ui.plotly(make_figure([], [], now - 86400, now)).classes("w-full")
            history_status = ui.label("Loading readings…").classes("text-sm text-slate-500")
        ui.button("Archive", icon="history", on_click=lambda: ui.navigate.to("/archive"))

    async def refresh_live() -> None:
        try:
            readings = await run.io_bound(read_o2_helium)
            value = float(readings[SENSOR_NAME])
            if not math.isfinite(value):
                raise ValueError("Non-finite oxygen value")
        except Exception:
            logger.exception("Could not read live oxygen level")
            value_label.set_text("—")
            live_status.set_text("Reading unavailable · retrying automatically")
            return
        value_label.set_text(f"{value:.1f}")
        live_status.set_text(f"Last successful read: {datetime.now(LOCAL_TZ):%d %b %Y %H:%M:%S}")

    async def refresh_history() -> None:
        end = int(datetime.now(LOCAL_TZ).timestamp())
        await update_history(plot, history_status, end - 86400, end)

    # Async callbacks move HTTP and MySQL work off the UI event loop.
    ui.timer(settings.o2_poll_period_seconds, refresh_live, immediate=True)
    ui.timer(settings.o2_plot_period_seconds, refresh_history, immediate=True)


def parse_archive_window(start_text: str, end_text: str) -> tuple[int, int]:
    """Interpret the chosen local times and prevent inverted or future-only ranges."""
    start = datetime.fromisoformat(start_text).replace(tzinfo=LOCAL_TZ)
    end = datetime.fromisoformat(end_text).replace(tzinfo=LOCAL_TZ)
    end = min(end, datetime.now(LOCAL_TZ))
    if start >= end:
        raise ValueError("Choose a start time before the end time and before now.")
    return int(start.timestamp()), int(end.timestamp())


@ui.page("/archive")
def archive_page() -> None:
    now = datetime.now(LOCAL_TZ).replace(second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        ui.button("Live display", icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props("flat")
        ui.label("Oxygen history").classes("text-3xl font-semibold")
        ui.label("Oxygen level in the helium line · ppm").classes("text-slate-500")
        with ui.row().classes("items-end gap-4"):
            start_input = ui.input("From (Europe/Paris)", value=week_ago.isoformat(timespec="minutes")[:16]).props("type=datetime-local")
            end_input = ui.input("To (Europe/Paris)", value=now.isoformat(timespec="minutes")[:16]).props("type=datetime-local")
            show_button = ui.button("Show data")
        plot = ui.plotly(make_figure([], [], int(week_ago.timestamp()), int(now.timestamp()))).classes("w-full")
        status = ui.label("Loading readings…").classes("text-sm text-slate-500")
        ui.label("Longer time spans use wider averages, up to 2,000 plotted points.").classes("text-sm text-slate-400")

    async def refresh() -> None:
        try:
            start, end = parse_archive_window(str(start_input.value), str(end_input.value))
        except (TypeError, ValueError) as exc:
            ui.notify(f"Invalid time span: {exc}", type="negative")
            return
        show_button.disable()
        try:
            await update_history(plot, status, start, end)
        finally:
            show_button.enable()

    show_button.on_click(refresh)
    ui.timer(0.1, refresh, once=True)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="0.0.0.0", port=settings.o2_port, title="Helium Oxygen Monitor", reload=False)
