import os
import json
import csv
import io
import zipfile
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


# ---------------------------------------------------------
# MÉMOIRE
# ---------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "delays": {},
            "cancellations": {}
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------------------------------------------------------
# NOTIFICATIONS NTFY
# ---------------------------------------------------------

def send_notification(title, message, tags="train"):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC n'est pas configuré.")

    response = requests.post(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": tags,
        },
        data=message.encode("utf-8"),
        timeout=15,
    )

    response.raise_for_status()


# ---------------------------------------------------------
# DONNÉES TEMPS RÉEL
# ---------------------------------------------------------

def get_feed(url):
    response = requests.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    return feed


# ---------------------------------------------------------
# DONNÉES STATIQUES SNCF
# ---------------------------------------------------------

def get_static_data():

    print("Téléchargement des données horaires SNCF...")

    response = requests.get(
        STATIC_URL,
        timeout=60
    )

    response.raise_for_status()

    routes = {}
    trips = {}

    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as z:

        # -------------------------
        # routes.txt
        # -------------------------

        with z.open("routes.txt") as f:

            text = io.TextIOWrapper(
                f,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:

                route_id = row.get("route_id", "")

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

        # -------------------------
        # trips.txt
        # -------------------------

        with z.open("trips.txt") as f:

            text = io.TextIOWrapper(
                f,
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

                    "route_short_name": route.get(
                        "short_name",
                        ""
                    ),

                    "route_long_name": route.get(
                        "long_name",
                        ""
                    ),

                    "trip_short_name": row.get(
                        "trip_short_name",
                        ""
                    ),

                    "trip_headsign": row.get(
                        "trip_headsign",
                        ""
                    )
                }

    print(
        f"{len(trips)} trajets chargés."
    )

    return trips


# ---------------------------------------------------------
# FILTRE : INOUI / OUIGO / INTERCITÉS
# ---------------------------------------------------------

def identify_train(trip_id, static_trips):

    data = static_trips.get(trip_id)

    if not data:
        return None

    text = " ".join([
        data.get("route_short_name", ""),
        data.get("route_long_name", ""),
        data.get("trip_short_name", "")
    ]).upper()

    # TER = EXCLU
    if "TER" in text:
        return None

    # INOUI
    if "INOUI" in text or "TGV" in text:
        train_type = "TGV INOUI"

    # OUIGO
    elif "OUIGO" in text:
        train_type = "OUIGO"

    # INTERCITÉS
    elif (
        "INTERCITES" in text
        or "INTERCITÉS" in text
        or "INTERCITE" in text
    ):
        train_type = "INTERCITÉS"

    else:
        return None

    number = (
        data.get("trip_short_name")
        or data.get("route_short_name")
        or trip_id
    )

    destination = data.get(
        "trip_headsign",
        ""
    )

    return {
        "type": train_type,
        "number": number,
        "destination": destination,
        "route_id": data.get(
            "route_id",
            ""
        )
    }


# ---------------------------------------------------------
# RETARD
# ---------------------------------------------------------

def get_delay(trip_update):

    delays = []

    for stop in trip_update.stop_time_update:

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

    return round(
        delays[-1] / 60
    )


# ---------------------------------------------------------
# SURVEILLANCE DES RETARDS
# ---------------------------------------------------------

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

        # Ignore TER + autres trains
        if not train:
            continue

        delay = get_delay(
            trip_update
        )

        if delay is None:
            continue

        new_delays[trip_id] = delay

        previous_delay = state["delays"].get(
            trip_id,
            0
        )

        new_delay = (
            previous_delay < MIN_DELAY
            and delay >= MIN_DELAY
        )

        increased_delay = (
            previous_delay >= MIN_DELAY
            and delay >= previous_delay + 10
        )

        if not (
            new_delay
            or increased_delay
        ):
            continue

        if increased_delay:
            title = (
                f"Retard {train['type']} "
                f"+{delay} min"
            )
        else:
            title = (
                f"Nouveau retard "
                f"{train['type']} "
                f"+{delay} min"
            )

        message = (
            f"Train : {train['number']}\n"
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
                f"Notification retard : "
                f"{train['type']} "
                f"{train['number']} "
                f"+{delay} min"
            )

        except Exception as error:

            print(
                f"Erreur notification : "
                f"{error}"
            )

    return new_delays, notifications


# ---------------------------------------------------------
# SUPPRESSIONS
# ---------------------------------------------------------

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

        # Nous recherchons uniquement
        # les suppressions de circulation.
        if alert.effect != (
            gtfs_realtime_pb2.Alert.CANCELED
        ):
            continue

        for informed in alert.informed_entity:

            trip_id = ""

            if informed.HasField(
                "trip"
            ):
                trip_id = informed.trip.trip_id

            if not trip_id:
                continue

            train = identify_train(
                trip_id,
                static_trips
            )

            # Ignore TER
            if not train:
                continue

            cancellation_key = (
                trip_id
                + "_cancelled"
            )

            if cancellation_key in state[
                "cancellations"
            ]:
                continue

            new_cancellations[
                cancellation_key
            ] = True

            title = (
                f"Train supprimé - "
                f"{train['type']}"
            )

            message = (
                f"Train : {train['number']}\n"
                f"Destination : "
                f"{train['destination']}\n"
                f"Le train est indiqué "
                f"comme supprimé."
            )

            try:

                send_notification(
                    title,
                    message,
                    "train,no_entry"
                )

                notifications += 1

                print(
                    f"Notification suppression : "
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


# ---------------------------------------------------------
# PROGRAMME PRINCIPAL
# ---------------------------------------------------------

def main():

    if not NTFY_TOPIC:
        raise RuntimeError(
            "Le secret NTFY_TOPIC est absent."
        )

    state = load_state()

    static_trips = get_static_data()

    # -------------------------------------
    # RETARDS
    # -------------------------------------

    print(
        "Lecture du flux des retards..."
    )

    delay_feed = get_feed(
        TRIP_UPDATES_URL
    )

    new_delays, delay_notifications = (
        process_delays(
            delay_feed,
            static_trips,
            state
        )
    )

    # -------------------------------------
    # SUPPRESSIONS
    # -------------------------------------

    print(
        "Lecture du flux des suppressions..."
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

    # -------------------------------------
    # SAUVEGARDE
    # -------------------------------------

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
        f"Surveillance terminée : "
        f"{total} notification(s)."
    )


if __name__ == "__main__":
    main()