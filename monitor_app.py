from datetime import datetime
import sqlite3
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError
from zoneinfo import ZoneInfo
from typing import Any, Optional
import threading
from contextlib import contextmanager
from collections.abc import Iterator
import numpy as np

import plotly.graph_objects as go
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from nicegui import ui, app

# Sensors and signals
from sensors import SIGNAL_TABLE

# Translation table
from languages import translate

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATABASE_FILE = BASE_DIR / "water_loop.db"
LOCAL_TZ = ZoneInfo("Europe/Paris")

# Optional API token.
#
# If API_TOKEN is None, the POST endpoint accepts requests without a token.
# If API_TOKEN is a string, clients must send:
#
#   X-API-Token: your-token-here
#
API_TOKEN: Optional[str] = None
# API_TOKEN = "change-this-token"


# Refresh periods in seconds.
#
# LEDs can refresh more frequently because they are light.
# Plots refresh less frequently to avoid unnecessary browser work.
LED_REFRESH_PERIOD_SECONDS = 5
PLOT_REFRESH_PERIOD_SECONDS = 30
# If the latest value is older than this, the LED becomes orange.
STALE_AFTER_SECONDS = 5 * 60

# Signals and allowed operating ranges for LED indicators
#
# Green LED: value is within range.
# Orange LED: sensor is stalled (no data for along time)
# Red LED: value is outside range.
# Grey LED: no data or no range configured.
STATUS_LEDS = [
    "secondary_temperature_1",
    "primary_temperature_1",
    "primary_pressure_1",
    "secondary_flow_1",
    "pmp07_state",
]
SCHEME_STATUS_COLORS = {
    "ok": "#16a34a",        # green
    "alarm": "#dc2626",     # red
    "stale": "#f97316",     # orange
    "no_data": "#9ca3af",   # grey
    "no_range": "#000000",   # black
}

# Plots
#
# The graph title is taken from the "title" field below.
# The signal names are the internal database names.
# The trace labels shown in the legend are taken from the optional "legend" field below.
#
# Each entry creates one Plotly graph.
# The "signals" list can contain one or several signals.
DEFAULT_TIMESPAN_HOURS = 12
PLOTS = [
    {
        "title": {"en": "Loop Temperatures", "fr": "Températures des circuits"},
        "xlabel": {"en": "Time", "fr": "Temps"},
        "ylabel": {"en": "Temperature (°C)", "fr": "Température (°C)"},
        "signals": ["primary_temperature_1", "secondary_temperature_1"],
        "legend" : [{"en":"Primary","fr":"Primaire"},{"en":"Secondary","fr":"Secondaire"}]
    },
    {
        "title": {"en": "Primary Pressure", "fr": "Pression primaire"},
        "xlabel": {"en": "Time", "fr": "Temps"},
        "ylabel": {"en": "Pressure (bar)", "fr": "Pression (bar)"},
        "signals": ["primary_pressure_1"],
    },
    {
        "title": {"en": "Secondary Flow", "fr": "Débit secondaire"},
        "xlabel": {"en": "Time", "fr": "Temps"},
        "ylabel": {"en": "Flow rate (L/min)", "fr": "Débit (L/min)"},
        "signals": ["secondary_flow_1"],
    },
]

# ntfy notification settings.
#
# Set NTFY_TOPIC to your phone subscription topic to enable alerts.
# Leave it as None to disable notifications.
#
# Example:
#   NTFY_TOPIC = "water-loop-lab-alerts-8f4a92"
#
# With this configuration, notifications are sent to:
#   https://ntfy.sh/water-loop-lab-alerts-8f4a92
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC: Optional[str] = None
#NTFY_TOPIC = "lps-waterloop-8f4a92"

# Notification priority used by ntfy.
# Common values: "default", "high", "urgent"
NTFY_PRIORITY = "urgent"



# ---------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------
@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_FILE, timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    finally:
        connection.close()

