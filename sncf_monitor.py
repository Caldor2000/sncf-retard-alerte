import os
import json
import csv
import io
import zipfile
import re
import requests

from google.transit import gtfs_realtime_pb2


TRIP_UPDATES_URL = (
    "https://proxy.transport.data.gouv.fr/"
    "resource/sncf-gtfs-rt-trip-updates"
)

SERVICE_ALERTS_URL = (
    "https://proxy.transport.data.gouv.fr/"
    "resource/sncf-gtfs-rt-service-alerts"
)

STATIC_URL = (
    "https://eu.ftp.opendatasoft.com/sncf/plandata/"
    "Export_OpenData_SNCF_GTFS_NewTripId.zip"
)

NTFY_URL = "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

STATE_FILE = "state.json"

MIN_DELAY = 10


# =========================================================
# MÉMOIRE
# =========================================================

def load_state():

    if not os.path.exists(STATE_FILE):
        return {
            "delays": {},
            "cancellations": {}
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        # Compatibilité avec l'ancienne version
        if "delays" not in state:

            state = {
                "delays": state,
                "cancellations": {}
            }

        if "cancellations" not in state:
            state["cancellations"] = {}

        return state

    except Exception:

        return {
            "delays": {},
            "cancellations": {}
        }


def save_state(state):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================================================
# NOTIFICATION NTFY
# =========================================================

def send_notification(
    title,
    message,
    tags="train"
):

    if not NTFY_TOPIC:
        raise RuntimeError(
            "NTFY_TOPIC n'est pas configuré."
        )

    response = requests.post(

        f"{NTFY_URL}/{NTFY_TOPIC}",

        headers={
            "Title": title,
            "Priority": "high",
            "Tags": tags
        },

        data=message.encode("utf-8"),

        timeout=15
    )

    response.raise_for_status()


# =========================================================
# FLUX GTFS-RT
# =========================================================

def get_feed(url):

    response = requests.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    feed = (
        gtfs_realtime_pb2.FeedMessage()
    )

    feed.ParseFromString(
        response.content
    )

    return feed


# =========================================================
# DONNÉES STATIQUES SNCF
# =========================================================

def get_static_data():

    print(
        "Téléchargement des données horaires SNCF..."
    )

    response = requests.get(
        STATIC_URL,
        timeout=60
    )

    response.raise_for_status()

    routes = {}
    trips = {}

    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as archive:

        # -------------------------------------------------
        # ROUTES
        # -------------------------------------------------

        with archive.open(
            "routes.txt"
        ) as file:

            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:

                route_id = row.get(
                    "route_id",
                    ""
                )

                if not route_id:
                    continue

                routes[route_id] = {
                    "short_name": row.get(
                        "route_short_name",
                        ""
                    ),
                    "long_name": row.get(
                        "route_long_name",
                        ""
                    )
                }

# -------------------------------------------------
# TRIPS
# -------------------------------------------------

with archive.open(
    "trips.txt"
) as file:

    text = io.TextIOWrapper(
        file,
        encoding="utf-8-sig",
        newline=""
    )

    reader = csv.DictReader(text)

    for row in reader:

        trip_id = row.get(
            "trip_id",
            ""
        )

        if not trip_id:
            continue

        route_id = row.get(
            "route_id",
            ""
        )

        route = routes.get(
            route_id,
            {}
        )

        trips[trip_id] = {

            "route_id": route_id,

            "route_short_name":
                route.get(
                    "short_name",
                    ""
                ),

            "route_long_name":
                route.get(
                    "long_name",
                    ""
                ),

            "trip_short_name":
                row.get(
                    "trip_short_name",
                    ""
                ),

            "trip_headsign":
                row.get(
                    "trip_headsign",
                    ""
                ),

            "departure_time": ""
        }
        
# -------------------------------------------------
# STOP TIMES
# -------------------------------------------------

with archive.open(
    "stop_times.txt"
) as file:

    text = io.TextIOWrapper(
        file,
        encoding="utf-8-sig",
        newline=""
    )

    reader = csv.DictReader(text)

    first_stop_sequence = {}

    for row in reader:

        trip_id = row.get(
            "trip_id",
            ""
        )

        if not trip_id:
            continue

        stop_sequence = row.get(
            "stop_sequence",
            ""
        )

        departure_time = row.get(
            "departure_time",
            ""
        )

        if not departure_time:
            continue

        try:
            sequence = int(
                stop_sequence
            )
        except ValueError:
            continue

        if (
            trip_id not in first_stop_sequence
            or sequence < first_stop_sequence[trip_id]
        ):

            first_stop_sequence[trip_id] = sequence

            if trip_id in trips:
                trips[trip_id]["departure_time"] = (
                    departure_time
                )

    print(
        f"{len(trips)} trajets chargés."
    )

    return trips


# =========================================================
# IDENTIFICATION DU TRAIN
# =========================================================

def identify_train(
    trip_id,
    static_trips
):

    data = static_trips.get(
        trip_id
    )

    if not data:
        return None

    route_short = data.get(
        "route_short_name",
        ""
    )

    route_long = data.get(
        "route_long_name",
        ""
    )

    trip_short = data.get(
        "trip_short_name",
        ""
    )

    trip_headsign = data.get(
        "trip_headsign",
        ""
    )

    full_text = " ".join([
        route_short,
        route_long,
        trip_short,
        trip_headsign
    ]).upper()

    # -----------------------------------------------------
    # EXCLUSION TER
    # -----------------------------------------------------

    if "TER" in full_text:
        return None

    # -----------------------------------------------------
    # TYPE DE TRAIN
    # -----------------------------------------------------

    if "OUIGO" in full_text:

        train_type = "OUIGO"

    elif (
        "INTERCITES" in full_text
        or "INTERCITÉS" in full_text
        or "INTERCITE" in full_text
    ):

        train_type = "INTERCITÉS"

    elif "TGV" in full_text:

        train_type = "TGV INOUI"

    else:

        return None

    # -----------------------------------------------------
    # NUMÉRO COMMERCIAL
    # -----------------------------------------------------

    commercial_number = ""

    # Dans le flux SNCF utilisé ici,
    # le trip_headsign peut contenir le numéro commercial.
    candidate = trip_headsign.strip()

    if re.fullmatch(
        r"\d{1,6}",
        candidate
    ):

        commercial_number = candidate

    # Sinon on essaie trip_short_name
    if not commercial_number:

        candidate = trip_short.strip()

        if re.fullmatch(
            r"\d{1,6}",
            candidate
        ):

            commercial_number = candidate

        else:

            match = re.search(
                r"\b\d{3,6}\b",
                candidate
            )

            if match:
                commercial_number = (
                    match.group(0)
                )

    if not commercial_number:

        commercial_number = "Numéro non disponible"

    # -----------------------------------------------------
    # DESTINATION
    # -----------------------------------------------------

    # Pour l'instant on utilise route_long_name
    # afin de ne plus afficher le numéro comme destination.
    destination = route_long.strip()

    if not destination:

        destination = "Destination non disponible"

    return {
    "type": train_type,
    "number": commercial_number,
    "destination": destination,
    "departure_time": data.get(
        "departure_time",
        ""
    )
}


# =========================================================
# CALCUL DU RETARD
# =========================================================

def get_delay(trip_update):

    delays = []

    for stop in (
        trip_update.stop_time_update
    ):

        if (
            stop.HasField("arrival")
            and stop.arrival.HasField("delay")
        ):

            delays.append(
                stop.arrival.delay
            )

        elif (
            stop.HasField("departure")
            and stop.departure.HasField("delay")
        ):

            delays.append(
                stop.departure.delay
            )

    if not delays:
        return None

    # Le dernier retard connu
    # est le plus pertinent pour l'alerte.
    return round(
        delays[-1] / 60
    )


# =========================================================
# RETARDS
# =========================================================

def process_delays(
    feed,
    static_trips,
    state
):

    new_delays = {}

    notifications = 0

    for entity in feed.entity:

        if not entity.HasField(
            "trip_update"
        ):
            continue

        trip_update = entity.trip_update

        trip = trip_update.trip

        trip_id = trip.trip_id

        if not trip_id:
            continue

        train = identify_train(
            trip_id,
            static_trips
        )

        # TER et autres trains ignorés
        if not train:
            continue

        delay = get_delay(
            trip_update
        )

        if delay is None:
            continue

        new_delays[trip_id] = delay

        previous_delay = state[
            "delays"
        ].get(
            trip_id,
            0
        )

        # Premier passage au-dessus de 10 min
        new_delay = (
            previous_delay < MIN_DELAY
            and delay >= MIN_DELAY
        )

        # Hausse d'au moins 10 min
        increased_delay = (
            previous_delay >= MIN_DELAY
            and delay >= previous_delay + 10
        )

        if not (
            new_delay
            or increased_delay
        ):
            continue

        departure_time = train.get(
            "departure_time",
            ""
        )

        if departure_time:
            departure_time = (
                departure_time[:5]
            )
        else:
            departure_time = (
                "Heure non disponible"
            )

        # Calcul de l'heure de départ estimée
        estimated_time = ""

        if (
            departure_time !=
            "Heure non disponible"
        ):

            try:

                hours, minutes = map(
                    int,
                    departure_time.split(":")
                )

                total_minutes = (
                    hours * 60
                    + minutes
                    + delay
                )

                estimated_hours = (
                    total_minutes // 60
                ) % 24

                estimated_minutes = (
                    total_minutes % 60
                )

                estimated_time = (
                    f"{estimated_hours:02d}:"
                    f"{estimated_minutes:02d}"
                )

            except Exception:

                estimated_time = (
                    "Heure non disponible"
                )

        else:

            estimated_time = (
                "Heure non disponible"
            )

        title = (
            f"{train['type']} "
            f"{train['number']} "
            f"+{delay} min"
        )

        if increased_delay:

            message = (
                f"Départ prévu : "
                f"{departure_time}\n"
                f"Départ estimé : "
                f"{estimated_time}\n"
                f"Destination : "
                f"{train['destination']}\n"
                f"Retard actuel : +{delay} min\n"
                f"Retard précédent : "
                f"+{previous_delay} min"
            )

        else:

            message = (
                f"Départ prévu : "
                f"{departure_time}\n"
                f"Départ estimé : "
                f"{estimated_time}\n"
                f"Destination : "
                f"{train['destination']}\n"
                f"Retard : +{delay} min"
            )

        try:

            send_notification(
                title,
                message,
                "train,warning"
            )

            notifications += 1

            print(
                f"Alerte retard : "
                f"{train['type']} "
                f"{train['number']} "
                f"+{delay} min"
            )

        except Exception as error:

            print(
                f"Erreur notification : "
                f"{error}"
            )

    return (
        new_delays,
        notifications
    )


# =========================================================
# SUPPRESSIONS
# =========================================================

def process_cancellations(
    feed,
    static_trips,
    state
):

    new_cancellations = dict(
        state["cancellations"]
    )

    notifications = 0

    for entity in feed.entity:

        if not entity.HasField(
            "alert"
        ):
            continue

        alert = entity.alert

        # -------------------------------------------------
        # GTFS-RT : NO_SERVICE = service supprimé
        # -------------------------------------------------

        if alert.effect != (
            gtfs_realtime_pb2.Alert.NO_SERVICE
        ):

            continue

        for informed in (
            alert.informed_entity
        ):

            if not informed.HasField(
                "trip"
            ):

                continue

            trip_id = (
                informed.trip.trip_id
            )

            if not trip_id:
                continue

            train = identify_train(
                trip_id,
                static_trips
            )

            # TER et autres trains ignorés
            if not train:
                continue

            # -------------------------------------------------
            # CLÉ ANTI-SPAM
            # -------------------------------------------------

            cancellation_key = (
                f"{entity.id}:{trip_id}"
            )

            if cancellation_key in (
                state["cancellations"]
            ):

                continue

            new_cancellations[
                cancellation_key
            ] = True

            title = (
                f"{train['type']} "
                f"{train['number']} "
                f"SUPPRIMÉ"
            )

            message = (
                f"Destination : "
                f"{train['destination']}\n"
                f"Le train est indiqué "
                f"comme supprimé par SNCF."
            )

            # Ajout du message SNCF si disponible
            if alert.HasField(
                "header_text"
            ):

                header = (
                    alert.header_text
                    .translation[0]
                    .text
                )

                if header:
                    message += (
                        f"\n\n{header}"
                    )

            try:

                send_notification(
                    title,
                    message,
                    "train,no_entry"
                )

                notifications += 1

                print(
                    f"Alerte suppression : "
                    f"{train['type']} "
                    f"{train['number']}"
                )

            except Exception as error:

                print(
                    f"Erreur notification : "
                    f"{error}"
                )

    return (
        new_cancellations,
        notifications
    )


# =========================================================
# PROGRAMME PRINCIPAL
# =========================================================

def main():

    if not NTFY_TOPIC:

        raise RuntimeError(
            "Le secret NTFY_TOPIC est absent."
        )

    print(
        "================================"
    )

    print(
        "SURVEILLANCE SNCF"
    )

    print(
        "TGV INOUI / OUIGO / INTERCITÉS"
    )

    print(
        "TER EXCLUS"
    )

    print(
        "================================"
    )

    state = load_state()

    static_trips = get_static_data()

    # -----------------------------------------------------
    # RETARDS
    # -----------------------------------------------------

    print(
        "Lecture des retards..."
    )

    delay_feed = get_feed(
        TRIP_UPDATES_URL
    )

    (
        new_delays,
        delay_notifications
    ) = process_delays(
        delay_feed,
        static_trips,
        state
    )

    # -----------------------------------------------------
    # SUPPRESSIONS
    # -----------------------------------------------------

    print(
        "Lecture des suppressions..."
    )

    alert_feed = get_feed(
        SERVICE_ALERTS_URL
    )

    (
        new_cancellations,
        cancellation_notifications
    ) = process_cancellations(
        alert_feed,
        static_trips,
        state
    )

    # -----------------------------------------------------
    # SAUVEGARDE
    # -----------------------------------------------------

    state = {
        "delays": new_delays,
        "cancellations": new_cancellations
    }

    save_state(state)

    total = (
        delay_notifications
        + cancellation_notifications
    )

    print(
        "================================"
    )

    print(
        f"Terminé : {total} notification(s)"
    )

    print(
        "================================"
    )


if __name__ == "__main__":
    main()