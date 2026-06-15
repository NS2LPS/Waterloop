
import sensors 

# Primary circcuit (eau glacée)
primary_pressure_1 = sensors.FloatSensorValidRange(
    {"en": "Primary Circuit Pressure", "fr": "Pression primaire"},
    "bar",
    1.8,None,
    )

primary_temperature_1 = sensors.FloatSensorValidRange(
    {"en": "Primary Temperature", "fr": "Température primaire"},
    "°C",
    None,14.0,
    )

primary_temperature_2 =sensors.FloatSensor(
    {"en": "Primary Temperature (back)", "fr": "Température primaire (retour)"},
    "°C",
    )

valve_command = sensors.FloatSensor(
    {"en": "Mixing Valve", "fr": "Vanne échangeur"},
    "%",
    )

pmp07_state = sensors.IntSensorValidValues(
    {"en": "Primary Pump State", "fr": "Pompe circuit primaire"},
    [3,],
    )

# Groupes froids
gf01_state = sensors.StateSensor({"en":"GF01 State","fr":"GF01 état"})
gf01_temperature_out = sensors.FloatSensor(
    {"en": "GF01 Exit Temperature", "fr": "GF01 température de sortie"},
    "°C",
    )

gf02_state = sensors.StateSensor({"en":"GF02 State","fr":"GF02 état"})
gf02_temperature_out = sensors.FloatSensor(
    {"en": "GF02 Exit Temperature", "fr": "GF02 température de sortie"},
    "°C",
    )


# Secondary circuit (boucle d'eau)
secondary_temperature_1 = sensors.FloatSensorValidRange(
    {"en": "Water Loop Temperature", "fr": "Température boucle d'eau"},
    "°C",
    None,18.0,
    )

secondary_temperature_2 = sensors.FloatSensor(
    {"en": "Water Loop Temperature (SEMFEG)", "fr": "Température boucle d'eau (SEMFEG)"},
    "°C",
    )

secondary_flow_1 = sensors.FloatSensorValidRange(
    {"en": "Water Loop Flow", "fr": "Débit boucle d'eau"},
    "L/min",
    110,None,
    )

# External Temperature
external_temperature_1 = sensors.FloatSensor(
    {"en": "External Temperature", "fr": "Température extérieure"},
    "°C",)

SIGNAL_TABLE = {
    "primary_pressure_1": primary_pressure_1,
    "primary_temperature_1": primary_temperature_1,
    "primary_temperature_2": primary_temperature_2,
    "pmp07_state" : pmp07_state,
    "secondary_flow_1": secondary_flow_1,
    "secondary_temperature_1": secondary_temperature_1,
    "secondary_temperature_2": secondary_temperature_2,
    "gf01_temperature_out": gf01_temperature_out,
    "gf02_temperature_out": gf02_temperature_out,
    "gf01_state": gf01_state,
    "gf02_state": gf02_state,
    "valve_command": valve_command,
    "external_temperature_1" : external_temperature_1
}
