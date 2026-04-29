# Water Loop Monitor

A small FastAPI/NiceGUI application for monitoring water-loop sensor values, displaying live status indicators, plotting recent measurements, updating an SVG process scheme, and recording alarm transitions.

## Overview

The application provides:

- A NiceGUI web dashboard at `/`.
- LED-style status cards for selected validating sensors.
- A live SVG water-loop scheme with sensor values injected into placeholder text elements.
- Plotly time-series graphs for pressure, temperature, and flow.
- A data ingestion API at `POST /api/data`.
- An alarm history page at `/alarms`.
- A scheme-value API at `GET /api/scheme-values`.
- A local SQLite database, `water_loop.db`.
- Optional API-token protection for ingestion.
- Optional ntfy notifications for new alarm events.

The application uses sensor definitions from `sensors.py` and translations from `languages.py`.

## Startup behavior

Startup is handled with NiceGUI's startup hook:

```python
app.on_startup(startup)
```

The startup function performs configuration validation and creates the database tables/indexes if needed.

At startup, the app should validate:

- Required SVG scheme files exist:
  - `water_loop_scheme_en.svg`
  - `water_loop_scheme_fr.svg`
- Every sensor listed in `STATUS_LEDS` exists in `SIGNAL_TABLE`.
- Every sensor listed in `STATUS_LEDS` has a `validate()` method.
- Database tables and indexes exist.

Recommended additional startup checks:

- Every sensor used in `PLOTS` exists in `SIGNAL_TABLE`.
- Every sensor used in `SCHEME_PLACEHOLDERS` exists in `SIGNAL_TABLE`.
- Plot legend lengths match their signal lists when a `legend` field is provided.

The `if __name__ in {"__main__", "__mp_main__"}` block should only start the NiceGUI server with `ui.run(...)`.

## Configuration

Main configuration values are defined in `monitor_app.py`.

### Database

```python
DATABASE_FILE = BASE_DIR / "water_loop.db"
```

### Time zone

```python
LOCAL_TZ = ZoneInfo("Europe/Paris")
```

### API token

By default:

```python
API_TOKEN = None
```

When `API_TOKEN` is `None`, ingestion requests are accepted without a token.

When set to a string, clients must send:

```text
X-API-Token: your-token-here
```

### Refresh intervals

```python
LED_REFRESH_PERIOD_SECONDS = 5
PLOT_REFRESH_PERIOD_SECONDS = 30
STALE_AFTER_SECONDS = 5 * 60
```

A validating sensor whose most recent value is older than `STALE_AFTER_SECONDS` is considered stale.

## Status model and colors

The app uses shared status names for dashboard indicators and SVG scheme coloring.

Recommended status color map:

```python
SCHEME_STATUS_COLORS = {
    "ok": "#16a34a",        # green
    "alarm": "#dc2626",     # red
    "stale": "#f97316",     # orange
    "no_data": "#9ca3af",   # grey
    "no_range": "#000000",  # black
}
```

Status meanings:

| Status | Meaning | Color |
| --- | --- | --- |
| `ok` | Fresh validating sensor, value within valid range | Green |
| `alarm` | Fresh validating sensor, value outside valid range | Red |
| `stale` | Validating sensor has an old value | Orange |
| `no_data` | No value or malformed value | Grey |
| `no_range` | Sensor has no `validate()` method | Black |

Important behavior:

- Stale validating sensors keep displaying their last formatted value, but turn orange.
- Sensors without a valid range stay black, even if their latest value is old.
- Missing data displays `--` in grey.
- Malformed historical data should display `--` in grey.

## Dashboard

The dashboard is served at:

```text
GET /
```

It includes:

- Title and language buttons.
- SVG scheme.
- LED-style status cards.
- Timespan selector.
- Plotly graphs.
- Link/button to the alarm history page.

### LED status cards

`STATUS_LEDS` defines which sensors appear as LED cards:

```python
STATUS_LEDS = [
    "secondary_temperature_1",
    "primary_pressure_1",
    "primary_temperature_1",
    "secondary_flow_1",
    "pmp07_state",
]
```

