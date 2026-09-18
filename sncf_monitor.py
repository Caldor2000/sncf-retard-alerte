import os
import json
import csv
import io
import zipfile
import re
from datetime import datetime, timedelta

import requests
from google.transit import gtfs_realtime_pb2


# ============================================================
# CONFIGURATION
# ============================================================

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

        # Compatibilité avec l'ancien format
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
        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# NOTIFICATIONS NTFY
# ============================================================

def notify(title, message):

    if not NTFY_TOPIC:
        print("NTFY_TOPIC absent")
        return

    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high"
        },
        timeout=20
    )

    print("Notification:", response.status_code)

    if response.status_code >= 400:
        print(response.text)


# ============================================================
# GTFS-RT
# ============================================================

def download_feed(url):

    response = requests.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()

    feed.ParseFromString(
        response.content
    )

    return feed


# ============================================================
# OUTILS
# ============================================================

def clean(value):

    if value is None:
        return ""

    return str(value).strip()


def normalize(value):

    value = clean(value).upper()

    value = (
        value
        .replace("É", "E")
        .replace("È", "E")
        .replace("Ê", "E")
        .replace("Ë", "E")
        .replace("À", "A")
        .replace("Â", "A")
        .replace("Ä", "A")
        .replace("Î", "I")
        .replace("Ï", "I")
        .replace("Ô", "O")
        .replace("Ö", "O")
        .replace("Ù", "U")
        .replace("Û", "U")
        .replace("Ü", "U")
        .replace("Ç", "C")
    )

    return value


def extract_train_number(*values):

    for value in values:

        text = clean(value)

        if not text:
            continue

        # Cas où le champ est uniquement le numéro
        if text.isdigit():
            return text

        # Recherche d'un numéro de train de 3 à 5 chiffres
        numbers = re.findall(
            r"\b\d{3,5}\b",
            text
        )

        if numbers:
            return numbers[0]

    return ""


# ============================================================
# DONNÉES STATIQUES SNCF
# ============================================================

