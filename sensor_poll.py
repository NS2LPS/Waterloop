import asyncio
import json
import math
import os
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from asyncua import Client
from asyncua.ua import AttributeIds, ReadParameters, ReadValueId
from dotenv import load_dotenv


POLL_PERIOD_SECONDS = 60
SECONDARY_WATER_LOOP_URL = "http://adm-uc-a.lps.u-psud.fr/data"
SECONDARY_WATER_LOOP_FLOW_URL = "http://adm-uc-b.lps.u-psud.fr/data"
PRIMARY_PRESSURE_URL = "http://192.168.142.126:8080"

# NiceGUI/FastAPI monitor app POST endpoint.
MONITOR_APP_POST_URL = "http://127.0.0.1:8080/api/data"

# Load local .env file containing WATERLOOP_API_TOKEN=...
load_dotenv(Path(__file__).resolve().with_name(".env"))

# Must match API_TOKEN in monitor_app_v1.py.
# Leave WATERLOOP_API_TOKEN unset if the app has API_TOKEN = None.
API_TOKEN = os.getenv("WATERLOOP_API_TOKEN") or None

# OPC UA settings.
ENDPOINT = "opc.tcp://129.175.113.201:49320"
APPLICATION_URI = "urn:water-loop:python-client"

# The OPC UA connection is intentionally made without a local client
# certificate/key. Do not call set_security_string(...) unless the server
# is reconfigured to require certificate-based secure channels.

# Internal monitor-app sensor name -> OPC UA node id.
OPCUA_NODES = {
    "primary_temperature_1": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.EA.T_DEP",
        "type" : "float",
        "scale": 0.1,
    },
    "primary_temperature_2": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.EA.T_RET",
        "type" : "float",
        "scale": 0.1,
    },
    "gf01_temperature_out": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.GF01.EA.T_SORTIE",
        "type" : "float",
        "scale": 0.1,
    },
    "gf02_temperature_out": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.GF02.EA.T_SORTIE",
        "type" : "float",
        "scale": 0.1,
    },
    "gf01_state": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.GF01.ETAT",
        "type" : "int"
    },
    "gf02_state": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.GF02.ETAT",
        "type" : "int"
    },
    "pmp07_state": {
        "node_id": "ns=2;s=ARM06.Device1.RES.FR.PMP07.ETAT",
        "type" : "int"
    },
    "secondary_temperature_1": {
        "node_id": "ns=2;s=ARM01.Device1.RES.FR.ECH02.EA.T_DEP_SEC",
        "type" : "float",
        "scale": 0.1,
    },
    "valve_command": {
        "node_id": "ns=2;s=ARM01.Device1.RES.FR.ECH02.VMF.SA.CMD",
        "type" : "float",
        "scale": 0.1,
    },
    "external_temperature_1": {
        "node_id": "ns=2;s=ARM01A.Device1.GEN.EA.T_EXT",
        "type" : "float",
        "scale": 0.1,
    },
}


def read_secondary_water_loop_temperature() -> dict[str, float]:
    """
    Poll the secondary water-loop temperature from an HTTP endpoint.

    Returns:
        {
            "secondary_temperature_1": 19.36
        }

    Raises:
        RuntimeError if the HTTP request fails, the field is missing,
        or the value is invalid.
    """
    http_request = urlrequest.Request(
        SECONDARY_WATER_LOOP_URL,
        headers={
            "User-Agent": "WaterLoopMonitor/1.0",
            "Connection": "close",
        },
    )

    try:
        with urlrequest.urlopen(http_request, timeout=5) as response:
            response_text = response.read().decode("utf-8", errors="replace")

    except HTTPError as exc:
        raise RuntimeError(
            f"HTTP error while reading secondary water-loop temperature: "
            f"{exc.code} {exc.reason}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(
            f"Network error while reading secondary water-loop temperature: "
            f"{exc.reason}"
        ) from exc

    except ConnectionResetError as exc:
        raise RuntimeError(
            "Connection reset by peer while reading secondary water-loop temperature"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "Timeout while reading secondary water-loop temperature"
        ) from exc

    for item in response_text.split():
        if not item.startswith("WATER_LOOP_TEMPERATURE:"):
            continue

        _, value_text = item.split(":", maxsplit=1)

        try:
            value = float(value_text)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid WATER_LOOP_TEMPERATURE value: {value_text!r}"
            ) from exc

        if math.isnan(value):
            raise RuntimeError("WATER_LOOP_TEMPERATURE is nan")

        return {"secondary_temperature_2": f"{value:.2f}"}

    raise RuntimeError("WATER_LOOP_TEMPERATURE not found in sensor response")

 

