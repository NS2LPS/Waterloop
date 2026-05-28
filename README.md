# Water Loop Monitor

A small FastAPI/NiceGUI application for monitoring water-loop sensor values on a local network. It receives sensor readings through an HTTP API, stores them in MySQL, displays live status indicators and plots, updates an SVG process scheme, records alarm transitions, and can send ntfy notifications.

The app is intended for a small deployment such as a lab or technical room dashboard. It is suitable for a local network with a limited number of viewers, provided the plot history and database retention settings are kept reasonable.

## Main features

- NiceGUI dashboard at `/`.
- LED-style status cards for selected validating sensors.
- Live SVG water-loop scheme with injected sensor values.
- Plotly time-series graphs for temperatures, pressure, and flow.
- Data ingestion API at `POST /api/data`.
- Alarm history page at `/alarms`.
- Scheme-values API at `GET /api/scheme-values`.
- MySQL storage with connection pooling.
- Live tables plus archive tables for old measurements and alarms.
- Optional API-token protection for ingestion.
- Optional ntfy notifications for new alarm transitions.
- English/French UI strings through `languages.py`.

## Source files

Typical file roles:

| File | Purpose |
| --- | --- |
| `waterloop_app.py` | Main NiceGUI/FastAPI application, MySQL setup, routes, alarm logic, dashboard, archive logic. |
| `signals.py` | Sensor configuration: declared sensors, units, valid ranges/states, and `SIGNAL_TABLE`. |
| `sensors.py` | Sensor classes used by `signals.py`: parsing, formatting, validation, alarm messages. |
| `languages.py` | English/French translation table. |
| `water_loop_scheme_en.svg` | English SVG process scheme. |
| `water_loop_scheme_fr.svg` | French SVG process scheme. |
| `.env` | Local configuration and secrets. Do not commit this file if it contains passwords or tokens. |

## Configuration overview

Runtime settings are loaded from environment variables or from a `.env` file next to `waterloop_app.py`.

All environment variables use the `WATERLOOP_` prefix. For example, the Python setting `db_host` is configured as `WATERLOOP_DB_HOST`.

The app uses `pydantic-settings`, so values in the real environment override values in `.env`.

## Example `.env`

```bash
# MySQL connection
WATERLOOP_DB_HOST=127.0.0.1
WATERLOOP_DB_PORT=3306
WATERLOOP_DB_USER=waterloop
WATERLOOP_DB_PASSWORD=change-me
WATERLOOP_DB_NAME=waterloop

# Database creation and connection pool
WATERLOOP_CREATE_DATABASE_IF_NEEDED=true
WATERLOOP_DB_POOL_SIZE=10

# Retention and archiving
# For 1-minute logging and 24h max plots, 4 days is usually enough.
WATERLOOP_MONITORED_DATA_RETENTION_DAYS=4
WATERLOOP_ALARMS_RETENTION_DAYS=365
WATERLOOP_ARCHIVE_ROLLOVER_PERIOD_SECONDS=21600
WATERLOOP_ARCHIVE_BATCH_SIZE=10000

# API ingestion token
# Leave empty only on a fully trusted local network.
WATERLOOP_API_TOKEN=change-me-long-random-token

# ntfy notifications
# Leave WATERLOOP_NTFY_TOPIC empty to disable notifications.
WATERLOOP_NTFY_SERVER=https://ntfy.sh
WATERLOOP_NTFY_TOPIC=
WATERLOOP_NTFY_PRIORITY=urgent
```

## `.env` parameters

### MySQL connection

| Variable | Default | Meaning |
| --- | --- | --- |
| `WATERLOOP_DB_HOST` | `127.0.0.1` | MySQL server hostname or IP address. |
| `WATERLOOP_DB_PORT` | `3306` | MySQL TCP port. |
| `WATERLOOP_DB_USER` | `waterloop` | MySQL user used by the application. |
| `WATERLOOP_DB_PASSWORD` | empty | MySQL password. |
| `WATERLOOP_DB_NAME` | `waterloop` | MySQL database name. |
| `WATERLOOP_CREATE_DATABASE_IF_NEEDED` | `true` | If true, the app tries to create the database at startup. |
| `WATERLOOP_DB_POOL_SIZE` | `10` | Number of MySQL connections in the application pool. |
| `WATERLOOP_ALARM_HOLDOFF_MINUTES` | `10` | Minimum delay before recording or notifying another alarm event for the same sensor. |

