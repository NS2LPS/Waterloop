from datetime import datetime
import time
import threading
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError
from zoneinfo import ZoneInfo
from typing import Any, Optional
from contextlib import contextmanager, closing
from collections.abc import Iterator
import re

import math
import plotly.graph_objects as go
import mysql.connector
from mysql.connector.connection import MySQLConnection
from mysql.connector import Error as MySQLError

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from nicegui import ui, app

# Sensors and signals
from signals import SIGNAL_TABLE

# Translation table
from languages import translate


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
LOCAL_TZ = ZoneInfo("Europe/Paris")
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env.

    Environment variables use the WATERLOOP_ prefix.

    Example:
        WATERLOOP_DB_HOST=127.0.0.1
        WATERLOOP_DB_PORT=3306
        WATERLOOP_DB_USER=waterloop
        WATERLOOP_DB_PASSWORD=secret
        WATERLOOP_DB_NAME=waterloop
        WATERLOOP_API_TOKEN=secret-token
        WATERLOOP_NTFY_TOPIC=my-topic
    """

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "waterloop"
    db_password: str = ""
    db_name: str = "waterloop"
    create_database_if_needed: bool = True
    db_pool_size: int = 10

    monitored_data_retention_days: int = 4
    alarms_retention_days: int = 365
    archive_rollover_period_seconds: int = 6 * 3600
    archive_batch_size: int = 10_000

    api_token: Optional[str] = None

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: Optional[str] = None
    ntfy_priority: str = "urgent"

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="WATERLOOP_",
        extra="ignore",
    )

    @field_validator("api_token", "ntfy_topic", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: object) -> object:
        """Treat empty strings in .env as unset optional values."""
        if value == "":
            return None
        return value


settings = Settings()

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# Optional API token.
#
# If API_TOKEN is None, the POST endpoint accepts requests without a token.
# If API_TOKEN is a string, clients must send:
#
#   X-API-Token: your-token-here
#
API_TOKEN = settings.api_token

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
    "secondary_temperature_2",
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
        "signals": ["primary_temperature_1", "secondary_temperature_1", "secondary_temperature_2"],
        "legend" : [{"en":"Primary","fr":"Primaire"},{"en":"Secondary","fr":"Secondaire"},{"en":"Secondary (SEMFEG)","fr":"Secondaire (SEMFEG)"}]
    },
    # {
    #     "title": {"en": "Primary Pressure", "fr": "Pression primaire"},
    #     "xlabel": {"en": "Time", "fr": "Temps"},
    #     "ylabel": {"en": "Pressure (bar)", "fr": "Pression (bar)"},
    #     "signals": ["primary_pressure_1"],
    # },
    # {
    #     "title": {"en": "Secondary Flow", "fr": "Débit secondaire"},
    #     "xlabel": {"en": "Time", "fr": "Temps"},
    #     "ylabel": {"en": "Flow rate (L/min)", "fr": "Débit (L/min)"},
    #     "signals": ["secondary_flow_1"],
    # },
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
NTFY_SERVER = settings.ntfy_server
NTFY_TOPIC = settings.ntfy_topic
NTFY_PRIORITY = settings.ntfy_priority


# ---------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------
DATABASE_CONFIG = {
    "host": settings.db_host,
    "port": settings.db_port,
    "user": settings.db_user,
    "password": settings.db_password,
    "database": settings.db_name,
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
}

SERVER_DATABASE_CONFIG = {
    key: value
    for key, value in DATABASE_CONFIG.items()
    if key != "database"
}

_db_pool: Optional[mysql.connector.pooling.MySQLConnectionPool] = None
_db_pool_lock = threading.Lock()

# ---------------------------------------------------------------------
# Archive / retention settings
# ---------------------------------------------------------------------
MONITORED_DATA_LIVE_RETENTION_DAYS = settings.monitored_data_retention_days
ALARMS_LIVE_RETENTION_DAYS = settings.alarms_retention_days
ARCHIVE_ROLLOVER_PERIOD_SECONDS = settings.archive_rollover_period_seconds
ARCHIVE_BATCH_SIZE = settings.archive_batch_size

_archive_rollover_lock = threading.Lock()
_last_archive_rollover_timestamp = 0

def quote_mysql_identifier(identifier: str) -> str:
    """Safely quote a MySQL identifier such as a database name."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", identifier):
        raise RuntimeError(f"Unsafe MySQL identifier: {identifier!r}")
    return f"`{identifier}`"

def get_db_pool() -> mysql.connector.pooling.MySQLConnectionPool:
    """Return the shared MySQL connection pool, creating it lazily."""
    global _db_pool

    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                _db_pool = mysql.connector.pooling.MySQLConnectionPool(
                    pool_name="waterloop_pool",
                    pool_size=settings.db_pool_size,
                    pool_reset_session=True,
                    **DATABASE_CONFIG,
                )

    return _db_pool