def read_secondary_water_loop_flow() -> dict[str, float]:
    """
    Poll the secondary water-loop flow from an HTTP endpoint.

    Returns:
        {
            "secondary_flow_1": 128.1
        }

    Raises:
        RuntimeError if the HTTP request fails, the field is missing,
        or the value is invalid.
    """
    http_request = urlrequest.Request(
        SECONDARY_WATER_LOOP_FLOW_URL,
        headers={
            "User-Agent": "WaterLoopMonitor/1.0",
            "Connection": "close",
        },
    )

    try:
        with urlrequest.urlopen(http_request, timeout=5) as response:
            response_text = response.read().decode("utf-8", errors="replace")

    except HTTPError as exc:
        raise RuntimeError(
            f"HTTP error while reading secondary water-loop flow: "
            f"{exc.code} {exc.reason}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(
            f"Network error while reading secondary water-loop flow: "
            f"{exc.reason}"
        ) from exc

    except ConnectionResetError as exc:
        raise RuntimeError(
            "Connection reset by peer while reading secondary water-loop flow"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "Timeout while reading secondary water-loop flow"
        ) from exc

    for item in response_text.split():
        if not item.startswith("WATER_LOOP_FLOW:"):
            continue

        _, value_text = item.split(":", maxsplit=1)

        try:
            value = float(value_text)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid WATER_LOOP_FLOW value: {value_text!r}"
            ) from exc

        if math.isnan(value):
            raise RuntimeError("WATER_LOOP_FLOW is nan")

        return {"secondary_flow_1": f"{value:.2f}"}

    raise RuntimeError("WATER_LOOP_FLOW not found in sensor response")
   

def read_primary_pressure() -> dict[str, float]:
    """
    Poll the primary circuit pressure from an HTTP endpoint.

    Returns:
        {
            "primary_pressure_1": 2.8
        }

    Raises:
        RuntimeError if the HTTP request fails, the field is missing,
        or the value is invalid.
    """
    http_request = urlrequest.Request(
        PRIMARY_PRESSURE_URL,
        headers={
            "User-Agent": "WaterLoopMonitor/1.0",
            "Connection": "close",
        },
    )

    try:
        with urlrequest.urlopen(http_request, timeout=5) as response:
            response_text = response.read().decode("utf-8", errors="replace")

    except HTTPError as exc:
        raise RuntimeError(
            f"HTTP error while reading primary circuit pressure: "
            f"{exc.code} {exc.reason}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(
            f"Network error while reading primary circuit pressure: "
            f"{exc.reason}"
        ) from exc

    except ConnectionResetError as exc:
        raise RuntimeError(
            "Connection reset by peer while reading primary circuit pressure"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "Timeout while reading primary circuit pressure"
        ) from exc

    if response_text.startswith("Pressure:"):
        try:
            value_text = response_text.split()[1]    
            value = float(value_text)/100 # Convert to bar
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid primary pressure response: {response_text!r}"
            ) from exc

        if math.isnan(value):
            raise RuntimeError("Primary pressure is nan")

        return {"primary_pressure_1": f"{value:.2f}"}

    raise RuntimeError("'Pressure:' not found in sensor response")