Every sensor in `STATUS_LEDS` must exist in `SIGNAL_TABLE` and must have a `validate()` method.

The LED indicator helper should accept status names, not legacy color names:

```python
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
```

Initial indicators should be created with:

```python
make_indicator_html("no_data")
```

not with legacy strings such as `"grey"`.

## SVG scheme

The SVG scheme files are loaded from:

```python
SCHEME_SVG_TEMPLATE_FILES = {
    "en": BASE_DIR / "water_loop_scheme_en.svg",
    "fr": BASE_DIR / "water_loop_scheme_fr.svg",
}
```

The SVG placeholders are mapped to internal sensor names:

```python
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
}
```

In the SVG, text elements should have IDs like:

```xml
<text id="scheme-value-P001">--</text>
```

Optional status markers can be added with IDs like:

```xml
<circle id="scheme-status-P001" cx="100" cy="100" r="5" fill="#9ca3af" />
```

The JavaScript updater can then update both:

- `scheme-value-P001`
- `scheme-status-P001`

### Scheme value refresh

The scheme refresh JavaScript should expect structured objects:

```python
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
```

## Scheme values API

The scheme values endpoint is:

```text
GET /api/scheme-values
```

It returns a structured object per SVG placeholder.

Example response:

```json
{
  "P001": {
    "value": "2.8 bar",
    "status": "ok",
    "color": "#16a34a"
  },
  "T001": {
    "value": "7.2 °C",
    "status": "no_range",
    "color": "#000000"
  },
  "D001": {
    "value": "420.0 L/min",
    "status": "stale",
    "color": "#f97316"
  },
  "V001": {
    "value": "35.0 %",
    "status": "no_range",
    "color": "#000000"
  }
}
```

Recommended implementation:

```python
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
```

## Latest sensor values helper

`get_last_sensor_values()` should return structured value/status/color dictionaries.

Recommended implementation:

```python
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
```

## Plots

`PLOTS` defines the dashboard graphs.

Current plot groups:

- Loop temperatures:
  - `primary_temperature_1`
  - `secondary_temperature_1`
- Primary pressure:
  - `primary_pressure_1`
- Secondary flow:
  - `secondary_flow_1`

Each plot can define:

- `title`
- `xlabel`
- `ylabel`
- `signals`
- optional `legend`

Plot data are queried from `monitored_data` over the selected timespan.

## Database structure

The app uses a local SQLite database:

```text
water_loop.db
```

Tables are created by `create_tables_if_needed()`.

### `monitored_data`

Stores all accepted sensor readings.

| Column | Type | Description |
| --- | --- | --- |
| `timestamp` | `INTEGER NOT NULL` | Unix timestamp in seconds |
| `sensor` | `TEXT NOT NULL` | Sensor identifier |
| `value` | `TEXT NOT NULL` | Raw posted sensor value |

Notes:

- Values are stored as text because the ingestion payload accepts strings.
- Values are parsed by the corresponding sensor object when displayed, plotted, or validated.
- Unknown sensors should be rejected before insertion.
- Malformed values should be rejected before insertion.

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_monitored_data_sensor_timestamp
ON monitored_data (sensor, timestamp);

CREATE INDEX IF NOT EXISTS idx_monitored_data_timestamp
ON monitored_data (timestamp);
```

### `alarms`

Stores alarm transition events.

| Column | Type | Description |
| --- | --- | --- |
| `timestamp` | `INTEGER NOT NULL` | Unix timestamp in seconds |
| `sensor` | `TEXT NOT NULL` | Sensor identifier |
| `value` | `TEXT NOT NULL` | Raw value at transition |
| `transition` | `INTEGER NOT NULL` | New alarm state; `0` means back to normal |
| `acknowledged` | `INTEGER NOT NULL DEFAULT 0` | `0` or `1` |

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_alarms_sensor_timestamp
ON alarms (sensor, timestamp);

CREATE INDEX IF NOT EXISTS idx_alarms_timestamp
ON alarms (timestamp);
```

### `sensor_states`