@contextmanager
def db_connection() -> Iterator[Any]:
    """Borrow a MySQL connection from the pool and commit/rollback the transaction."""
    connection = get_db_pool().get_connection()
    connection.autocommit = False

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_database_if_needed() -> None:
    """Create the MySQL database if it does not already exist.

    Uses SERVER_DATABASE_CONFIG because DATABASE_CONFIG includes the database
    name, and connecting to a database that does not exist would fail.
    """
    if not settings.create_database_if_needed:
        return

    quoted_database_name = quote_mysql_identifier(settings.db_name)

    try:
        connection = mysql.connector.connect(**SERVER_DATABASE_CONFIG)
        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    f"""
                    CREATE DATABASE IF NOT EXISTS {quoted_database_name}
                    CHARACTER SET utf8mb4
                    COLLATE utf8mb4_unicode_ci
                    """
                )
            connection.commit()
        finally:
            connection.close()

    except MySQLError as exc:
        raise RuntimeError(
            f"Could not create or access MySQL database {settings.db_name!r}: {exc}"
        ) from exc
    

def create_tables_if_needed() -> None:
    """Create the MySQL tables and indexes if needed."""

    with db_connection() as connection:
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS monitored_data (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    `timestamp` BIGINT NOT NULL,
                    sensor VARCHAR(128) NOT NULL,
                    value VARCHAR(255) NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_monitored_data_sensor_timestamp_id (sensor, `timestamp` DESC, id DESC),
                    KEY idx_monitored_data_timestamp (`timestamp`)
                ) ENGINE=InnoDB
                DEFAULT CHARSET=utf8mb4
                COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS alarms (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    `timestamp` BIGINT NOT NULL,
                    sensor VARCHAR(128) NOT NULL,
                    value VARCHAR(255) NOT NULL,
                    transition INT NOT NULL,
                    acknowledged TINYINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (id),
                    KEY idx_alarms_sensor_timestamp (sensor, `timestamp`),
                    KEY idx_alarms_timestamp (`timestamp`),
                    CONSTRAINT chk_alarms_acknowledged
                        CHECK (acknowledged IN (0, 1))
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sensor_states (
                    sensor VARCHAR(128) NOT NULL,
                    last_state INT NOT NULL,
                    last_timestamp BIGINT NOT NULL,
                    last_value VARCHAR(255) NOT NULL,
                    PRIMARY KEY (sensor),
                    KEY idx_sensor_states_last_timestamp (last_timestamp)
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS monitored_data_archive (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    original_id BIGINT UNSIGNED NOT NULL,
                    `timestamp` BIGINT NOT NULL,
                    sensor VARCHAR(128) NOT NULL,
                    value VARCHAR(255) NOT NULL,
                    archived_at BIGINT NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_monitored_data_archive_original_id (original_id),
                    KEY idx_monitored_data_sensor_timestamp_id (sensor, `timestamp` DESC, id DESC),
                    KEY idx_monitored_data_archive_timestamp (`timestamp`)
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS alarms_archive (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    original_id BIGINT UNSIGNED NOT NULL,
                    `timestamp` BIGINT NOT NULL,
                    sensor VARCHAR(128) NOT NULL,
                    value VARCHAR(255) NOT NULL,
                    transition INT NOT NULL,
                    acknowledged TINYINT NOT NULL,
                    archived_at BIGINT NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_alarms_archive_original_id (original_id),
                    KEY idx_alarms_archive_sensor_timestamp (sensor, `timestamp`),
                    KEY idx_alarms_archive_timestamp (`timestamp`)
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )


def refresh_sensor_description_table() -> None:
    """Rebuild the sensor_description table from SIGNAL_TABLE at startup."""

    rows = [
        (
            sensor_name,
            sensor.description("en"),
            sensor.description("fr"),
        )
        for sensor_name, sensor in SIGNAL_TABLE.items()
    ]

    with db_connection() as connection:
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                DROP TABLE IF EXISTS sensor_description
                """
            )

            cursor.execute(
                """
                CREATE TABLE sensor_description (
                    key_name VARCHAR(128) NOT NULL,
                    description_en VARCHAR(255) NOT NULL,
                    description_fr VARCHAR(255) NOT NULL,
                    PRIMARY KEY (key_name)
                ) ENGINE=InnoDB
                  DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )

            if rows:
                cursor.executemany(
                    """
                    INSERT INTO sensor_description (
                        key_name,
                        description_en,
                        description_fr
                    )
                    VALUES (%s, %s, %s)
                    """,
                    rows,
                )


def archive_monitored_data_batch(cursor, cutoff_timestamp: int, archived_at: int, batch_size: int) -> int:
    """Move one batch of old monitored_data rows into monitored_data_archive."""
    cursor.execute(
        """
        SELECT id
        FROM monitored_data
        WHERE `timestamp` < %s
        ORDER BY `timestamp`, id
        LIMIT %s
        FOR UPDATE
        """,
        (cutoff_timestamp, batch_size),
    )
    ids = [int(row[0]) for row in cursor.fetchall()]

    if not ids:
        return 0

    placeholders = ",".join(["%s"] * len(ids))

    cursor.execute(
        f"""
        INSERT INTO monitored_data_archive (original_id, `timestamp`, sensor, value, archived_at)
        SELECT id, `timestamp`, sensor, value, %s
        FROM monitored_data
        WHERE id IN ({placeholders})
        """,
        [archived_at, *ids],
    )

    cursor.execute(
        f"""
        DELETE FROM monitored_data
        WHERE id IN ({placeholders})
        """,
        ids,
    )

    return len(ids)


def archive_alarms_batch(cursor, cutoff_timestamp: int, archived_at: int, batch_size: int) -> int:
    """Move one batch of old alarms rows into alarms_archive."""
    cursor.execute(
        """
        SELECT id
        FROM alarms
        WHERE `timestamp` < %s
        ORDER BY `timestamp`, id
        LIMIT %s
        FOR UPDATE
        """,
        (cutoff_timestamp, batch_size),
    )
    ids = [int(row[0]) for row in cursor.fetchall()]

    if not ids:
        return 0

    placeholders = ",".join(["%s"] * len(ids))

    cursor.execute(
        f"""
        INSERT INTO alarms_archive (
            original_id, `timestamp`, sensor, value, transition, acknowledged, archived_at
        )
        SELECT id, `timestamp`, sensor, value, transition, acknowledged, %s
        FROM alarms
        WHERE id IN ({placeholders})
        """,
        [archived_at, *ids],
    )

    cursor.execute(
        f"""
        DELETE FROM alarms
        WHERE id IN ({placeholders})
        """,
        ids,
    )

    return len(ids)


def archive_old_rows() -> None:
    """Move old rows from growing live tables into archive tables."""
    now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    monitored_data_cutoff = now_timestamp - MONITORED_DATA_LIVE_RETENTION_DAYS * 24 * 3600
    alarms_cutoff = now_timestamp - ALARMS_LIVE_RETENTION_DAYS * 24 * 3600

    with db_connection() as connection:
        with closing(connection.cursor()) as cursor:
            moved_monitored_data = archive_monitored_data_batch(
                cursor=cursor,
                cutoff_timestamp=monitored_data_cutoff,
                archived_at=now_timestamp,
                batch_size=ARCHIVE_BATCH_SIZE,
            )
            moved_alarms = archive_alarms_batch(
                cursor=cursor,
                cutoff_timestamp=alarms_cutoff,
                archived_at=now_timestamp,
                batch_size=ARCHIVE_BATCH_SIZE,
            )

    if moved_monitored_data or moved_alarms:
        print(
            "Archive rollover complete: "
            f"monitored_data={moved_monitored_data}, "
            f"alarms={moved_alarms}"
        )


def archive_old_rows_if_due(force: bool = False) -> None:
    """Run archive rollover in a background thread if enough time has passed."""
    global _last_archive_rollover_timestamp

    now_timestamp = int(time.time())

    if (
        not force
        and now_timestamp - _last_archive_rollover_timestamp < ARCHIVE_ROLLOVER_PERIOD_SECONDS
    ):
        return

    if not _archive_rollover_lock.acquire(blocking=False):
        return

    def worker() -> None:
        global _last_archive_rollover_timestamp

        try:
            archive_old_rows()
            _last_archive_rollover_timestamp = int(time.time())
        except Exception as exc:
            print(f"Archive rollover failed: {exc}")
        finally:
            _archive_rollover_lock.release()

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------
# API models and endpoints
# ---------------------------------------------------------------------
class SensorReadingIn(BaseModel):
    """Payload accepted by the sensor data ingestion endpoint."""
    sensor: str = Field(..., min_length=1, max_length=128, description="Sensor identifier")
    value: str = Field(..., min_length=1, max_length=255, description="Sensor value")


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


def check_for_alarm_in_transaction(
    cursor,
    sensor_name: str,
    value_str: str,
    timestamp: int,
) -> Optional[int]:
    """Update latest sensor state and detect alarm transitions.

    All sensors are written to sensor_states.

    Sensors in STATUS_LEDS are validated and may generate alarm rows.
    Sensors not in STATUS_LEDS are stored with state 0 by default.

    Returns:
        transition code when an alarm transition occurred, otherwise None.
    """
    sensor = SIGNAL_TABLE[sensor_name]

    is_alarm_sensor = sensor_name in STATUS_LEDS

    if is_alarm_sensor:
        value = sensor.value(value_str)
        new_state: int = sensor.validate(value)
    else:
        new_state = 0

    cursor.execute(
        """
        SELECT last_state
        FROM sensor_states
        WHERE sensor = %s
        FOR UPDATE
        """,
        (sensor_name,),
    )
    row = cursor.fetchone()
    previous_state = None if row is None else int(row[0])

    transition: Optional[int] = None
    acknowledged: Optional[int] = None

    if is_alarm_sensor:
        # First bad reading creates an alarm.
        if previous_state is None:
            if new_state != 0:
                transition = new_state
                acknowledged = 0

        # Later state changes create alarm or back-to-normal events.
        elif previous_state != new_state:
            transition = new_state
            acknowledged = 1 if new_state == 0 else 0

    cursor.execute(
        """
        INSERT INTO sensor_states (sensor, last_state, last_timestamp, last_value)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            last_state = VALUES(last_state),
            last_timestamp = VALUES(last_timestamp),
            last_value = VALUES(last_value)
        """,
        (sensor_name, new_state, timestamp, value_str),
    )

    if transition is not None and acknowledged is not None:
        cursor.execute(
            """
            INSERT INTO alarms (`timestamp`, sensor, value, transition, acknowledged)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (timestamp, sensor_name, value_str, transition, acknowledged),
        )

    return transition

@app.post("/api/data")
def post_sensor_reading(payload: SensorReadingIn, request: Request) -> dict[str, Any]:
    """Store one sensor value and update alarm state in one transaction."""
    check_api_token(request)

    if payload.sensor not in SIGNAL_TABLE:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sensor: {payload.sensor}",
        )

    sensor = SIGNAL_TABLE[payload.sensor]

    try:
        value = sensor.value(payload.value)

        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(
                status_code=400,
                detail="Sensor value must be finite before database insertion",
            )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    transition: Optional[int] = None

    try:
        with db_connection() as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    """
                    INSERT INTO monitored_data (`timestamp`, sensor, value)
                    VALUES (%s, %s, %s)
                    """,
                    (timestamp, payload.sensor, payload.value),
                )

                transition = check_for_alarm_in_transaction(
                    cursor=cursor,
                    sensor_name=payload.sensor,
                    value_str=payload.value,
                    timestamp=timestamp,
                )

    except MySQLError as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not store sensor reading or alarm state",
        ) from exc

    if transition is not None:
        start_alarm_notification_thread(
            payload.sensor,
            payload.value,
            timestamp,
            transition,
        )

    archive_old_rows_if_due()

    return {
        "status": "ok",
        "timestamp": timestamp,
        "sensor": payload.sensor,
        "value": payload.value,
    }



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


def get_latest_sensor_states_many(
    sensor_names: list[str],
    language: str,
) -> dict[str, tuple[Optional[int], Optional[str], Optional[int], str]]:
    """Fetch latest states for multiple sensors from sensor_states in one query.

    On database errors, return an empty dict so callers can use no-data fallbacks.
    """
    if not sensor_names:
        return {}

    placeholders = ",".join(["%s"] * len(sensor_names))

    try:
        with db_connection() as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    f"""
                    SELECT sensor, last_timestamp, last_value, last_state
                    FROM sensor_states
                    WHERE sensor IN ({placeholders})
                    """,
                    sensor_names,
                )
                rows = cursor.fetchall()

    except MySQLError as exc:
        print(f"Could not fetch latest sensor states: {exc}")
        return {}

    rows_by_sensor = {
        str(sensor_name): (last_timestamp, last_value, last_state)
        for sensor_name, last_timestamp, last_value, last_state in rows
    }

    result: dict[str, tuple[Optional[int], Optional[str], Optional[int], str]] = {}

    for sensor_name in sensor_names:
        sensor = SIGNAL_TABLE[sensor_name]
        label = sensor.description(language)

        row = rows_by_sensor.get(sensor_name)

        if row is None:
            result[sensor_name] = (None, None, None, label)
            continue

        last_timestamp, last_value, last_state = row

        try:
            value = sensor.value(last_value)
            value_str = sensor.format(value)
            is_valid = 1 if int(last_state) == 0 else 0
            result[sensor_name] = (
                int(last_timestamp),
                value_str,
                is_valid,
                label,
            )
        except Exception as exc:
            print(f"Could not format latest state for {sensor_name!r}: {exc}")
            result[sensor_name] = (None, None, None, label)

    return result


def get_sensor_points_many(
    sensors: list[str],
    start_timestamp: int,
    end_timestamp: int,
) -> dict[str, list[tuple[int, float]]]:
    """Fetch plot points for multiple sensors.

    On database errors, return empty series for all requested sensors.
    Bad individual rows are skipped.
    """
    result: dict[str, list[tuple[int, float]]] = {sensor: [] for sensor in sensors}

    if not sensors:
        return result

    placeholders = ",".join(["%s"] * len(sensors))

    try:
        with db_connection() as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    f"""
                    SELECT sensor, `timestamp`, value
                    FROM monitored_data
                    WHERE sensor IN ({placeholders})
                      AND `timestamp` BETWEEN %s AND %s
                    ORDER BY sensor, `timestamp`
                    """,
                    [*sensors, start_timestamp, end_timestamp],
                )
                rows = cursor.fetchall()

    except MySQLError as exc:
        print(f"Could not fetch plot data: {exc}")
        return result

    for sensor_name, timestamp, value_str in rows:
        sensor_name = str(sensor_name)

        if sensor_name not in SIGNAL_TABLE:
            print(f"Skipping plot row for unknown sensor {sensor_name!r}")
            continue

        try:
            sensor = SIGNAL_TABLE[sensor_name]
            result.setdefault(sensor_name, []).append(
                (int(timestamp), sensor.value(value_str))
            )
        except Exception as exc:
            print(
                f"Skipping invalid plot row for sensor={sensor_name!r}, "
                f"timestamp={timestamp!r}, value={value_str!r}: {exc}"
            )

    return result


def get_last_sensor_values(sensors: list[str]) -> dict[str, dict[str, str]]:
    """Fetch the latest formatted value and status for each requested sensor.

    Uses sensor_states, which stores the latest known value/state for each sensor.

    On database errors, return an empty dict so callers can use no-data fallbacks.
    """
    if not sensors:
        return {}

    now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    placeholders = ",".join(["%s"] * len(sensors))

    try:
        with db_connection() as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    f"""
                    SELECT sensor, last_timestamp, last_value, last_state
                    FROM sensor_states
                    WHERE sensor IN ({placeholders})
                    """,
                    sensors,
                )
                rows = cursor.fetchall()

    except MySQLError as exc:
        print(f"Could not fetch latest scheme sensor values: {exc}")
        return {}

    values: dict[str, dict[str, str]] = {}

    for sensor_name, timestamp, value_str, last_state in rows:
        sensor_name = str(sensor_name)

        if sensor_name not in SIGNAL_TABLE:
            print(f"Skipping latest-value row for unknown sensor {sensor_name!r}")
            continue

        sensor_obj = SIGNAL_TABLE[sensor_name]

        try:
            value = sensor_obj.value(value_str)
            formatted_value = sensor_obj.format(value)
        except Exception as exc:
            print(
                f"Skipping invalid latest value for sensor={sensor_name!r}, "
                f"timestamp={timestamp!r}, value={value_str!r}: {exc}"
            )
            status = "no_data"
            values[sensor_name] = {
                "value": "--",
                "status": status,
                "color": SCHEME_STATUS_COLORS[status],
            }
            continue

        age_seconds = now_timestamp - int(timestamp)

        if age_seconds > STALE_AFTER_SECONDS:
            status = "stale"
        elif not hasattr(sensor_obj, "validate"):
            status = "no_range"
        else:
            status = "ok" if int(last_state) == 0 else "alarm"

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

    try:
        points_by_sensor = get_sensor_points_many(
            signals,
            start_timestamp,
            now_timestamp,
        )
    except Exception as exc:
        print(f"Could not build plot data for {plot_config.get('title')}: {exc}")
        points_by_sensor = {sensor: [] for sensor in signals}

    figure = go.Figure()

    if language == "fr":
        x_tickformat = "%d/%m<br>%H:%M"
        x_hoverformat = "%d/%m/%Y %H:%M:%S"
    else:
        x_tickformat = "%Y-%m-%d<br>%H:%M"
        x_hoverformat = "%Y-%m-%d %H:%M:%S"

    for index, sensor in enumerate(signals):
        color = plotly_default_colors[index % len(plotly_default_colors)]
        sensor_colors[sensor] = color

        points = points_by_sensor.get(sensor, [])
        x_values = [datetime.fromtimestamp(int(timestamp), tz=LOCAL_TZ) for timestamp, _ in points]
        y_values = [value for _, value in points]

        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=y_values,
                mode="lines",
                name=plot_config["legend"][index][language] if "legend" in plot_config else None,
                line={"color": color, "shape": "linear"},
                marker={"color": color},
                hovertemplate="%{x|" + x_hoverformat + "}<br>%{y}<extra></extra>",
            )
        )

    figure.update_layout(
        title=plot_config["title"][language],
        xaxis_title=plot_config["xlabel"][language],
        yaxis_title=plot_config["ylabel"][language],
        margin={"l": 50, "r": 20, "t": 50, "b": 50},
        showlegend="legend" in plot_config,
        xaxis={
        "tickformat": x_tickformat,
        "hoverformat": x_hoverformat,},
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

    for sensor in signals:
        if sensor not in STATUS_LEDS:
            continue
        sensor_obj = SIGNAL_TABLE[sensor]

        if hasattr(sensor_obj, "min_value") and sensor_obj.min_value is not None :
            figure.add_hline(
                y=sensor_obj.min_value,
                line_dash="dash",
                line_color=sensor_colors.get(sensor),
                opacity=0.5,
            )

        if hasattr(sensor_obj, "max_value") and sensor_obj.max_value is not None:
            figure.add_hline(
                y=sensor_obj.max_value,
                line_dash="dash",
                line_color=sensor_colors.get(sensor),
                opacity=0.5,
            )

    return figure

# ---------------------------------------------------------------------
# Main page UI
# ---------------------------------------------------------------------


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
        """Update existing status cards using one batched sensor_states query."""
        language = state["language"]

        try:
            latest_states = get_latest_sensor_states_many(
                list(status_items.keys()),
                language,
            )
        except Exception as exc:
            print(f"Could not refresh status indicators: {exc}")
            latest_states = {}

        for sensor_name, items in status_items.items():
            timestamp, value_str, is_valid, label = latest_states.get(
                sensor_name,
                (None, None, None, SIGNAL_TABLE[sensor_name].description(language)),
            )

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
            try:
                plot_widget.figure = make_plot_figure(
                    plot_config,
                    state["language"],
                    DEFAULT_TIMESPAN_HOURS,
                )
                plot_widget.update()
            except Exception as exc:
                print(f"Could not refresh plot {plot_config.get('title')}: {exc}")


    def refresh_language_text() -> None:
        """Update static bilingual labels and then dynamic components."""
        title_label.text = tr("title")
        title_label.update()
        language_label.text = tr("language")
        language_label.update()
        alarms_button.text = tr("alarms")
        alarms_button.update()
        archive_button.text = tr("archive")
        archive_button.update()
        ntfy_label.text = tr("phone_registration").format(NTFY_TOPIC=NTFY_TOPIC)
        ntfy_label.update()
        refresh_status()
        refresh_plots()

    def set_language(language: str) -> None:
        state["language"] = language
        scheme_html.content = load_scheme_svg(language)
        scheme_html.update()
        refresh_scheme_values()
        refresh_language_text()


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
                        indicator = ui.html(make_indicator_html("no_data"))
                        label = ui.label(sensor.description(state["language"])).classes("font-semibold")
                    value_label = ui.label(tr("no_data")).classes("text-sm")
                    status_label = ui.label(tr("no_data")).classes("text-xs uppercase")
                    status_items[sensor_name] = {
                        "indicator": indicator,
                        "label": label,
                        "value": value_label,
                        "status": status_label,
                    }

        # Plots
        with ui.column().classes("w-full gap-4"):
            for plot_config in PLOTS:
                plot_widget = ui.plotly(
                    make_plot_figure(plot_config, state["language"], DEFAULT_TIMESPAN_HOURS)
                ).classes("w-full")
                plot_items.append((plot_config, plot_widget))

        # Bottom buttons
        with ui.row().classes("w-full items-end gap-3"):
            alarms_button = ui.button(tr("alarms"), on_click=lambda: ui.navigate.to("/alarms"))
            archive_button = ui.button(tr("archive"), on_click=lambda: ui.navigate.to("/archive"))
        if NTFY_TOPIC is not None:
            ui.separator()
            ntfy_label = ui.label(tr("phone_registration").format(NTFY_TOPIC=NTFY_TOPIC)).classes("text-sm text-gray-600")

    refresh_status()
    refresh_plots()
    ui.timer(LED_REFRESH_PERIOD_SECONDS, refresh_status)
    ui.timer(LED_REFRESH_PERIOD_SECONDS, refresh_scheme_values)
    ui.timer(PLOT_REFRESH_PERIOD_SECONDS, refresh_plots)


# ---------------------------------------------------------------------
# SVG Scheme
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
            .then(response => {
                if (!response.ok) {
                    throw new Error(`scheme-values HTTP ${response.status}`);
                }
                return response.json();
            })
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
            })
            .catch(error => {
                console.warn('Could not refresh scheme values:', error);
            });
    """)


@app.get("/api/scheme-values")
def api_scheme_values() -> dict[str, dict[str, str]]:
    sensors = list(set(SCHEME_PLACEHOLDERS.values()))

    try:
        latest_values = get_last_sensor_values(sensors)
    except Exception as exc:
        print(f"Could not build scheme values response: {exc}")
        latest_values = {}

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
# Alarm table UI
# ---------------------------------------------------------------------


ALARM_TIMESPANS_SECONDS = {
    "day": 24 * 3600,
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
    "year": 365 * 24 * 3600,
    "all": None,
}


def get_alarm_rows(timespan_key: str, language: str, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch alarm rows for the selected timespan.

    On database errors, return an empty table.
    """
    now_timestamp = int(datetime.now(tz=LOCAL_TZ).timestamp())
    timespan_seconds = ALARM_TIMESPANS_SECONDS[timespan_key]

    try:
        with db_connection() as connection:
            with closing(connection.cursor()) as cursor:
                if timespan_seconds is None:
                    cursor.execute(
                        """
                        SELECT `timestamp`, sensor, value, transition
                        FROM alarms
                        ORDER BY `timestamp` DESC, id DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                else:
                    start_timestamp = now_timestamp - timespan_seconds
                    cursor.execute(
                        """
                        SELECT `timestamp`, sensor, value, transition
                        FROM alarms
                        WHERE `timestamp` >= %s
                        ORDER BY `timestamp` DESC, id DESC
                        LIMIT %s
                        """,
                        (start_timestamp, limit),
                    )

                rows = cursor.fetchall()

    except MySQLError as exc:
        print(f"Could not fetch alarm rows: {exc}")
        return []

    formatted_rows = []

    for index, row in enumerate(rows):
        timestamp, sensor_name, value_str, transition = row
        sensor_name = str(sensor_name)

        if sensor_name not in SIGNAL_TABLE:
            print(f"Skipping alarm row for unknown sensor {sensor_name!r}")
            continue

        try:
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
                    "value": sensor.format(value),
                }
            )

        except Exception as exc:
            print(
                f"Skipping invalid alarm row for sensor={sensor_name!r}, "
                f"timestamp={timestamp!r}, value={value_str!r}, "
                f"transition={transition!r}: {exc}"
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
            back_button = ui.button(tr("back"),on_click=lambda: ui.navigate.to("/"))


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
# Archive plotter UI
# ---------------------------------------------------------------------


@ui.page("/archive")
def archive_page(request: Request) -> None:
    """Archived monitored-data viewer page.

    English-only page for plotting archived rows from monitored_data_archive.
    """

    ARCHIVE_TABLE = "monitored_data_archive"
    CURRENT_TABLE = "monitored_data"
    LANGUAGE = "en"

    now = datetime.now(tz=LOCAL_TZ)
    today = now.date()
    default_start = datetime.fromtimestamp(
        int(now.timestamp()) - 24 * 3600,
        tz=LOCAL_TZ,
    ).date()

    state: dict[str, Any] = {
        "start_date": default_start.isoformat(),
        "end_date": today.isoformat(),
    }

    plot_items: list[dict[str, Any]] = []

    def signal_options() -> dict[str, str]:
        return {
            sensor_name: SIGNAL_TABLE[sensor_name].description(LANGUAGE)
            for sensor_name in sorted(
                SIGNAL_TABLE.keys(),
                key=lambda name: SIGNAL_TABLE[name].description(LANGUAGE),
            )
        }

    def sensor_unit(sensor_name: str) -> str:
        """Return the sensor unit when the sensor class defines one."""
        return str(getattr(SIGNAL_TABLE[sensor_name], "__unit__", "") or "")

    def yaxis_label(sensor_name: str) -> str:
        description = SIGNAL_TABLE[sensor_name].description(LANGUAGE)
        unit = sensor_unit(sensor_name)
        return f"{description} ({unit})" if unit else description

    def parse_day_window() -> tuple[Optional[int], Optional[int]]:
        """Convert selected YYYY-MM-DD dates to an inclusive local-day timestamp window."""
        try:
            start_day = datetime.fromisoformat(state["start_date"]).date()
            end_day = datetime.fromisoformat(state["end_date"]).date()
        except ValueError:
            ui.notify("Invalid date selection", type="negative")
            return None, None

        if end_day < start_day:
            ui.notify("The end date must be after the start date", type="negative")
            return None, None

        start_dt = datetime(
            start_day.year,
            start_day.month,
            start_day.day,
            0,
            0,
            0,
            tzinfo=LOCAL_TZ,
        )
        end_dt = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            23,
            59,
            59,
            tzinfo=LOCAL_TZ,
        )

        return int(start_dt.timestamp()), int(end_dt.timestamp())

    def get_archive_sensor_points(
        sensor_name: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> list[tuple[int, float]]:
        """Fetch all archived points for one sensor and selected day window."""
        points: list[tuple[int, float]] = []

        try:
            with db_connection() as connection:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        f"""
                        SELECT `timestamp`, value
                        FROM {ARCHIVE_TABLE}
                        WHERE sensor = %s
                          AND `timestamp` BETWEEN %s AND %s
                        ORDER BY `timestamp`
                        """,
                        (sensor_name, start_timestamp, end_timestamp),
                    )
                    rows = cursor.fetchall()

                    cursor.execute(
                        f"""
                        SELECT `timestamp`, value
                        FROM {CURRENT_TABLE}
                        WHERE sensor = %s
                        AND `timestamp` BETWEEN %s AND %s
                        ORDER BY `timestamp`
                        """,
                        (sensor_name, start_timestamp, end_timestamp),
                    )
                    rows += cursor.fetchall()

        except MySQLError as exc:
            print(f"Could not fetch archived plot data for {sensor_name!r}: {exc}")
            return points

        sensor = SIGNAL_TABLE[sensor_name]

        for timestamp, value_str in rows:
            try:
                points.append((int(timestamp), sensor.value(value_str)))
            except Exception as exc:
                print(
                    f"Skipping invalid archived row for sensor={sensor_name!r}, "
                    f"timestamp={timestamp!r}, value={value_str!r}: {exc}"
                )

        return points

    def make_archive_figure(sensor_name: str) -> go.Figure:
        """Build one archived signal Plotly figure."""
        start_timestamp, end_timestamp = parse_day_window()

        figure = go.Figure()

        if start_timestamp is None or end_timestamp is None:
            points = []
        else:
            points = get_archive_sensor_points(
                sensor_name=sensor_name,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )

        x_values = [
            datetime.fromtimestamp(timestamp, tz=LOCAL_TZ)
            for timestamp, _ in points
        ]
        y_values = [value for _, value in points]

        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=y_values,
                mode="lines",
                hovertemplate="%{x|%Y-%m-%d %H:%M:%S}<br>%{y}<extra></extra>",
            )
        )

        figure.update_layout(
            title=SIGNAL_TABLE[sensor_name].description(LANGUAGE),
            xaxis_title="Time",
            yaxis_title=yaxis_label(sensor_name),
            margin={"l": 60, "r": 20, "t": 50, "b": 50},
            showlegend=False,
            xaxis={
                "tickformat": "%Y-%m-%d<br>%H:%M",
                "hoverformat": "%Y-%m-%d %H:%M:%S",
            },
            template="plotly_white",
            height=360,
        )

        return figure

    def refresh_plot(plot_state: dict[str, Any]) -> None:
        sensor_name = str(plot_state["select"].value)
        plot_state["figure"].figure = make_archive_figure(sensor_name)
        plot_state["figure"].update()

    def refresh_all_plots() -> None:
        state["start_date"] = str(start_date_input.value)
        state["end_date"] = str(end_date_input.value)

        for plot_state in list(plot_items):
            refresh_plot(plot_state)

    def remove_plot(plot_state: dict[str, Any]) -> None:
        if plot_state in plot_items:
            plot_items.remove(plot_state)

        plot_state["card"].delete()

    def add_plot() -> None:
        options = signal_options()
        default_sensor = next(iter(options.keys()))

        with plots_column:
            card = ui.card().classes("w-full p-4 gap-3")

            plot_state: dict[str, Any] = {}

            with card:
                with ui.row().classes("w-full items-end gap-3"):
                    signal_select = ui.select(
                        options,
                        value=default_sensor,
                        label="Signal",
                    ).classes("grow min-w-80")

                    remove_button = ui.button(
                        "Remove",
                        color="negative",
                    ).props("outline")

                plot_widget = ui.plotly(
                    make_archive_figure(default_sensor)
                ).classes("w-full")

            plot_state.update(
                {
                    "card": card,
                    "select": signal_select,
                    "figure": plot_widget,
                }
            )

            signal_select.on(
                "update:model-value",
                lambda _event, item=plot_state: refresh_plot(item),
            )
            remove_button.on_click(lambda item=plot_state: remove_plot(item))

            plot_items.append(plot_state)

    with ui.column().classes("w-full p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Archive").classes("text-2xl font-bold")
            ui.button("Back",on_click=lambda: ui.navigate.to("/")).props("outline")

        with ui.row().classes("w-full items-end justify-between gap-3"):
            with ui.row().classes("items-end gap-3"):
                start_date_input = ui.input(
                    "From",
                    value=state["start_date"],
                ).props("type=date").classes("w-44")

                end_date_input = ui.input(
                    "To",
                    value=state["end_date"],
                ).props("type=date").classes("w-44")

            ui.button("Refresh", on_click=refresh_all_plots)

        plots_column = ui.column().classes("w-full gap-4")

        ui.button("Add plot", on_click=add_plot).props("outline")

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
    create_database_if_needed()
    create_tables_if_needed()
    refresh_sensor_description_table()
    archive_old_rows_if_due(force=True)
    
app.on_startup(startup)

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="Waterloop",
    )