def create_tables_if_needed() -> None:
    """Create the SQLite database file, tables, and indexes if needed."""
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with db_connection() as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitored_data (
                timestamp INTEGER NOT NULL,
                sensor TEXT NOT NULL,
                value TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alarms (
                timestamp INTEGER NOT NULL,
                sensor TEXT NOT NULL,
                value TEXT NOT NULL,
                transition INTEGER NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_states (
                sensor TEXT PRIMARY KEY,
                last_state INTEGER NOT NULL,
                last_timestamp INTEGER NOT NULL,
                last_value TEXT NOT NULL
            )
            """
        )

        # Optimized for queries such as:
        #   SELECT ... FROM monitored_data
        #   WHERE sensor = ? AND timestamp BETWEEN ? AND ?
        #   ORDER BY timestamp;
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_monitored_data_sensor_timestamp
            ON monitored_data (sensor, timestamp)
            """
        )

        # Also useful when retrieving all sensor data in a time window.
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_monitored_data_timestamp
            ON monitored_data (timestamp)
            """
        )

        # Optimized for alarm history queries over a time window, optionally
        # restricted to a single sensor.
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alarms_sensor_timestamp
            ON alarms (sensor, timestamp)
            """
        )

        # Also useful when retrieving all alarms in a time window, regardless
        # of sensor, for dashboard-level alarm history views.
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alarms_timestamp
            ON alarms (timestamp)
            """
        )

        # Useful if you later want to query recently updated sensor states.
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sensor_states_last_timestamp
            ON sensor_states (last_timestamp)
            """
        )


# ---------------------------------------------------------------------
# API models and endpoints
# ---------------------------------------------------------------------
class SensorReadingIn(BaseModel):
    """Payload accepted by the sensor data ingestion endpoint."""
    sensor: str = Field(..., min_length=1, description="Sensor identifier")
    value: str = Field(..., min_length=1, description="Sensor value")


def check_api_token(request: Request) -> None:
    """Raise HTTP 401 if API token protection is enabled and the token is invalid."""
    if API_TOKEN is None:
        return

    supplied_token = request.headers.get("X-API-Token")
    if supplied_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


def send_alarm_notification(sensor: str, value_str: str, timestamp: int, transition: int) -> None:
    """Send an ntfy notification for a new alarm without blocking the API call."""
    if NTFY_TOPIC is None:
        return
    if transition==0:
        return

    sensor = SIGNAL_TABLE[sensor]
    msg = sensor.alarm_msg(transition,"fr")
    alarm_time = format_local_time(timestamp,"fr")
    value = sensor.value(value_str)
    value_formatted = sensor.format(value)

    title = "Alarme boucle d'eau"
    message = f"{msg} ({value_formatted}) \n {alarm_time}"

    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    data = message.encode("utf-8")
    headers = {
        "Title": title,
        "Priority": NTFY_PRIORITY,
        "Tags": "warning" if transition else "white_check_mark",
    }

    try:
        req = urlrequest.Request(url, data=data, headers=headers, method="POST")
        with urlrequest.urlopen(req, timeout=5):
            pass
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"ntfy notification failed for message={message!r}: {exc}")   
        
def start_alarm_notification_thread(sensor: str, value_str: str, timestamp: int, transition: int) -> None:
    """Start notification sending in a daemon thread to avoid API timeout."""
    thread = threading.Thread(
        target=send_alarm_notification,
        args=(sensor, value_str, timestamp, transition),
        daemon=True,
    )
    thread.start()


