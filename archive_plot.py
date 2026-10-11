"""Bounded archive reads and latest-request-only background loading."""

import asyncio
import logging
import math
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import background_tasks, run
from plotly.io import templates

MAX_ARCHIVE_PLOT_POINTS = 2000  # Includes separators for missing data.
MAX_ARCHIVE_PLOTS = 8
RAW_INTERVAL_SECONDS = 60
QUERY_TIMEOUT_MS = 15_000
LOCAL_TZ = ZoneInfo("Europe/Paris")
logger = logging.getLogger(__name__)
# Shared across clients. Archive reads use separate connections, leaving the
# ingestion/dashboard connection pool available while long reads are running.
_archive_slots = asyncio.Semaphore(2)


@dataclass(frozen=True)
class ArchiveRequest:
    sensor: str
    start: int
    end: int

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("The end time must be after the start time.")


def archive_bucket_seconds(start: int, end: int, *, force: bool = False) -> int:
    """Reserve space for gap separators and bound even unusually dense data."""
    if end < start:
        raise ValueError("The end time must be after the start time.")
    # One separator between adjacent samples can almost double the trace size.
    sample_limit = MAX_ARCHIVE_PLOT_POINTS // 2
    span = end - start + 1
    if not force and span <= sample_limit * RAW_INTERVAL_SECONDS:
        return 0
    required = max(1, math.ceil(span / sample_limit))
    for seconds in (60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800):
        if seconds >= required:
            return seconds
    return required


def fill_missing_archive_points(points, expected_delta):
    """One null breaks a Plotly line, regardless of the duration of an outage."""
    filled = []
    for timestamp, value in sorted(points):
        if filled and timestamp - filled[-1][0] > 2 * expected_delta:
            filled.append((filled[-1][0] + expected_delta, None))
        filled.append((timestamp, value))
    return filled


def read_archive_points(connection_factory, request, sensor):
    """Read raw samples when small, otherwise average in SQL before transfer.

    Each branch reduces its own table before UNION ALL, avoiding materializing
    all raw rows from a large time window. SUM/COUNT preserves weighted averages
    across the live/archive boundary. Buckets are anchored at the window start.
    """
    bucket = archive_bucket_seconds(request.start, request.end)
    sample_limit = MAX_ARCHIVE_PLOT_POINTS // 2
    with closing(connection_factory()) as connection:
        with closing(connection.cursor()) as cursor:
            if bucket == 0:
                branches = []
                params = []
                for table in ("monitored_data_archive", "monitored_data"):
                    branches.append(f"""
                        (SELECT `timestamp`, value FROM {table}
                         WHERE sensor = %s AND `timestamp` BETWEEN %s AND %s
                         ORDER BY `timestamp` LIMIT %s)
                    """)
                    params.extend((request.sensor, request.start, request.end, sample_limit + 1))
                cursor.execute(
                    f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */ `timestamp`, value "
                    f"FROM ({' UNION ALL '.join(branches)}) AS samples "
                    "ORDER BY `timestamp` LIMIT %s",
                    (*params, sample_limit + 1),
                )
                rows = cursor.fetchall()
                if len(rows) > sample_limit:
                    # A short window can contain much more than one row/minute.
                    # Retry with aggregation; never silently truncate the plot.
                    bucket = archive_bucket_seconds(request.start, request.end, force=True)
            if bucket:
                branches = []
                params = []
                for table in ("monitored_data_archive", "monitored_data"):
                    branches.append(f"""
                        SELECT FLOOR((`timestamp` - %s) / %s) AS bucket,
                               SUM(CAST(value AS DECIMAL(20, 6))) AS total,
                               COUNT(*) AS n
                        FROM {table}
                        WHERE sensor = %s AND `timestamp` BETWEEN %s AND %s
                        GROUP BY bucket
                    """)
                    params.extend((request.start, bucket, request.sensor, request.start, request.end))
                cursor.execute(
                    f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */ bucket, "
                    "SUM(total) / SUM(n) AS value "
                    f"FROM ({' UNION ALL '.join(branches)}) AS samples "
                    "GROUP BY bucket ORDER BY bucket",
                    tuple(params),
                )
                rows = cursor.fetchall()

    points = []
    for timestamp, raw_value in rows:
        try:
            # Aggregated state/int sensors can have fractional means. Preserve
            # them instead of passing averages through an integer parser.
            value = float(raw_value) if bucket else float(sensor.value(raw_value))
            if not math.isfinite(value):
                value = None
        except (TypeError, ValueError, OverflowError):
            value = None
        timestamp = request.start + int(timestamp) * bucket if bucket else int(timestamp)
        points.append((timestamp, value))
    points = fill_missing_archive_points(points, bucket or RAW_INTERVAL_SECONDS)
    if len(points) > MAX_ARCHIVE_PLOT_POINTS:
        raise RuntimeError("Archive query exceeded the plot point budget")
    return points, bucket