def load_static_data():

    print("Téléchargement du GTFS statique...")

    response = requests.get(
        GTFS_URL,
        timeout=60
    )

    response.raise_for_status()

    routes = {}
    trips = {}

    # Index secondaires pour les correspondances
    trip_numbers = {}
    route_trip_numbers = {}

    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as archive:

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

                route_id = clean(
                    row.get("route_id")
                )

                if not route_id:
                    continue

                routes[route_id] = {
                    "short_name": clean(
                        row.get("route_short_name")
                    ),
                    "long_name": clean(
                        row.get("route_long_name")
                    )
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

                trip_id = clean(
                    row.get("trip_id")
                )

                if not trip_id:
                    continue

                route_id = clean(
                    row.get("route_id")
                )

                trip_short_name = clean(
                    row.get("trip_short_name")
                )

                trip_headsign = clean(
                    row.get("trip_headsign")
                )

                route = routes.get(
                    route_id,
                    {}
                )

                train_number = extract_train_number(
                    trip_short_name,
                    trip_headsign,
                    trip_id
                )

                trips[trip_id] = {
                    "route_id": route_id,
                    "route_short_name": route.get(
                        "short_name",
                        ""
                    ),
                    "route_long_name": route.get(
                        "long_name",
                        ""
                    ),
                    "trip_short_name": trip_short_name,
                    "trip_headsign": trip_headsign,
                    "train_number": train_number,
                    "departure_time": ""
                }

                if train_number:

                    trip_numbers.setdefault(
                        train_number,
                        []
                    ).append(trip_id)

                    route_key = (
                        route_id,
                        train_number
                    )

                    route_trip_numbers.setdefault(
                        route_key,
                        []
                    ).append(trip_id)

        # ----------------------------------------------------
        # PREMIER DÉPART
        # ----------------------------------------------------

        first_departures = {}

        with archive.open(
            "stop_times.txt"
        ) as file:

            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:

                trip_id = clean(
                    row.get("trip_id")
                )

                if trip_id not in trips:
                    continue

                departure_time = clean(
                    row.get("departure_time")
                )

                if not departure_time:
                    continue

                try:
                    sequence = int(
                        row.get(
                            "stop_sequence",
                            ""
                        )
                    )
                except Exception:
                    continue

                previous = first_departures.get(
                    trip_id
                )

                if (
                    previous is None
                    or sequence < previous["sequence"]
                ):
                    first_departures[trip_id] = {
                        "sequence": sequence,
                        "departure_time": departure_time
                    }

        for trip_id, data in first_departures.items():

            if trip_id in trips:
                trips[trip_id]["departure_time"] = (
                    data["departure_time"]
                )

    print(
        f"{len(routes)} lignes chargées"
    )

    print(
        f"{len(trips)} trains chargés"
    )

    print(
        f"{len(trip_numbers)} numéros de train indexés"
    )

    return (
        routes,
        trips,
        trip_numbers,
        route_trip_numbers
    )


# ============================================================
# IDENTIFICATION DU TRAIN
# ============================================================

def identify_train(
    trip_id,
    realtime_trip,
    routes,
    trips,
    trip_numbers,
    route_trip_numbers
):

    trip_id = clean(trip_id)

    # --------------------------------------------------------
    # 1. CORRESPONDANCE EXACTE
    # --------------------------------------------------------

    data = trips.get(trip_id)

    # --------------------------------------------------------
    # 2. FALLBACK PAR ROUTE + NUMÉRO
    # --------------------------------------------------------

    if not data:

        realtime_route_id = clean(
            getattr(
                realtime_trip,
                "route_id",
                ""
            )
        )

        train_number = extract_train_number(
            trip_id
        )

        if (
            realtime_route_id
            and train_number
        ):

            candidates = route_trip_numbers.get(
                (
                    realtime_route_id,
                    train_number
                ),
                []
            )

            if candidates:

                data = trips.get(
                    candidates[0]
                )

    # --------------------------------------------------------
    # 3. FALLBACK PAR NUMÉRO SEUL
    # --------------------------------------------------------

    if not data:

        train_number = extract_train_number(
            trip_id
        )

        candidates = trip_numbers.get(
            train_number,
            []
        )

        if candidates:

            # On privilégie une course dont le numéro
            # correspond exactement.
            data = trips.get(
                candidates[0]
            )

    if not data:
        return None

    # --------------------------------------------------------
    # INFORMATIONS STATIQUES
    # --------------------------------------------------------

    route_id = data.get(
        "route_id",
        ""
    )

    route = routes.get(
        route_id,
        {}
    )

    route_short = normalize(
        route.get(
            "short_name",
            ""
        )
    )

    route_long = normalize(
        route.get(
            "long_name",
            ""
        )
    )

    trip_short = normalize(
        data.get(
            "trip_short_name",
            ""
        )
    )

    trip_headsign_raw = clean(
        data.get(
            "trip_headsign",
            ""
        )
    )

    trip_headsign = normalize(
        trip_headsign_raw
    )

    # --------------------------------------------------------
    # EXCLUSION DES TRAINS NON SOUHAITÉS
    # --------------------------------------------------------

    search_text = " ".join([
        route_short,
        route_long,
        trip_short,
        trip_headsign
    ])

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
        "INTERCITES" in search_text
        or "INTERCITÉS" in search_text
    ):

        train_type = "INTERCITÉS"

    elif (
        "INOUI" in search_text
        or "TGV" in search_text
    ):

        train_type = "TGV INOUI"

    else:

        return None

    # --------------------------------------------------------
    # NUMÉRO DU TRAIN
    # --------------------------------------------------------

    train_number = data.get(
        "train_number",
        ""
    )

    if not train_number:

        train_number = extract_train_number(
            trip_id,
            data.get(
                "trip_short_name",
                ""
            ),
            trip_headsign_raw
        )

    if not train_number:

        train_number = "numéro non dispo"

    # --------------------------------------------------------
    # DESTINATION
    # --------------------------------------------------------

    destination = trip_headsign_raw

    # Si trip_headsign contient uniquement le numéro,
    # on utilise le nom de ligne comme destination.
    if (
        not destination
        or destination.isdigit()
    ):

        destination = clean(
            route.get(
                "long_name",
                ""
            )
        )

    if not destination:

        destination = "Destination non disponible"

    return {
        "type": train_type,
        "number": train_number,
        "destination": destination,
        "departure_time": data.get(
            "departure_time",
            ""
        )
    }


# ============================================================
# RETARD
# ============================================================

def get_delay(trip_update):

    latest_delay = None

    for stop in trip_update.stop_time_update:

        if stop.HasField("departure"):

            if stop.departure.HasField("delay"):

                latest_delay = (
                    stop.departure.delay
                )

        if stop.HasField("arrival"):

            if stop.arrival.HasField("delay"):

                latest_delay = (
                    stop.arrival.delay
                )

    if latest_delay is None:
        return 0

    return latest_delay


# ============================================================
# HORAIRES
# ============================================================

def format_time(value):

    if not value:
        return "Non disponible"

    return value[:5]


def estimated_departure(
    scheduled_time,
    delay_minutes
):

    if not scheduled_time:
        return "Non disponible"

    try:

        hour = int(
            scheduled_time[:2]
        )

        minute = int(
            scheduled_time[3:5]
        )

        base = datetime(
            2000,
            1,
            1,
            hour,
            minute
        )

        estimated = (
            base
            + timedelta(
                minutes=delay_minutes
            )
        )

        return estimated.strftime(
            "%H:%M"
        )

    except Exception:

        return "Non disponible"


# ============================================================
# RETARDS
# ============================================================

