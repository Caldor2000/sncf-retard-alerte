import os
import json
import csv
import io
import zipfile
import re
from datetime import datetime, timedelta

import requests
from google.transit import gtfs_realtime_pb2


TRIP_UPDATES_URL = (
    "https://proxy.transport.data.gouv.fr/resource/"
    "sncf-gtfs-rt-trip-updates"
)

SERVICE_ALERTS_URL = (
    "https://proxy.transport.data.gouv.fr/resource/"
    "sncf-gtfs-rt-service-alerts"
)

GTFS_URL = (
    "https://eu.ftp.opendatasoft.com/sncf/plandata/"
    "Export_OpenData_SNCF_GTFS_NewTripId.zip"
)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

STATE_FILE = "state.json"


# ============================================================
# ÉTAT
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "delays": {},
            "cancellations": {}
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Compatibilité avec l'ancien format :
        # {
        #   "trip_id": 10
        # }
        if "delays" not in state:
            state = {
                "delays": state,
                "cancellations": {}
            }

        state.setdefault("delays", {})
        state.setdefault("cancellations", {})

        return state

    except Exception:
        return {
            "delays": {},
            "cancellations": {}
        }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ============================================================
# NOTIFICATIONS
# ============================================================

def notify(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC absent")
        return

    url = f"https://ntfy.sh/{NTFY_TOPIC}"

    headers = {
        "Title": title,
        "Priority": "high"
    }

    response = requests.post(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20
    )

    print("Notification:", response.status_code)

    if response.status_code >= 400:
        print(response.text)


# ============================================================
# GTFS-RT
# ============================================================

def download_feed(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    return feed


# ============================================================
# DONNÉES STATIQUES SNCF
# ============================================================

def load_static_data():
    print("Téléchargement du GTFS statique...")

    response = requests.get(GTFS_URL, timeout=60)
    response.raise_for_status()

    routes = {}
    trips = {}
    first_departures = {}

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:

        # ----------------------------------------------------
        # ROUTES
        # ----------------------------------------------------

        with archive.open("routes.txt") as file:
            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:
                route_id = row.get("route_id", "")

                routes[route_id] = {
                    "short_name": row.get("route_short_name", ""),
                    "long_name": row.get("route_long_name", "")
                }

        # ----------------------------------------------------
        # TRIPS
        # ----------------------------------------------------

        with archive.open("trips.txt") as file:
            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:
                trip_id = row.get("trip_id", "")

                if not trip_id:
                    continue

                route_id = row.get("route_id", "")

                trips[trip_id] = {
                    "route_id": route_id,
                    "trip_short_name": row.get(
                        "trip_short_name",
                        ""
                    ),
                    "trip_headsign": row.get(
                        "trip_headsign",
                        ""
                    ),
                    "departure_time": ""
                }

        # ----------------------------------------------------
        # STOP TIMES
        # ----------------------------------------------------

        with archive.open("stop_times.txt") as file:
            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:
                trip_id = row.get("trip_id", "")

                if trip_id not in trips:
                    continue

                stop_sequence = row.get(
                    "stop_sequence",
                    ""
                )

                try:
                    sequence = int(stop_sequence)
                except Exception:
                    continue

                departure_time = row.get(
                    "departure_time",
                    ""
                )

                if not departure_time:
                    continue

                current = first_departures.get(trip_id)

                if current is None or sequence < current["sequence"]:
                    first_departures[trip_id] = {
                        "sequence": sequence,
                        "departure_time": departure_time
                    }

    # Ajouter le premier départ à chaque train
    for trip_id, data in first_departures.items():
        if trip_id in trips:
            trips[trip_id]["departure_time"] = data[
                "departure_time"
            ]

    print(f"{len(routes)} lignes chargées")
    print(f"{len(trips)} trains chargés")

    return routes, trips


# ============================================================
# IDENTIFICATION DU TRAIN
# ============================================================

def identify_train(trip_id, routes, trips):
    data = trips.get(trip_id)

    if not data:
        return None

    route_id = data.get("route_id", "")

    route = routes.get(route_id, {})

    route_short = route.get(
        "short_name",
        ""
    ).strip()

    route_long = route.get(
        "long_name",
        ""
    ).strip()

    trip_short = data.get(
        "trip_short_name",
        ""
    ).strip()

    trip_headsign = data.get(
        "trip_headsign",
        ""
    ).strip()

    search_text = " ".join([
        route_short,
        route_long,
        trip_short,
        trip_headsign
    ]).upper()

    # --------------------------------------------------------
    # EXCLUSION TER
    # --------------------------------------------------------

    if (
        "TER" in search_text
        or "TRANSILIEN" in search_text
        or "RER" in search_text
    ):
        return None

    # --------------------------------------------------------
    # TYPE DE TRAIN
    # --------------------------------------------------------

    if "OUIGO" in search_text:
        train_type = "OUIGO"

    elif (
        "INTERCITÉS" in search_text
        or "INTERCITES" in search_text
    ):
        train_type = "INTERCITÉS"

    elif (
        "TGV INOUI" in search_text
        or "TGV INOUI" in route_long.upper()
        or "INOUI" in search_text
    ):
        train_type = "TGV INOUI"

    else:
        return None

    # --------------------------------------------------------
    # NUMÉRO COMMERCIAL
    # --------------------------------------------------------

    commercial_number = ""

    # Dans le GTFS SNCF, le numéro est souvent dans
    # trip_headsign.
    if trip_headsign.isdigit():
        commercial_number = trip_headsign

    elif trip_short.isdigit():
        commercial_number = trip_short

    else:
        numbers = re.findall(
            r"\b\d{3,5}\b",
            trip_headsign
        )

        if numbers:
            commercial_number = numbers[0]

    if not commercial_number:
        commercial_number = "numéro non dispo"

    # --------------------------------------------------------
    # DESTINATION
    # --------------------------------------------------------

    destination = route_long

    if not destination:
        destination = "Destination non disponible"

    # --------------------------------------------------------
    # DÉPART
    # --------------------------------------------------------

    departure_time = data.get(
        "departure_time",
        ""
    )

    return {
        "type": train_type,
        "number": commercial_number,
        "destination": destination,
        "departure_time": departure_time
    }


# ============================================================
# RETARD
# ============================================================

def get_delay(trip):
    """
    Retourne le retard en secondes.
    """

    latest_delay = None

    for stop in trip.stop_time_update:
        if stop.HasField("departure"):
            if stop.departure.HasField("delay"):
                latest_delay = stop.departure.delay

        if stop.HasField("arrival"):
            if stop.arrival.HasField("delay"):
                latest_delay = stop.arrival.delay

    if latest_delay is None:
        return 0

    return latest_delay


def format_time(time_string):
    if not time_string:
        return "Non disponible"

    return time_string[:5]


def estimated_departure(scheduled_time, delay_minutes):
    if not scheduled_time:
        return "Non disponible"

    try:
        hour, minute = scheduled_time[:5].split(":")

        base = datetime(
            2000,
            1,
            1,
            int(hour),
            int(minute)
        )

        estimated = base + timedelta(
            minutes=delay_minutes
        )

        return estimated.strftime("%H:%M")

    except Exception:
        return "Non disponible"


# ============================================================
# TRAITEMENT DES RETARDS
# ============================================================

def process_delays(feed, routes, trips, state):
    previous_delays = state.setdefault(
        "delays",
        {}
    )

    current_delays = {}

    for entity in feed.entity:

        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update

        if not trip_update.HasField("trip"):
            continue

        trip_id = trip_update.trip.trip_id

        if not trip_id:
            continue

        train = identify_train(
            trip_id,
            routes,
            trips
        )

        if not train:
            continue

        delay_seconds = get_delay(trip_update)

        delay_minutes = int(
            round(delay_seconds / 60)
        )

        # On ignore les retards inférieurs à 10 minutes
        if delay_minutes < 10:
            current_delays[trip_id] = delay_minutes
            continue

        previous_delay = previous_delays.get(
            trip_id,
            0
        )

        # Première alerte à partir de 10 minutes
        first_alert = (
            previous_delay < 10
        )

        # Nouvelle alerte si le retard augmente
        increase_alert = (
            delay_minutes >= previous_delay + 10
        )

        if first_alert or increase_alert:

            scheduled = format_time(
                train["departure_time"]
            )

            estimated = estimated_departure(
                train["departure_time"],
                delay_minutes
            )

            message = (
                f"{train['type']} {train['number']}\n"
                f"Départ prévu : {scheduled}\n"
                f"Départ estimé : {estimated}\n"
                f"Destination : {train['destination']}\n"
                f"Retard actuel : +{delay_minutes} min"
            )

            if previous_delay >= 10:
                message += (
                    f"\nRetard précédent : "
                    f"+{previous_delay} min"
                )

            notify(
                f"{train['type']} {train['number']} en retard",
                message
            )

    state["delays"] = current_delays


# ============================================================
# ANNULATIONS
# ============================================================

def process_cancellations(
    feed,
    routes,
    trips,
    state
):
    previous_cancellations = state.setdefault(
        "cancellations",
        {}
    )

    current_cancellations = {}

    for entity in feed.entity:

        if not entity.HasField("alert"):
            continue

        alert = entity.alert

        # GTFS-RT :
        # NO_SERVICE = service supprimé / annulé
        if alert.effect != gtfs_realtime_pb2.Alert.NO_SERVICE:
            continue

        for informed_entity in alert.informed_entity:

            if not informed_entity.HasField("trip"):
                continue

            trip_id = informed_entity.trip.trip_id

            if not trip_id:
                continue

            train = identify_train(
                trip_id,
                routes,
                trips
            )

            if not train:
                continue

            cancellation_key = (
                f"{entity.id}:{trip_id}"
            )

            current_cancellations[
                cancellation_key
            ] = True

            if cancellation_key in previous_cancellations:
                continue

            scheduled = format_time(
                train["departure_time"]
            )

            message = (
                f"{train['type']} "
                f"{train['number']} SUPPRIMÉ\n"
                f"Départ prévu : {scheduled}\n"
                f"Destination : "
                f"{train['destination']}\n"
                f"Le train est indiqué comme "
                f"supprimé par SNCF."
            )

            notify(
                f"{train['type']} "
                f"{train['number']} SUPPRIMÉ",
                message
            )

    state["cancellations"] = (
        current_cancellations
    )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    print("================================")
    print("SNCF MONITOR")
    print("================================")

    state = load_state()

    routes, trips = load_static_data()

    # --------------------------------------------------------
    # RETARDS
    # --------------------------------------------------------

    print("Téléchargement des retards...")

    trip_feed = download_feed(
        TRIP_UPDATES_URL
    )

    print(
        f"{len(trip_feed.entity)} "
        f"mises à jour reçues"
    )

    process_delays(
        trip_feed,
        routes,
        trips,
        state
    )

    # --------------------------------------------------------
    # ANNULATIONS
    # --------------------------------------------------------

    print("Téléchargement des suppressions...")

    alert_feed = download_feed(
        SERVICE_ALERTS_URL
    )

    print(
        f"{len(alert_feed.entity)} "
        f"alertes reçues"
    )

    process_cancellations(
        alert_feed,
        routes,
        trips,
        state
    )

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    save_state(state)

    print("Surveillance terminée.")


if __name__ == "__main__":
    main()