For production or semi-production use, prefer creating the database manually and granting only the required privileges to the application user. Then set:

```bash
WATERLOOP_CREATE_DATABASE_IF_NEEDED=false
```

### Retention and archiving

| Variable | Default | Meaning |
| --- | ---: | --- |
| `WATERLOOP_MONITORED_DATA_RETENTION_DAYS` | `180` | Number of days kept in the live `monitored_data` table before rows are moved to `monitored_data_archive`. |
| `WATERLOOP_ALARMS_RETENTION_DAYS` | `365` | Number of days kept in the live `alarms` table before rows are moved to `alarms_archive`. |
| `WATERLOOP_ARCHIVE_ROLLOVER_PERIOD_SECONDS` | `21600` | Minimum delay between archive runs. Default is 6 hours. |
| `WATERLOOP_ARCHIVE_BATCH_SIZE` | `10000` | Maximum number of old rows moved per table per archive run. |

For 1-minute logging, a 24-hour maximum plot range, and a small local deployment, a practical live-data retention is:

```bash
WATERLOOP_MONITORED_DATA_RETENTION_DAYS=4
```

This keeps enough margin for recent debugging while keeping the live table small. Older rows are kept in the archive table.

Archive rollover is not a separate cron job. It runs:

- at application startup, and
- after successful `POST /api/data` ingestion when the rollover period has elapsed.

### API token

| Variable | Default | Meaning |
| --- | --- | --- |
| `WATERLOOP_API_TOKEN` | empty / unset | Optional token required by `POST /api/data`. |

When `WATERLOOP_API_TOKEN` is set, clients must send:

```text
X-API-Token: your-token-here
```

When it is empty or unset, the ingestion endpoint accepts requests without a token. That is convenient for testing, but any device that can reach the app can inject readings. For a real local-network deployment, set a token.

### ntfy notifications

| Variable | Default | Meaning |
| --- | --- | --- |
| `WATERLOOP_NTFY_SERVER` | `https://ntfy.sh` | ntfy server URL. |
| `WATERLOOP_NTFY_TOPIC` | empty / unset | ntfy topic. If empty, notifications are disabled. |
| `WATERLOOP_NTFY_PRIORITY` | `urgent` | ntfy priority header. |

When enabled, the app sends a notification for new alarm transitions. The current notification logic skips `transition == 0`, so back-to-normal events are stored in MySQL but are not sent to ntfy.

If alarm messages are sensitive, use a private random topic, restrict access, or self-host ntfy.

## MySQL setup

Install and start MySQL or MariaDB, then create a database and user. Example:

```sql
CREATE DATABASE waterloop
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'waterloop'@'localhost' IDENTIFIED BY 'change-me';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON waterloop.*
TO 'waterloop'@'localhost';

FLUSH PRIVILEGES;
```

If the app should create tables but not create the database itself, use:

```bash
WATERLOOP_CREATE_DATABASE_IF_NEEDED=false
```

If `WATERLOOP_CREATE_DATABASE_IF_NEEDED=true`, the app connects without selecting a database first and runs `CREATE DATABASE IF NOT EXISTS`. In that case, the MySQL user needs enough privilege to create or access the configured database.

## Database tables

The app creates tables automatically at startup.

### `monitored_data`

Stores accepted raw sensor readings.

| Column | Type | Description |
| --- | --- | --- |
| `id` | `BIGINT UNSIGNED AUTO_INCREMENT` | Primary key. |
| `timestamp` | `BIGINT NOT NULL` | Server receive time as Unix epoch seconds. |
| `sensor` | `VARCHAR(128) NOT NULL` | Internal sensor name, for example `primary_pressure_1`. |
| `value` | `VARCHAR(255) NOT NULL` | Raw posted value as text. |

Indexes:

- primary key on `id`
- `(sensor, timestamp DESC, id DESC)` for latest/range queries
- `(timestamp)` for retention/archive queries

### `alarms`

Stores alarm transition events.

| Column | Type | Description |
| --- | --- | --- |
| `id` | `BIGINT UNSIGNED AUTO_INCREMENT` | Primary key. |
| `timestamp` | `BIGINT NOT NULL` | Transition time as Unix epoch seconds. |
| `sensor` | `VARCHAR(128) NOT NULL` | Sensor name. |
| `value` | `VARCHAR(255) NOT NULL` | Raw value at transition. |
| `transition` | `INT NOT NULL` | New state. `0` means back to normal. |
| `acknowledged` | `TINYINT NOT NULL DEFAULT 0` | `0` or `1`. |