def process_delays(
    feed,
    routes,
    trips,
    trip_numbers,
    route_trip_numbers,
    state
):

    previous_delays = state.setdefault(
        "delays",
        {}
    )

    current_delays = {}

    total_trains = 0
    recognized_trains = 0
    delayed_trains = 0
    notifications_sent = 0

    for entity in feed.entity:

        if not entity.HasField(
            "trip_update"
        ):
            continue

        trip_update = entity.trip_update

        if not trip_update.HasField(
            "trip"
        ):
            continue

        total_trains += 1

        trip_id = clean(
            trip_update.trip.trip_id
        )

        if not trip_id:
            continue

        train = identify_train(
            trip_id,
            trip_update.trip,
            routes,
            trips,
            trip_numbers,
            route_trip_numbers
        )

        if not train:
            continue

        recognized_trains += 1

        delay_seconds = get_delay(
            trip_update
        )

        delay_minutes = int(
            round(
                delay_seconds / 60
            )
        )

        # On mémorise toujours le retard
        current_delays[trip_id] = (
            delay_minutes
        )

        if delay_minutes < 10:
            continue

        delayed_trains += 1

        previous_delay = previous_delays.get(
            trip_id,
            0
        )

        first_alert = (
            previous_delay < 10
        )

        increase_alert = (
            delay_minutes
            >= previous_delay + 10
        )

        if not (
            first_alert
            or increase_alert
        ):
            continue

        departure_time = format_time(
            train["departure_time"]
        )

        estimated_time = (
            estimated_departure(
                train["departure_time"],
                delay_minutes
            )
        )

        message = (
            f"{train['type']} {train['number']}\n"
            f"Départ prévu : {departure_time}\n"
            f"Départ estimé : {estimated_time}\n"
            f"Destination : {train['destination']}\n"
            f"Retard actuel : +{delay_minutes} min"
        )

        if previous_delay >= 10:

            message += (
                f"\nRetard précédent : "
                f"+{previous_delay} min"
            )

        notify(
            f"{train['type']} "
            f"{train['number']} en retard",
            message
        )

        notifications_sent += 1

    state["delays"] = current_delays

    print("--------------------------------")
    print("DIAGNOSTIC RETARDS")
    print(
        f"Trains analysés : "
        f"{total_trains}"
    )
    print(
        f"Trains reconnus : "
        f"{recognized_trains}"
    )
    print(
        f"Trains avec >= 10 min : "
        f"{delayed_trains}"
    )
    print(
        f"Notifications envoyées : "
        f"{notifications_sent}"
    )
    print("--------------------------------")


# ============================================================
# SUPPRESSIONS
# ============================================================

def process_cancellations(
    feed,
    routes,
    trips,
    trip_numbers,
    route_trip_numbers,
    state
):

    previous_cancellations = (
        state.setdefault(
            "cancellations",
            {}
        )
    )

    current_cancellations = {}

    notifications_sent = 0

    for entity in feed.entity:

        if not entity.HasField(
            "alert"
        ):
            continue

        alert = entity.alert

        if (
            alert.effect
            != gtfs_realtime_pb2.Alert.NO_SERVICE
        ):
            continue

        for informed_entity in (
            alert.informed_entity
        ):

            if not informed_entity.HasField(
                "trip"
            ):
                continue

            realtime_trip = (
                informed_entity.trip
            )

            trip_id = clean(
                realtime_trip.trip_id
            )

            if not trip_id:
                continue

            train = identify_train(
                trip_id,
                realtime_trip,
                routes,
                trips,
                trip_numbers,
                route_trip_numbers
            )

            if not train:
                continue

            cancellation_key = (
                f"{entity.id}:{trip_id}"
            )

            current_cancellations[
                cancellation_key
            ] = True

            if (
                cancellation_key
                in previous_cancellations
            ):
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

            notifications_sent += 1

    state["cancellations"] = (
        current_cancellations
    )

    print("--------------------------------")
    print("DIAGNOSTIC SUPPRESSIONS")
    print(
        f"Suppressions notifiées : "
        f"{notifications_sent}"
    )
    print("--------------------------------")


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    print("================================")
    print("SNCF MONITOR")
    print("================================")

    state = load_state()

    (
        routes,
        trips,
        trip_numbers,
        route_trip_numbers
    ) = load_static_data()

    # --------------------------------------------------------
    # RETARDS
    # --------------------------------------------------------

    print(
        "Téléchargement des retards..."
    )

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
        trip_numbers,
        route_trip_numbers,
        state
    )

    # --------------------------------------------------------
    # SUPPRESSIONS
    # --------------------------------------------------------

    print(
        "Téléchargement des suppressions..."
    )

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
        trip_numbers,
        route_trip_numbers,
        state
    )

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    save_state(state)

    print(
        "Surveillance terminée."
    )


if __name__ == "__main__":
    main()