# Translation table

en = {
    "title": "Water Loop Monitoring",
    "alarm_title": "Alarm history",
    "language": "Language",
    "timespan": "Timespan",
    "alarms": "Open alarms",
    "no_data": "No data",
    "unknown_time": "Unknown time",
    "stale": "STALE",
    "ok": "OK",
    "alarm": "ALARM",
    "hours_suffix": "h",
    "time_axis": "Time",
    "value_axis": "Value",
    "about": "About",
    "last_day": "Last day",
    "last_week": "1 week",
    "last_month": "1 month",
    "last_year": "Last year",
    "all": "All",
    "phone_registration": "To receive alarm notifications on your phone, install the ntfy app and subscribe to {NTFY_TOPIC}.",
    "back": "Back",
    "refresh": "Refresh",
    "time": "Time",
    "sensor": "Sensor",
    "transition": "Transition",
    "acknowledged": "Acknowledged",
    "yes": "Yes",
    "no": "No",
    "alarm_on": "Out of range",
    "alarm_off": "Back in range",
    "no_rows": "No alarms in the selected period",
    "value": "Value",
    "records_per_page": "Records per page",
    "event": "Event",
    "back_to_normal": "back to normal",
    "too_high": "too high",
    "too_low": "too low",
    "stopped": "stopped",
    "invalid_state": "invalid state",
    "archive":"Archive"
}


fr = {
    "title": "Surveillance de la boucle d'eau",
    "alarm_title": "Historique des alarmes",
    "language": "Langue",
    "timespan": "Durée",
    "alarms": "Ouvrir les alarmes",
    "no_data": "Pas de donnée",
    "unknown_time": "Heure inconnue",
    "stale": "PÉRIMÉ",
    "ok": "OK",
    "alarm": "ALARME",
    "hours_suffix": "h",
    "last_day": "Dernier jour",
    "last_week": "1 semaine",
    "last_month": "1 mois",
    "last_year": "Dernière année",
    "all": "Tout",
    "time_axis": "Temps",
    "value_axis": "Valeur",
    "about": "À propos",
    "phone_registration": "Pour recevoir les alarmes sur votre téléphone, installez l'application ntfy et abonnez-vous à {NTFY_TOPIC}.",
    "back": "Retour",
    "refresh": "Rafraîchir",
    "time": "Heure",
    "sensor": "Capteur",
    "transition": "Transition",
    "acknowledged": "Acquittée",
    "yes": "Oui",
    "no": "Non",
    "alarm_on": "Hors limites",
    "alarm_off": "Retour dans les limites",
    "no_rows": "Aucune alarme dans la période sélectionnée",
    "value": "Valeur",
    "records_per_page": "Lignes par page",
    "event": "Évènement",
    "back_to_normal": "retour à la normale",
    "too_high": "trop élevé(e)",
    "too_low": "trop faible",
    "stopped": "arrêté(e)",
    "invalid_state": "état invalide",
    "archive":"Archive"
}


LANGUAGES = {"en": en, "fr": fr}


def translate(s, language):
    if language not in LANGUAGES:
        return s
    return LANGUAGES[language].get(s, s)