Indexes:

- primary key on `id`
- `(sensor, timestamp)`
- `(timestamp)`

### `sensor_states`

Stores the latest known value and state per sensor.

| Column | Type | Description |
| --- | --- | --- |
| `sensor` | `VARCHAR(128)` | Primary key. |
| `last_state` | `INT NOT NULL` | Latest validation state. `0` means normal/valid. |
| `last_timestamp` | `BIGINT NOT NULL` | Timestamp of latest reading. |
| `last_value` | `VARCHAR(255) NOT NULL` | Latest raw value. |

For validating sensors, `last_state` is produced by the sensor's `validate()` method. For non-validating sensors, use state `0` by default so the table can also serve as the fast latest-value source for the SVG scheme.

### `monitored_data_archive`

Archive table for old `monitored_data` rows.

Important columns:

- `original_id`: original live-table ID
- `timestamp`, `sensor`, `value`: copied reading data
- `archived_at`: archive time as Unix epoch seconds

### `alarms_archive`

Archive table for old `alarms` rows.

Important columns:

- `original_id`: original live-table ID
- `timestamp`, `sensor`, `value`, `transition`, `acknowledged`: copied alarm data
- `archived_at`: archive time as Unix epoch seconds

## Startup behavior

Startup is registered with:

```python
app.on_startup(startup)
```

At startup, the app:

1. Validates static configuration.
2. Optionally creates the MySQL database.
3. Creates tables and indexes if needed.
4. Starts an archive rollover check.

Configuration validation checks that:

- required SVG files exist:
  - `water_loop_scheme_en.svg`
  - `water_loop_scheme_fr.svg`
- every sensor listed in `STATUS_LEDS` exists in `SIGNAL_TABLE`
- every sensor listed in `STATUS_LEDS` has a `validate()` method
- every sensor used in `PLOTS` exists in `SIGNAL_TABLE`
- every sensor used in `SCHEME_PLACEHOLDERS` exists in `SIGNAL_TABLE`
- plot legend lengths match their signal lists when a `legend` field is provided

## Running the application

Install dependencies in your Python environment, configure `.env`, then run:

```bash
python waterloop_app.py
```

By default the app starts on:

```text
http://0.0.0.0:8080
```

The server is started with:

```python
ui.run(
    host="0.0.0.0",
    port=8080,
    title="WaterLoop",
)
```

For HTTPS, the recommended deployment is to run NiceGUI on `127.0.0.1:8080` and put Caddy or Nginx in front of it to terminate TLS.

## Data ingestion API

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

Both fields are strings. The server adds the timestamp when the reading is received.

Success response:

```json
{
  "status": "ok",
  "timestamp": 1710000000,
  "sensor": "primary_pressure_1",
  "value": "3.1"
}
```

With token protection enabled:

```python
import requests

url = "http://localhost:8080/api/data"
headers = {"X-API-Token": "your-token-here"}
payload = {"sensor": "primary_pressure_1", "value": "3.1"}

response = requests.post(url, json=payload, headers=headers, timeout=5)
response.raise_for_status()
print(response.json())
```

Expected validation behavior:

| Input | Result |
| --- | --- |
| Unknown sensor | HTTP `400` |
| Malformed value | HTTP `400` |
| Non-finite float such as `nan` or `inf` | HTTP `400` |
| Well-formed in-range value | Stored in `monitored_data`; state updated. |
| Well-formed out-of-range value | Stored; state updated; alarm transition may be inserted. |
| MySQL error | HTTP `503` |

## Alarm model

A validating sensor returns a state code from `validate()`:

| State | Meaning |
| ---: | --- |
| `0` | Normal / valid |
| `1` | Invalid, too low, stopped, or invalid state depending on sensor type |
| `2` | Too high, for range sensors |

Alarm rows are transition events, not the current state. The current state is stored in `sensor_states`.

Typical behavior:

| Previous state | New state | DB behavior |
| ---: | ---: | --- |
| none | `0` | Initialize state; no alarm event. |
| none | non-zero | Initialize state; insert alarm event. |
| `0` | non-zero | Insert alarm event. |
| non-zero | `0` | Insert back-to-normal event. |
| `1` | `2` | Insert alarm-type-change event. |
| same state | same state | Update latest timestamp/value; no duplicate alarm event. |

