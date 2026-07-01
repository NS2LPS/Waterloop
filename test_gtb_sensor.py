import asyncio
from asyncua import Client
from asyncua.ua import AttributeIds, ReadParameters, ReadValueId
import math

from sensor_poll import OPCUA_NODES

CONNECT_URL = "opc.tcp://129.175.113.201:49320"

async def main() -> None:
    client = Client(url=CONNECT_URL)

    await client.connect()
    print("Connected")

    read_params = ReadParameters()
    read_params.NodesToRead = []

    sensors = list(OPCUA_NODES.keys())

    for sensor in sensors:
        node_id = str(OPCUA_NODES[sensor]["node_id"])
        read_value_id = ReadValueId()
        read_value_id.NodeId = client.get_node(node_id).nodeid
        read_value_id.AttributeId = AttributeIds.Value
        read_params.NodesToRead.append(read_value_id)

    results = await client.uaclient.read(read_params)
    #for res in results:
    #    print(res)

    for sensor, result in zip(sensors, results):
        if not result.StatusCode.is_good():
            print(f"Bad OPC UA status for {sensor}: {result.StatusCode}")
            continue

        raw_value = result.Value.Value

        if raw_value is None:
            print(f"Skipping empty OPC UA value for {sensor}")
            continue

        sensor_type = str(OPCUA_NODES[sensor]["type"])

        if sensor_type == "float":
            scale = float(OPCUA_NODES[sensor].get("scale", 1.0))
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

        print(sensor,':',value)


    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
