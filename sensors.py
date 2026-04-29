from languages import translate

class Sensor:
    def description(self, language):
        return self.__description__.get(language, self.__description__["en"])

class FloatSensor(Sensor):
    def __init__(self,description,unit):
        self.__description__ = description
        self.__unit__ = unit
    def format(self, value):
        return f"{value:.1f} {self.__unit__}"
    def value(self,val_str):
        return float(val_str)
    
class StateSensor(Sensor):
    def __init__(self,description):
        self.__description__ = description
    def format(self, value):
        return "ON" if value else "OFF"
    def value(self,val_str):
        return int(val_str)

class IntSensor(Sensor):
    def __init__(self,description):
        self.__description__ = description
    def format(self, value):
        return "str(value)"
    def value(self,val_str):
        return int(val_str)
    
class FloatSensorValidRange(FloatSensor):
    def __init__(self,description,unit,min,max):
        self.__description__ = description
        self.__unit__ = unit
        self.min_value = min
        self.max_value = max
    def validate(self, value: float) -> int:
        if value < self.min_value:
            return 1
        if value > self.max_value:
            return 2
        return 0
    def alarm_msg(self, errcode, language):
        d = self.description(language)
        if errcode==0:
            return f"{d} : {translate('back_to_normal',language)}"
        elif errcode==1:
            return f"{d} : {translate('too_low',language)}"
        elif errcode==2:
            return f"{d} : {translate('too_high',language)}"


class StateSensorValid(StateSensor):
    def __init__(self,description,valid_value=1):
        self.__description__ = description
        self.valid_value = valid_value
    def validate(self,value:int)->int:
        return  0 if value==self.valid_value else 1
    def alarm_msg(self, errcode, language):
        d = self.description(language)
        if errcode==0:
            return f"{d} : {translate('back_to_normal',language)}"
        elif errcode==1:
            return f"{d} : {translate('stopped',language)}"

class IntSensorValidValues(IntSensor):
    def __init__(self,description,valid_values):
        self.__description__ = description
        self.valid_values =  valid_values
    def format(self, value):
        return "ON" if value in self.valid_values else "OFF"
    def validate(self, value: int) -> int:
        return 0 if value in self.valid_values else 1
    def alarm_msg(self, errcode, language):
        d = self.description(language)
        if errcode==0:
            return f"{d} : {translate('back_to_normal',language)}"
        elif errcode==1:
            return f"{d} : {translate('invalid_state',language)}"
    


# Primary circcuit (eau glacée)
primary_pressure_1 = FloatSensorValidRange(
    {"en": "Primary Circuit Pressure", "fr": "Pression primaire"},
    "bar",
    2.0,3.5,
    )

primary_temperature_1 = FloatSensorValidRange(
    {"en": "Primary Temperature", "fr": "Température primaire"},
    "°C",
    5.0,15.0,
    )

primary_temperature_2 = FloatSensor(
    {"en": "Primary Temperature (back)", "fr": "Température primaire (retour)"},
    "°C",
    )

valve_command = FloatSensor(
    {"en": "Mixing Valve", "fr": "Vanne échangeur"},
    "%",
    )

pmp07_state = IntSensorValidValues(
    {"en": "Primary Pump State", "fr": "Pompe circuit primaire"},
    [3,],
    )

# Groupes froids
gf01_state = StateSensor({"en":"GF01 State","fr":"GF01"})
gf01_temperature_out = FloatSensor(
    {"en": "GF01 Exit Temperature", "fr": "GF01 sortie"},
    "°C",
    )

gf02_state = StateSensor({"en":"GF02 State","fr":"GF02"})
gf02_temperature_out = FloatSensor(
    {"en": "GF02 Exit Temperature", "fr": "GF02 sortie"},
    "°C",
    )


# Secondary circuit (boucle d'eau)
secondary_temperature_1 = FloatSensorValidRange(
    {"en": "Water Loop Temperature", "fr": "Température boucle d'eau"},
    "°C",
    13.5,20.5,
    )

secondary_temperature_2 = FloatSensor(
    {"en": "Water Loop Temperature (SEMFEG)", "fr": "Température boucle d'eau (SEMFEG)"},
    "°C",
    )

secondary_flow_1 = FloatSensorValidRange(
    {"en": "Water Loop Flow", "fr": "Débit boucle d'eau"},
    "L/min",
    0,1000,
    )

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
    "valve_command": valve_command
}