Alarm retriggering is limited by `WATERLOOP_ALARM_HOLDOFF_MINUTES`, which defaults to 10 minutes. During the holdoff window, new active alarm transitions for the same sensor are suppressed, but live sensor state is still updated. Suppressed transitions do not create MySQL alarm rows and do not send ntfy notifications. Back-to-normal rows are only stored when the latest stored alarm event for that sensor is active.

Important: stale/no-data display state is determined from `last_timestamp`. Unless you add a dedicated stale-alarm checker, a sensor that stops sending data becomes visually stale but does not automatically create a new alarm row or ntfy notification.

## Status colors

The dashboard and SVG use these status names:

| Status | Meaning | Color |
| --- | --- | --- |
| `ok` | Fresh validating sensor, value is valid | Green |
| `alarm` | Fresh validating sensor, value is invalid | Red |
| `stale` | Latest value is older than `STALE_AFTER_SECONDS` | Orange |
| `no_data` | No row or unreadable value | Grey |
| `no_range` | Sensor has no `validate()` method | Black |

The default color map is:

```python
SCHEME_STATUS_COLORS = {
    "ok": "#16a34a",
    "alarm": "#dc2626",
    "stale": "#f97316",
    "no_data": "#9ca3af",
    "no_range": "#000000",
}
```

## Dashboard configuration in `waterloop_app.py`

Some UI configuration is still kept directly in `waterloop_app.py`.

### Refresh intervals

```python
LED_REFRESH_PERIOD_SECONDS = 5
PLOT_REFRESH_PERIOD_SECONDS = 30
STALE_AFTER_SECONDS = 5 * 60
```

`STALE_AFTER_SECONDS` controls when old values turn orange in the UI.

### `STATUS_LEDS`

`STATUS_LEDS` defines the sensors shown as status cards and checked for alarm transitions:

```python
STATUS_LEDS = [
    "secondary_temperature_1",
    "primary_temperature_1",
    "primary_pressure_1",
    "secondary_flow_1",
    "pmp07_state",
]
```

Every sensor in `STATUS_LEDS` must exist in `SIGNAL_TABLE` and must have a `validate()` method.

### `PLOTS`

`PLOTS` defines the dashboard graphs. Each entry contains:

- `title`: translated plot title
- `xlabel`: translated x-axis label
- `ylabel`: translated y-axis label
- `signals`: list of internal sensor names
- `legend`: optional translated trace labels

For 1-minute logging and a local network dashboard, keep the maximum plot timespan at 24 hours unless SQL-side aggregation or caching is added.

### `SCHEME_PLACEHOLDERS`

`SCHEME_PLACEHOLDERS` maps placeholder IDs in the SVG files to sensor names:

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
    "S001": "gf01_state",
    "S002": "gf02_state",
    "S003": "pmp07_state",
}
```

In the SVG, value text elements should use IDs such as:

```xml
<text id="scheme-value-P001">--</text>
```

Optional status markers can use IDs such as:

```xml
<circle id="scheme-status-P001" cx="100" cy="100" r="5" fill="#9ca3af" />
```

The `/api/scheme-values` endpoint returns the value, status, and color for each placeholder.

## `signals.py` as the sensor configuration file

`signals.py` defines the sensors known to the application. It is the main configuration file for sensor names, display descriptions, units, and validity rules.

Each sensor is created from a class in `sensors.py`, then all sensors are registered in `SIGNAL_TABLE`.

The internal names in `SIGNAL_TABLE` are important. They are used by:

- the ingestion API payload field `sensor`
- `STATUS_LEDS`
- `PLOTS`
- `SCHEME_PLACEHOLDERS`
- rows stored in MySQL
- alarm history

### Sensor classes

| Class | Purpose |
| --- | --- |
| `FloatSensor` | Floating-point sensor with a unit and no validation range. |
| `FloatSensorValidRange` | Floating-point sensor with optional minimum and maximum. |
| `StateSensor` | Integer state sensor displayed as ON/OFF, without validation. |
| `StateSensorValid` | Integer state sensor with one valid state. |
| `IntSensorValidValues` | Integer sensor valid only when its value is in a configured list. |

### Current configured sensors

| Sensor name | Type | Unit/display | Validation |
| --- | --- | --- | --- |
| `primary_pressure_1` | `FloatSensorValidRange` | `bar` | `2.0 <= value <= 3.5` |
| `primary_temperature_1` | `FloatSensorValidRange` | `°C` | `value <= 14.0` |
| `primary_temperature_2` | `FloatSensor` | `°C` | none |
| `valve_command` | `FloatSensor` | `%` | none |
| `pmp07_state` | `IntSensorValidValues` | ON/OFF | valid when value is `3` |
| `gf01_state` | `StateSensor` | ON/OFF | none |
| `gf01_temperature_out` | `FloatSensor` | `°C` | none |
| `gf02_state` | `StateSensor` | ON/OFF | none |
| `gf02_temperature_out` | `FloatSensor` | `°C` | none |
| `secondary_temperature_1` | `FloatSensorValidRange` | `°C` | `value <= 18.0` |
| `secondary_temperature_2` | `FloatSensor` | `°C` | none |
| `secondary_flow_1` | `FloatSensorValidRange` | `L/min` | `0 <= value <= 1000` |

### Adding a new sensor

1. Define the sensor object in `signals.py`.
2. Add it to `SIGNAL_TABLE` using the exact internal name that clients will post.
3. If it should create alarm transitions and appear as a status LED, add it to `STATUS_LEDS` in `waterloop_app.py`.
4. If it should be plotted, add it to a `PLOTS` entry.
5. If it should appear on the SVG scheme, add it to `SCHEME_PLACEHOLDERS` and add matching IDs in both SVG files.

Example:

```python
secondary_pressure_1 = sensors.FloatSensorValidRange(
    {"en": "Secondary Pressure", "fr": "Pression secondaire"},
    "bar",
    1.0,
    4.0,
)