def check_for_alarm(sensor_name: str, value_str: str, timestamp: int) -> None:
    """Detect range transitions for known sensors and write alarm events.

    The first reading for a known sensor initializes itscstate and does not 
    create an alarm because there is no previous edge.

    Transition values written to alarms:
       1,2, ...: valid -> invalid, acknowledged = false
       0       : invalid -> valid, acknowledged = true
    """
    if sensor_name not in STATUS_LEDS:
        return
    try:
        sensor = SIGNAL_TABLE[sensor_name]
        value = sensor.value(value_str)
        new_state:int = sensor.validate(value)
    except Exception as exc:
        # Alarm bookkeeping should not make data ingestion fail.
        print(
        f"Alarm bookkeeping failed for sensor={sensor_name!r}, "
        f"value={value_str!r}, timestamp={timestamp!r}: {exc}"
        )
        return
    
    transition: Optional[int] = None
    try:
        with db_connection() as connection:           
            row = connection.execute(
                """
                SELECT last_state
                FROM sensor_states
                WHERE sensor = ?
                """,
                (sensor_name,),
            ).fetchone()
            previous_state = None if row is None else int(row[0])

            if previous_state is not None and previous_state != new_state:
                transition = new_state
                acknowledged = 1 if new_state == 0 else 0

            connection.execute(
                """
                INSERT INTO sensor_states (sensor, last_state, last_timestamp, last_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sensor) DO UPDATE SET
                    last_state = excluded.last_state,
                    last_timestamp = excluded.last_timestamp,
                    last_value = excluded.last_value
                """,
                (sensor_name, new_state, timestamp, value_str),
            )

            if transition is not None:
                start_alarm_notification_thread(sensor_name, value_str, timestamp, transition)
                connection.execute(
                    """
                    INSERT INTO alarms (timestamp, sensor, value, transition, acknowledged)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (timestamp, sensor_name, value_str, transition, acknowledged),
                )
    except sqlite3.Error as exc:
        # Alarm bookkeeping should not make data ingestion fail.
        print(
        f"Alarm bookkeeping failed for sensor={sensor_name!r}, "
        f"value={value_str!r}, timestamp={timestamp!r}: {exc}"
        )
        return


@app.post("/api/data")
def post_sensor_reading(payload: SensorReadingIn, request: Request) -> dict[str, Any]:
    """Store one sensor value in the monitored_data table."""
    check_api_token(request)

    if payload.sensor not in SIGNAL_TABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sensor: {payload.sensor}",
        )

    sensor = SIGNAL_TABLE[payload.sensor]

    try:
        # Parse once here to reject malformed values before DB insert.
        sensor.value(payload.value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())

    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO monitored_data (timestamp, sensor, value)
                VALUES (?, ?, ?)
                """,
                (timestamp, payload.sensor, payload.value),
            )
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not store sensor reading",
        ) from exc

    check_for_alarm(payload.sensor, payload.value, timestamp)

    return {
        "status": "ok",
        "timestamp": timestamp,
        "sensor": payload.sensor,
        "value": payload.value,
    }

# ---------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------

def choose_language_from_request(request: Request) -> str:
    """Return 'fr' for French browsers, otherwise default to English."""
    accept_language = request.headers.get("accept-language", "").lower()
    return "fr" if accept_language.startswith("fr") else "en"


def format_local_time(timestamp: Optional[int], language: str) -> str:
    """Format Unix epoch seconds as local French or English display time."""
    if timestamp is None:
        return translate("unknown_time",language)
    dt = datetime.fromtimestamp(timestamp, tz=LOCAL_TZ)
    if language == "fr":
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_latest_sensor_state(sensor_name: str, language:str) -> tuple[Optional[int], Optional[str], Optional[int], str]:
    """Fetch the latest state for one sensor from sensor_states."""
    with db_connection() as connection:           
        connection.execute("PRAGMA busy_timeout = 30000")
        row = connection.execute(
            """
            SELECT last_timestamp, last_value, last_state
            FROM sensor_states
            WHERE sensor = ?
            """,
            (sensor_name,),
        ).fetchone()
    sensor = SIGNAL_TABLE[sensor_name]
    label = sensor.description(language)
    if row is None:
        return None, None, None, label
    value = sensor.value(row[1])
    value_str = sensor.format(value)
    is_valid = 1 if int(row[2])==0 else 0
    return int(row[0]), value_str, is_valid, label 