async def read_opcua_nodes_once(
    client: Client,
    nodes: dict[str, dict[str, object]],
) -> dict[str, str]:
    """Read several OPC UA node values in one request."""
    read_params = ReadParameters()
    read_params.NodesToRead = []

    sensors = list(nodes.keys())

    for sensor in sensors:
        node_id = str(nodes[sensor]["node_id"])

        read_value_id = ReadValueId()
        read_value_id.NodeId = client.get_node(node_id).nodeid
        read_value_id.AttributeId = AttributeIds.Value
        read_params.NodesToRead.append(read_value_id)

    results = await client.uaclient.read(read_params)

    values: dict[str, str] = {}

    for sensor, result in zip(sensors, results):
        if not result.StatusCode.is_good():
            print(f"Bad OPC UA status for {sensor}: {result.StatusCode}")
            continue

        raw_value = result.Value.Value

        if raw_value is None:
            print(f"Skipping empty OPC UA value for {sensor}")
            continue

        try:
            sensor_type = str(nodes[sensor]["type"])

            if sensor_type == "float":
                scale = float(nodes[sensor].get("scale", 1.0))
                value_float = float(raw_value) * scale

                if not math.isfinite(value_float):
                    print(
                        f"Skipping non-finite OPC UA value for {sensor}: "
                        f"{raw_value!r}"
                    )
                    continue

                value = f"{value_float:.2f}"

            elif sensor_type == "int":
                value = str(int(raw_value))

            else:
                print(f"Unsupported OPC UA type for {sensor}: {sensor_type!r}")
                continue

        except (TypeError, ValueError) as exc:
            print(
                f"Skipping invalid OPC UA value for {sensor}: "
                f"{raw_value!r} ({exc})"
            )
            continue

        values[sensor] = value

    return values

async def read_opcua_measurements() -> dict[str, str]:
    """Connect to the OPC UA server and read all configured nodes."""
    client = Client(url=ENDPOINT)
    client.application_uri = APPLICATION_URI
    client.session_timeout = 60_000

    try:
        await client.connect()
        return await read_opcua_nodes_once(client, OPCUA_NODES)

    finally:
        await client.disconnect()


def post_measurement(sensor: str, value: str) -> None:
    """
    Send one measurement to the monitor app.

    The app adds the timestamp and runs its alarm logic.
    """
    payload = json.dumps({"sensor": sensor, "value": value}).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if API_TOKEN is not None:
        headers["X-API-Token"] = API_TOKEN

    request = urlrequest.Request(
        MONITOR_APP_POST_URL,
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Monitor app returned unexpected status: {response.status}"
                )

    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP error while posting {sensor}: {exc.code} {exc.reason}. "
            f"Response body: {body}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(
            f"Network error while posting {sensor}: {exc.reason}"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(f"Timeout while posting {sensor}") from exc


def post_measurements(values: dict[str, str]) -> None:
    """Send all measurements to the monitor app POST endpoint."""
    for sensor, value in values.items():
        post_measurement(sensor, value)


def collect_measurements() -> dict[str, str]:
    """Read all configured data sources and return one measurement dictionary.

    The legacy HTTP temperature is still read. OPC UA values are then added.
    If the same sensor appears in both sources, the OPC UA value wins because
    it is the configured source for that sensor in NODE_IDS.
    """
    values: dict[str, float] = {}

    # Read SEMFEG water loop temperature
    try:
        values.update(read_secondary_water_loop_temperature())
    except Exception as exc:
        print(f"HTTP polling error: {exc}")

    # Read water loop flow
    try:
        values.update(read_secondary_water_loop_flow())
    except Exception as exc:
        print(f"HTTP polling error: {exc}")

    # Read primary circuit pressure
    try:
        values.update(read_primary_pressure())
    except Exception as exc:
        print(f"HTTP polling error: {exc}")

    try:
        opcua_values = asyncio.run(read_opcua_measurements())
        values.update(opcua_values)
    except Exception as exc:
        print(f"OPC UA polling error: {exc}")

    return values


def main() -> None:
    while True:
        try:
            values = collect_measurements()

            if values:
                post_measurements(values)
                for sensor, value in values.items():
                    print(f"Posted {sensor} = {value}")
            else:
                print("No values collected")

        except Exception as exc:
            # Do not stop the service because of one bad read or failed POST.
            print(f"Polling error: {exc}")

        time.sleep(POLL_PERIOD_SECONDS)


if __name__ == "__main__":
    main()