SIGNAL_TABLE = {
    # existing sensors...
    "secondary_pressure_1": secondary_pressure_1,
}
```

Then clients can post:

```json
{
  "sensor": "secondary_pressure_1",
  "value": "2.4"
}
```

### Choosing validation rules

Use `FloatSensorValidRange` when a value has numeric limits:

```python
sensors.FloatSensorValidRange(description, unit, min_value, max_value)
```

Use `None` for an open end:

```python
# only maximum enforced
sensors.FloatSensorValidRange(description, "°C", None, 18.0)

# only minimum enforced
sensors.FloatSensorValidRange(description, "L/min", 10.0, None)
```

Use `IntSensorValidValues` when only specific integer states are normal:

```python
sensors.IntSensorValidValues(description, [3])
```

## Plot behavior and performance

Plot data are read from `monitored_data` for the selected time range.

For 1-minute logging:

```text
24 hours × 60 points/hour = 1,440 points per sensor
```

That is small enough for this app. Long ranges such as 1 week or 1 month should be avoided unless you add SQL-side aggregation or server-side caching.

Recommended dashboard options for the current implementation:

```python
def make_timespan_options() -> dict[str, str]:
    return {
        1: "1h",
        12: "12h",
        24: "24h",
    }
```

## Alarm history page

The alarm page is served at:

```text
GET /alarms
```

It displays alarm events from the live `alarms` table with selectable timespans:

- last day
- last week
- last month
- last year
- all live alarm rows

Archived rows are stored in `alarms_archive`. If you want `/alarms` to show archived history too, extend the query to include `alarms_archive`.

## Language support

Translations are defined in `languages.py`.

Supported UI languages:

- English: `en`
- French: `fr`

The initial language is chosen from the browser `Accept-Language` header. The dashboard and alarm page also provide EN/FR buttons.

Plot titles and axis labels are configured in `PLOTS`. For localized x-axis tick formatting, set Plotly `xaxis.tickformat` and `hovertemplate` according to the selected language when building the figure.

## SVG editing workflow

When editing the SVG files:

- Keep placeholder IDs stable, such as `scheme-value-P001`.
- Keep optional status IDs stable, such as `scheme-status-P001`.
- Keep English and French SVG files aligned.
- Do not let untrusted users upload or edit SVG templates, because they are injected into the page as HTML/SVG.

## Security notes

For a small trusted LAN, the app can be run directly, but the following settings are recommended:

- Set `WATERLOOP_API_TOKEN` so random LAN clients cannot inject readings.
- Keep MySQL bound to localhost or a protected management network.
- Use a least-privilege MySQL user.
- Put the app behind a reverse proxy if you need HTTPS.
- Do not commit `.env` to version control.
- Treat ntfy topics as secrets if alarm messages reveal operational information.