def load_archive_plot(connection_factory, request, sensor):
    """Run the complete data/figure preparation in a worker, without UI access."""
    started = time.monotonic()
    try:
        points, bucket = read_archive_points(connection_factory, request, sensor)
        description = sensor.description("en")
        unit = str(getattr(sensor, "__unit__", "") or "")
        figure = {
            "data": [{
                "type": "scatter", "mode": "lines", "connectgaps": False,
                "x": [datetime.fromtimestamp(t, LOCAL_TZ).isoformat() for t, _ in points],
                "y": [v for _, v in points],
                "hovertemplate": "%{x|%Y-%m-%d %H:%M:%S}<br>%{y}<extra></extra>",
            }],
            "layout": {
                "title": {"text": description},
                "xaxis": {
                    "title": {"text": "Time"}, "type": "date",
                    "tickformat": "%Y-%m-%d<br>%H:%M",
                    "hoverformat": "%Y-%m-%d %H:%M:%S",
                    "range": [datetime.fromtimestamp(t, LOCAL_TZ).isoformat()
                              for t in (request.start, request.end)],
                },
                "yaxis": {"title": {"text": f"{description} ({unit})" if unit else description}},
                "margin": {"l": 60, "r": 20, "t": 50, "b": 50},
                "showlegend": False, "height": 360,
                "template": templates["plotly_white"].to_plotly_json(),
            },
        }
        status = "No readings for this time span."
        if any(value is not None for _, value in points):
            status = f"Averaged in {bucket:,}-second intervals. Zoom in for more detail." if bucket else "Full-resolution readings."
        logger.info(
            "Archive plot sensor=%s start=%s end=%s bucket_seconds=%s points=%s elapsed_seconds=%.3f",
            request.sensor, request.start, request.end, bucket, len(points), time.monotonic() - started,
        )
        return figure, status
    except Exception:
        logger.exception(
            "Archive plot failed sensor=%s start=%s end=%s elapsed_seconds=%.3f",
            request.sensor, request.start, request.end, time.monotonic() - started,
        )
        raise


class LatestArchiveLoader:
    """Coalesce refreshes; running work finishes but only the newest result wins.

    Do not cancel running threads when a plot is removed: cancellation would
    release the concurrency slot while its SQL query was still running.
    """

    def __init__(self, load, on_result, on_error):
        self.load = load
        self.on_result = on_result
        self.on_error = on_error
        self.pending = None
        self.revision = 0
        self.closed = False
        self.task = None

    def submit(self, request):
        if self.closed:
            return
        self.revision += 1
        self.pending = request
        if self.task is None:
            self.task = background_tasks.create(self._drain(), name="archive-plot")

    def invalidate(self):
        self.revision += 1
        self.pending = None

    def close(self):
        self.closed = True
        self.invalidate()

    async def _drain(self):
        try:
            while self.pending is not None and not self.closed:
                async with _archive_slots:
                    if self.closed or self.pending is None:
                        return
                    request, self.pending = self.pending, None
                    revision = self.revision
                    try:
                        result = await run.io_bound(self.load, request)
                    except Exception as exc:
                        if not self.closed and revision == self.revision:
                            self.on_error(exc)
                    else:
                        if result is not None and not self.closed and revision == self.revision:
                            self.on_result(result)
        finally:
            self.task = None
