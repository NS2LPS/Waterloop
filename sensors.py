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
        if self.min_value is not None and value < self.min_value:
            return 1
        if self.max_value is not None and value > self.max_value:
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
    