def get_sensor_points_many(
    sensors: list[str],
    start_timestamp: int,
    end_timestamp: int,
) -> dict[str, list[tuple[int, float]]]:
    if not sensors:
        return {}

    placeholders = ",".join("?" for _ in sensors)

    with db_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT sensor, timestamp, value
            FROM monitored_data
            WHERE sensor IN ({placeholders})
              AND timestamp BETWEEN ? AND ?
            ORDER BY sensor, timestamp
            """,
            [*sensors, start_timestamp, end_timestamp],
        ).fetchall()

    result: dict[str, list[tuple[int, float]]] = {sensor: [] for sensor in sensors}

    for sensor_name, timestamp, value_str in rows:
        sensor = SIGNAL_TABLE[sensor_name]
        result[sensor_name].append((int(timestamp), sensor.value(value_str)))

    return result


def get_last_sensor_values(sensors: list[str]) -> dict[str, dict[str, str]]:
    """Fetch the latest formatted value and status for each requested sensor.

    Returns:
        {
            sensor_name: {
                "value": formatted_value,
                "status": "ok" | "alarm" | "stale" | "no_data" | "no_range",
                "color": html_color,
            },
            ...
        }

    Behavior:
        - no data: "--", grey
        - malformed data: "--", grey
        - no valid range: last value, black, even if old/stalled
        - stale validating sensor: last value, orange
        - fresh validating sensor in range: last value, green
        - fresh validating sensor out of range: last value, red
    """
    if not sensors:
        return {}

    now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    placeholders = ",".join("?" for _ in sensors)

    query = f"""
        SELECT sensor, timestamp, value
        FROM (
            SELECT sensor, timestamp, value,
                ROW_NUMBER() OVER (
                    PARTITION BY sensor
                    ORDER BY timestamp DESC, rowid DESC
                ) AS rn
            FROM monitored_data
            WHERE sensor IN ({placeholders})
        )
        WHERE rn = 1
    """

    with db_connection() as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        rows = connection.execute(query, sensors).fetchall()

    values: dict[str, dict[str, str]] = {}

    for sensor_name, timestamp, value_str in rows:
        sensor_name = str(sensor_name)
        sensor_obj = SIGNAL_TABLE[sensor_name]

        try:
            value = sensor_obj.value(value_str)
            formatted_value = sensor_obj.format(value)
        except ValueError:
            status = "no_data"
            values[sensor_name] = {
                "value": "--",
                "status": status,
                "color": SCHEME_STATUS_COLORS[status],
            }
            continue

        age_seconds = now_timestamp - int(timestamp)

        if not hasattr(sensor_obj, "validate"):
            status = "no_range"
        elif age_seconds > STALE_AFTER_SECONDS:
            status = "stale"
        else:
            status = "ok" if sensor_obj.validate(value) == 0 else "alarm"

        values[sensor_name] = {
            "value": formatted_value,
            "status": status,
            "color": SCHEME_STATUS_COLORS[status],
        }

    return values

def make_indicator_html(status: str) -> str:
    """Create the colored LED-like indicator HTML.

    Valid statuses:
        ok, alarm, stale, no_data, no_range
    """
    dot_color = SCHEME_STATUS_COLORS.get(
        status,
        SCHEME_STATUS_COLORS["no_data"],
    )

    return (
        '<div style="width:18px;height:18px;border-radius:50%;'
        f'background:{dot_color};box-shadow:0 0 10px {dot_color};'
        'border:1px solid rgba(0,0,0,0.25);"></div>'
    )

def split_downsampled_points(
    points: list[tuple[int, float]],
    max_points: int = 2000,
) -> tuple[np.ndarray, np.ndarray]:
    """Return downsampled timestamp and value arrays."""
    if not points:
        return np.array([], dtype=np.int64), np.array([], dtype=float)

    data = np.asarray(points, dtype=float)
    n_points = data.shape[0]

    if n_points > max_points:
        indices = np.linspace(
            0,
            n_points - 1,
            num=max_points,
            dtype=np.int64,
        )
        data = data[indices]

    timestamps = data[:, 0].astype(np.int64)
    values = data[:, 1]

    return timestamps, values


def make_plot_figure(plot_config: dict[str, Any], language: str, timespan_hours: float) -> go.Figure:
    """Build a Plotly figure for one PLOTS entry."""
    now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    start_timestamp = now_timestamp - int(timespan_hours * 3600)
    signals = plot_config.get("signals", [])

    plotly_default_colors = [
        "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
        "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
    ]

    sensor_colors: dict[str, str] = {}
    points_by_sensor = get_sensor_points_many(signals, start_timestamp, now_timestamp)

    figure = go.Figure()

    for index, sensor in enumerate(signals):
        color = plotly_default_colors[index % len(plotly_default_colors)]
        sensor_colors[sensor] = color

        points = points_by_sensor.get(sensor, [])
        timestamps, y_values = split_downsampled_points(points, max_points=2000)
        x_values = [
            datetime.fromtimestamp(int(timestamp), tz=LOCAL_TZ)
            for timestamp in timestamps
        ]

        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=y_values,
                mode="lines",
                name=plot_config["legend"][index][language] if "legend" in plot_config else None,
                line={"color": color, "shape": "linear"},
                marker={"color": color},
            )
        )

    figure.update_layout(
        title=plot_config["title"][language],
        xaxis_title=plot_config["xlabel"][language],
        yaxis_title=plot_config["ylabel"][language],
        margin={"l": 50, "r": 20, "t": 50, "b": 50},
        showlegend="legend" in plot_config,
        template="plotly_white",
        height=360,
        legend=dict(
            x=0.02,
            y=0.98,
            xanchor="left",
            yanchor="top",
            bgcolor="rgba(255,255,255,0.7)",
            bordercolor="rgba(0,0,0,0.2)",
            borderwidth=1,
        ),
    )

    for sensor in signals:
        if sensor not in STATUS_LEDS:
            continue
        sensor_obj = SIGNAL_TABLE[sensor]
        if hasattr(sensor_obj, "min_value"):
            figure.add_hline(
                y=sensor_obj.min_value,
                line_dash="dash",
                line_color=sensor_colors.get(sensor),
                opacity=0.5,
            )

        if hasattr(sensor_obj, "max_value"):
            figure.add_hline(
                y=sensor_obj.max_value,
                line_dash="dash",
                line_color=sensor_colors.get(sensor),
                opacity=0.5,
            )

    return figure

@ui.page("/")
def dashboard_page(request: Request) -> None:
    """Main NiceGUI dashboard page."""
    state: dict[str, Any] = {
        "language": choose_language_from_request(request),
        "timespan_hours": float(DEFAULT_TIMESPAN_HOURS),
    }
    status_items: dict[str, dict[str, Any]] = {}
    plot_items: list[tuple[dict[str, Any], Any]] = []
    tr = lambda s : translate(s,state["language"])

    def refresh_status() -> None:
        """Update existing status cards using only sensor_states."""
        language = state["language"]

        for sensor_name, items in status_items.items():
            timestamp, value_str, is_valid, label = get_latest_sensor_state(sensor_name, language)

            if timestamp is None or value_str is None or is_valid is None:
                status = "no_data"
                value_text = tr("no_data")
            else:
                now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
                value_text = f"{value_str} · {format_local_time(timestamp, language)}"

                if now_timestamp - timestamp > STALE_AFTER_SECONDS:
                    status = "stale"
                elif is_valid == 1:
                    status = "ok"
                else:
                    status = "alarm"

            items["indicator"].content = make_indicator_html(status)
            items["indicator"].update()

            items["label"].text = label
            items["label"].update()

            items["value"].text = value_text
            items["value"].update()

            status_text_by_status = {
                "ok": tr("ok"),
                "alarm": tr("alarm"),
                "stale": tr("stale"),
                "no_data": tr("no_data"),
                "no_range": tr("no_data"),
            }

            items["status"].text = status_text_by_status.get(status, tr("no_data"))
            items["status"].update()
        
    def refresh_plots() -> None:
        """Update existing Plotly widgets without rebuilding the page."""
        for plot_config, plot_widget in plot_items:
            plot_widget.figure = make_plot_figure(
                plot_config,
                state["language"],
                state["timespan_hours"],
            )
            plot_widget.update()

    def make_timespan_options() -> dict[str, str]:
        return {
            1 : "1h",
            12 : "12h",
            24 : "24h",
            168 : translate("last_week",state["language"]),
            720 : translate("last_month",state["language"]),
            }
       

    def refresh_language_text() -> None:
        """Update static bilingual labels and then dynamic components."""
        title_label.text = tr("title")
        title_label.update()
        language_label.text = tr("language")
        language_label.update()
        timespan_select.label = tr("timespan")
        timespan_select.options = make_timespan_options()
        timespan_select.update()
        alarms_button.text = tr("alarms")
        alarms_button.update()
        refresh_status()
        refresh_plots()

    def set_language(language: str) -> None:
        state["language"] = language
        scheme_html.content = load_scheme_svg(language)
        scheme_html.update()
        refresh_scheme_values()
        refresh_language_text()

    def set_timespan(event: Any) -> None:
        state["timespan_hours"] = float(event.value)
        refresh_plots()


    ui.add_head_html("""
        <style>
        .status-card { min-width: 220px; }
        .water-loop-scheme {
            width: 100%;
            display: flex;
            justify-content: center;
        }
        .water-loop-scheme svg {
            width: 100%;
            max-width: 1100px;
            height: auto;
            display: block;
        }        
        </style>
        """)

    with ui.column().classes("w-full p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            # Title, language 
            title_label = ui.label(tr("title")).classes("text-2xl font-bold")
            with ui.row().classes("items-center gap-2"):
                language_label = ui.label(tr("language"))
                ui.button("EN", on_click=lambda: set_language("en")).props("outline dense")
                ui.button("FR", on_click=lambda: set_language("fr")).props("outline dense")

        # Water-loop scheme
        with ui.card().classes("w-full p-4"):
            with ui.element("div").classes("w-full flex justify-center"):
                scheme_html = ui.html(load_scheme_svg(state["language"])).classes("water-loop-scheme")
        
        # LED status
        with ui.row().classes("w-full gap-3 items-stretch"):
            for sensor_name in STATUS_LEDS:
                sensor = SIGNAL_TABLE[sensor_name]
                with ui.card().classes("status-card grow"):
                    with ui.row().classes("items-center gap-3"):
                        indicator = ui.html(make_indicator_html("no-data"))
                        label = ui.label(sensor.description(state["language"])).classes("font-semibold")
                    value_label = ui.label(tr("no_data")).classes("text-sm")
                    status_label = ui.label(tr("no_data")).classes("text-xs uppercase")
                    status_items[sensor_name] = {
                        "indicator": indicator,
                        "label": label,
                        "value": value_label,
                        "status": status_label,
                    }
            timespan_select = ui.select(
                make_timespan_options(),
                value=DEFAULT_TIMESPAN_HOURS,
                label=tr("timespan"),
                on_change=set_timespan,
            ).classes("w-40")

        # Plots
        with ui.column().classes("w-full gap-4"):
            for plot_config in PLOTS:
                plot_widget = ui.plotly(
                    make_plot_figure(plot_config, state["language"], state["timespan_hours"])
                ).classes("w-full")
                plot_items.append((plot_config, plot_widget))

        # Bottom buttons
        with ui.row().classes("w-full items-end gap-3"):
            #refresh_button = ui.button(tr("refresh"), on_click=refresh_plots)
            alarms_button = ui.button(tr("alarms"), on_click=lambda: ui.navigate.to("/alarms"))
        if NTFY_TOPIC is not None:
            ui.separator()
            ui.label(tr("phone_registration").format(NTFY_TOPIC=NTFY_TOPIC)).classes("text-sm text-gray-600")

    refresh_status()
    refresh_plots()
    ui.timer(LED_REFRESH_PERIOD_SECONDS, refresh_status)
    ui.timer(LED_REFRESH_PERIOD_SECONDS, refresh_scheme_values)
    ui.timer(PLOT_REFRESH_PERIOD_SECONDS, refresh_plots)


# ---------------------------------------------------------------------
# Scheme
# ---------------------------------------------------------------------
SCHEME_SVG_TEMPLATE_FILES = {
    "en": BASE_DIR / "water_loop_scheme_en.svg",
    "fr": BASE_DIR / "water_loop_scheme_fr.svg",
}

SCHEME_PLACEHOLDERS = {
    "P001": "primary_pressure_1",
    "T001": "gf01_temperature_out",
    "T002": "gf02_temperature_out",
    "T005": "primary_temperature_1",
    "T004": "primary_temperature_2",
    "D001": "secondary_flow_1",
    "T006": "secondary_temperature_1",
    "T007": "secondary_temperature_2",
    "V001": "valve_command",
    "S001": "gf01_state",
    "S002": "gf02_state",
    "S003": "pmp07_state",
}


def load_scheme_svg(language: str) -> str:
    """Load the SVG template for the selected language."""
    svg_file = SCHEME_SVG_TEMPLATE_FILES.get(
        language,
        SCHEME_SVG_TEMPLATE_FILES["en"],
    )
    return svg_file.read_text(encoding="utf-8")

def refresh_scheme_values() -> None:
    ui.run_javascript("""
        fetch('/api/scheme-values')
            .then(response => response.json())
            .then(values => {
                for (const [key, item] of Object.entries(values)) {
                    const valueElement = document.getElementById(`scheme-value-${key}`);

                    if (valueElement) {
                        valueElement.textContent = item.value;
                        valueElement.style.fill = item.color;
                    }

                    const statusElement = document.getElementById(`scheme-status-${key}`);

                    if (statusElement) {
                        statusElement.style.fill = item.color;
                        statusElement.style.stroke = item.color;
                    }
                }
            });
    """)

@app.get("/api/scheme-values")
def api_scheme_values() -> dict[str, dict[str, str]]:
    sensors = list(set(SCHEME_PLACEHOLDERS.values()))
    latest_values = get_last_sensor_values(sensors)

    result: dict[str, dict[str, str]] = {}

    for placeholder, sensor_name in SCHEME_PLACEHOLDERS.items():
        result[placeholder] = latest_values.get(
            sensor_name,
            {
                "value": "--",
                "status": "no_data",
                "color": SCHEME_STATUS_COLORS["no_data"],
            },
        )

    return result

# ---------------------------------------------------------------------
# Insert alarm-table UI functions below this line.
# ---------------------------------------------------------------------


ALARM_TIMESPANS_SECONDS = {
    "day": 24 * 3600,
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
    "year": 365 * 24 * 3600,
    "all": None,
}


def get_alarm_rows(timespan_key: str, language: str, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch alarm rows for the selected timespan."""
    now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    timespan_seconds = ALARM_TIMESPANS_SECONDS[timespan_key]

    with db_connection() as connection:           
        connection.execute("PRAGMA busy_timeout = 30000")

        if timespan_seconds is None:
            rows = connection.execute(
                """
                SELECT timestamp, sensor, value, transition
                FROM alarms
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            start_timestamp = now_timestamp - timespan_seconds
            rows = connection.execute(
                """
                SELECT timestamp, sensor, value, transition
                FROM alarms
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (start_timestamp, limit),
            ).fetchall()

    formatted_rows = []
    for index, row in enumerate(rows):
        timestamp, sensor_name, value_str, transition = row
        transition = int(transition)
        sensor = SIGNAL_TABLE[sensor_name]
        value = sensor.value(value_str)
        event = sensor.alarm_msg(transition, language)
        formatted_rows.append(
            {
                "id": index,
                "time": format_local_time(int(timestamp), language),
                "event": event,
                "transition_code": transition,
                "value" : sensor.format(value),
            }
        )

    return formatted_rows