Stores the latest known validation state for each validating status sensor.

| Column | Type | Description |
| --- | --- | --- |
| `sensor` | `TEXT PRIMARY KEY` | Sensor identifier |
| `last_state` | `INTEGER NOT NULL` | `0` if valid; non-zero if invalid |
| `last_timestamp` | `INTEGER NOT NULL` | Timestamp of the latest reading |
| `last_value` | `TEXT NOT NULL` | Raw latest value |

Index:

```sql
CREATE INDEX IF NOT EXISTS idx_sensor_states_last_timestamp
ON sensor_states (last_timestamp);
```

## Sensor definitions

Sensors are defined in `sensors.py` and collected in `SIGNAL_TABLE`.

Current known sensors:

| Sensor name | Type | Validation |
| --- | --- | --- |
| `primary_pressure_1` | Float, bar | Valid range: `2.0` to `3.5` |
| `primary_temperature_1` | Float, °C | Valid range: `5.0` to `15.0` |
| `primary_temperature_2` | Float, °C | No range |
| `valve_command` | Float, % | No range |
| `pmp07_state` | Integer | Valid values: `[3]` |
| `gf01_state` | State/int | No range |
| `gf01_temperature_out` | Float, °C | No range |
| `gf02_state` | State/int | No range |
| `gf02_temperature_out` | Float, °C | No range |
| `secondary_temperature_1` | Float, °C | Valid range: `13.5` to `20.5` |
| `secondary_temperature_2` | Float, °C | No range |
| `secondary_flow_1` | Float, L/min | Valid range: `0` to `1000` |

## Sensor value parsing

Sensor classes parse raw string values using their `value()` method.

Recommended behavior:

- Float sensors should reject malformed values.
- Float sensors should reject `nan`, `inf`, and `-inf`.
- Integer sensors should reject non-integer strings such as `"3.0"`.
- Malformed API values should be rejected before insertion into `monitored_data`.

Recommended helpers in `sensors.py`:

```python
import math


def parse_float_value(raw_value: str, sensor_name: str) -> float:
    try:
        value = float(str(raw_value).strip())
    except ValueError as exc:
        raise ValueError(f"Invalid float value for {sensor_name}: {raw_value!r}") from exc

    if not math.isfinite(value):
        raise ValueError(f"Invalid float value for {sensor_name}: {raw_value!r}")

    return value


def parse_int_value(raw_value: str, sensor_name: str) -> int:
    raw_text = str(raw_value).strip()

    if raw_text == "":
        raise ValueError(f"Invalid integer value for {sensor_name}: {raw_value!r}")

    if not raw_text.lstrip("+-").isdigit():
        raise ValueError(f"Invalid integer value for {sensor_name}: {raw_value!r}")

    return int(raw_text)
```

## Posting data

Sensor readings are posted to:

```text
POST /api/data
```

Request body:

```json
{
  "sensor": "primary_pressure_1",
  "value": "3.1"
}
```

Both fields are strings. The server adds the timestamp automatically when the reading is received.

### Validation before insertion

The ingestion endpoint should reject invalid data before inserting into `monitored_data`.

Expected behavior:

| Input | Result |
| --- | --- |
| Unknown sensor | Rejected with HTTP `400` |
| Malformed value | Rejected with HTTP `400` |
| Well-formed in-range value | Inserted |
| Well-formed out-of-range value | Inserted and alarm-checked |
| SQLite error | HTTP `503` |

Relevant logic:

```python
if payload.sensor not in SIGNAL_TABLE:
    raise HTTPException(
        status_code=400,
        detail=f"Unknown sensor: {payload.sensor}",
    )

sensor = SIGNAL_TABLE[payload.sensor]

try:
    sensor.value(payload.value)
except ValueError as exc:
    raise HTTPException(
        status_code=400,
        detail=str(exc),
    ) from exc
```

Only after this validation should the row be inserted.

### Python example

```python
import requests

url = "http://localhost:8080/api/data"

payload = {
    "sensor": "primary_pressure_1",
    "value": "3.1",
}

response = requests.post(url, json=payload, timeout=5)
response.raise_for_status()

print(response.json())
```