@ui.page("/alarms")
def alarms_page(request: Request) -> None:
    """Alarm history page."""
    state: dict[str, Any] = {
        "language": choose_language_from_request(request),
        "timespan": "day",
    }

    ui.add_head_html(
        """
        <style>
        .alarm-row-on {
            background-color: #fee2e2 !important;
        }

        .alarm-row-off {
            background-color: #dcfce7 !important;
        }
        </style>
        """
    )

    def tr(key: str) -> str:
        return translate(key, state["language"])

    def make_timespan_options() -> dict[str, str]:
        return {
            "day": tr("last_day"),
            "week": tr("last_week"),
            "month": tr("last_month"),
            "year": tr("last_year"),
            "all": tr("all"),
        }

    def make_columns() -> list[dict[str, Any]]:
        return [
            {
                "name": "time",
                "label": tr("time"),
                "field": "time",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "event",
                "label": tr("event"),
                "field": "event",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "value",
                "label": tr("value"),
                "field": "value",
                "align": "left",
                "sortable": True,
            },
        ]

    def refresh_table() -> None:
        alarm_table.rows = get_alarm_rows(state["timespan"], state["language"])
        alarm_table.update()

    def refresh_language_text() -> None:
        title_label.text = tr("alarm_title")
        title_label.update()

        language_label.text = tr("language")
        language_label.update()

        timespan_select.label = tr("timespan")
        timespan_select.options = make_timespan_options()
        timespan_select.update()

        refresh_button.text = tr("refresh")
        refresh_button.update()

        back_button.text = tr("back")
        back_button.update()

        alarm_table.props(
            f'no-data-label="{tr("no_rows")}" '
            f'rows-per-page-label="{tr("records_per_page")}"'
        )

        alarm_table.columns = make_columns()
        alarm_table.no_data_label = tr("no_rows")
        refresh_table()

    def set_language(language: str) -> None:
        state["language"] = language
        refresh_language_text()

    def set_timespan(event: Any) -> None:
        state["timespan"] = str(event.value)
        refresh_table()

    with ui.column().classes("w-full p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            title_label = ui.label(tr("alarm_title")).classes("text-2xl font-bold")

            with ui.row().classes("items-center gap-2"):
                language_label = ui.label(tr("language"))
                ui.button("EN", on_click=lambda: set_language("en")).props("outline dense")
                ui.button("FR", on_click=lambda: set_language("fr")).props("outline dense")

        with ui.row().classes("w-full items-end gap-3"):
            timespan_select = ui.select(
                make_timespan_options(),
                value=state["timespan"],
                label=tr("timespan"),
                on_change=set_timespan,
            ).classes("w-56")

            refresh_button = ui.button(tr("refresh"), on_click=refresh_table)

            back_button = ui.button(
                tr("back"),
                on_click=lambda: ui.run_javascript("history.back()"),
            )


        alarm_table = ui.table(
            columns=make_columns(),
            rows=get_alarm_rows(state["timespan"], state["language"]),
            row_key="id",
            pagination=25,
        ).classes("w-full")

        alarm_table.props(
            f'no-data-label="{tr("no_rows")}" '
            f'rows-per-page-label="{tr("records_per_page")}"'
        )

        alarm_table.add_slot(
            "body",
            """
            <q-tr :props="props"
                  :class="props.row.transition_code > 0 ? 'alarm-row-on' : 'alarm-row-off'">
                <q-td v-for="col in props.cols"
                      :key="col.name"
                      :props="props">
                    {{ col.value }}
                </q-td>
            </q-tr>
            """,
        )

        alarm_table.no_data_label = tr("no_rows")

# ---------------------------------------------------------------------
# Application start
# ---------------------------------------------------------------------

def check_startup_configuration() -> None:
    """Validate static app configuration before serving requests."""
    missing_files = [
        f"{language}: {path}"
        for language, path in SCHEME_SVG_TEMPLATE_FILES.items()
        if not path.is_file()
    ]

    if missing_files:
        missing = "\n".join(missing_files)
        raise RuntimeError(
            "Missing scheme SVG template file(s):\n"
            f"{missing}"
        )

    missing_status_leds = [
        sensor
        for sensor in STATUS_LEDS
        if sensor not in SIGNAL_TABLE
    ]

    if missing_status_leds:
        missing = ", ".join(missing_status_leds)
        raise RuntimeError(
            "Invalid STATUS_LEDS configuration: "
            f"these sensors are not in SIGNAL_TABLE: {missing}"
        )

    non_validating_status_leds = [
        sensor
        for sensor in STATUS_LEDS
        if not hasattr(SIGNAL_TABLE[sensor], "validate")
    ]

    if non_validating_status_leds:
        invalid = ", ".join(non_validating_status_leds)
        raise RuntimeError(
            "Invalid STATUS_LEDS configuration: "
            f"these sensors have no validate() method: {invalid}"
        )
    plot_sensors = [
        sensor
        for plot_config in PLOTS
        for sensor in plot_config.get("signals", [])
    ]

    missing_plot_sensors = [
        sensor
        for sensor in plot_sensors
        if sensor not in SIGNAL_TABLE
    ]

    if missing_plot_sensors:
        missing = ", ".join(missing_plot_sensors)
        raise RuntimeError(
            "Invalid PLOTS configuration: "
            f"these sensors are not in SIGNAL_TABLE: {missing}"
        )

    missing_scheme_sensors = [
        sensor
        for sensor in SCHEME_PLACEHOLDERS.values()
        if sensor not in SIGNAL_TABLE
    ]

    if missing_scheme_sensors:
        missing = ", ".join(sorted(set(missing_scheme_sensors)))
        raise RuntimeError(
            "Invalid SCHEME_PLACEHOLDERS configuration: "
            f"these sensors are not in SIGNAL_TABLE: {missing}"
        )
    
    for plot_config in PLOTS:
        if "legend" in plot_config and len(plot_config["legend"]) != len(plot_config.get("signals", [])):
            raise RuntimeError(
                f"Invalid PLOTS configuration for {plot_config.get('title')}: "
                "legend length must match signals length"
            )

def startup() -> None:
    check_startup_configuration()
    create_tables_if_needed()

app.on_startup(startup)

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="WaterLoop",
    )