With API token enabled:

```python
import requests

url = "http://localhost:8080/api/data"

headers = {
    "X-API-Token": "your-token-here",
}

payload = {
    "sensor": "primary_pressure_1",
    "value": "3.1",
}

response = requests.post(url, json=payload, headers=headers, timeout=5)
response.raise_for_status()

print(response.json())
```

Example success response:

```json
{
  "status": "ok",
  "timestamp": 1710000000,
  "sensor": "primary_pressure_1",
  "value": "3.1"
}
```

## Alarm detection

Alarm detection runs for sensors listed in `STATUS_LEDS`.

A validating sensor returns:

- `0` for valid/normal.
- Non-zero values for invalid/alarm states.

For range sensors:

- `1` means too low.
- `2` means too high.

For valid-value integer sensors:

- `1` means invalid state.

Recommended transition logic:

```python
if previous_state is not None and previous_state != new_state:
    transition = new_state
    acknowledged = 1 if new_state == 0 else 0
```

This records:

| Previous state | New state | Meaning |
| ---: | ---: | --- |
| `0` | `1` or `2` | Alarm starts |
| `1` or `2` | `0` | Back to normal |
| `1` | `2` | Alarm type changed |
| `2` | `1` | Alarm type changed |

The first reading initializes `sensor_states`. If you want alarms to be generated when the very first reading is invalid, add explicit logic for `previous_state is None and new_state != 0`.

## Notifications

Notifications are sent through ntfy when enabled:

```python
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = None
NTFY_PRIORITY = "urgent"
```

When `NTFY_TOPIC` is `None`, notifications are disabled.

The current notification function skips recovery notifications when:

```python
transition == 0
```

so back-to-normal events are stored in the database but are not sent to ntfy unless this behavior is changed.

Recommended ordering:

1. Insert alarm row.
2. Commit database transaction.
3. Send notification.

This avoids sending a notification for an alarm row that failed to commit.

## Alarm history page

The alarm page is served at:

```text
GET /alarms
```

It displays recent alarm events with selectable timespans:

- Last day
- Last week
- Last month
- Last year
- All

The page is bilingual English/French.

Rows with `transition_code > 0` are styled as active alarm transitions.

Rows with `transition_code == 0` are styled as return-to-normal transitions.

## Language support

Translations are defined in `languages.py`.

Supported languages:

- English: `en`
- French: `fr`

Language is selected from the browser `Accept-Language` header and can be changed with dashboard buttons.

## Running the application

Install dependencies according to your environment, then run:

```bash
python monitor_app.py
```

By default the app starts on:

```text
http://localhost:8080
```

The NiceGUI server is configured with:

```python
ui.run(
    host="0.0.0.0",
    port=8080,
    title="WaterLoop",
)
```

## Operational notes

### Security

`API_TOKEN` is disabled by default. For any deployment reachable outside a trusted network, enable token protection.

A good production pattern is to load secrets from environment variables instead of hard-coding them.

### Database growth

`monitored_data` grows indefinitely. For long-running deployments, consider adding:

- Retention cleanup.
- Downsampling.
- Archival.
- Periodic vacuuming if appropriate.

### SQLite concurrency

SQLite is suitable for a small local monitor, but alarm transition logic can race if multiple concurrent requests update the same sensor at the same time.

For stronger consistency, wrap the alarm state read/update/insert sequence in an explicit write transaction, for example using `BEGIN IMMEDIATE`.

### Historical malformed rows

The API should reject malformed future values. If malformed rows already exist from an older version, display/plot helpers should skip or degrade gracefully instead of crashing.

## Suggested SVG editing workflow

When editing the SVG in Inkscape:

- Keep placeholder text element IDs stable, such as `scheme-value-P001`.
- Optional color markers should use IDs such as `scheme-status-P001`.
- Use “Paste in Place” to copy elements between language SVG files at the same coordinates.
- If scaling the whole drawing, scale strokes and fonts as well.
- Keep English and French SVG files aligned so the same placeholder IDs exist in both.
