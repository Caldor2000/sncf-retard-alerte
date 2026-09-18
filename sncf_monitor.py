import os
import json
import csv
import io
import zipfile
import requests
from google.transit import gtfs_realtime_pb2

REALTIME_URL = (
    "https://proxy.transport.data.gouv.fr/"
    "resource/sncf-gtfs-rt-trip-updates"
)

STATIC_URL = (
    "https://eu.ftp.opendatasoft.com/sncf/plandata/"
    "Export_OpenData_SNCF_GTFS_NewTripId.zip"
)

NTFY_URL = "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

STATE_FILE = "state.json"
MIN_DELAY = 10


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def send_notification(title, message):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC n'est pas configuré.")

    response = requests.post(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "train,warning",
        },
        data=message.encode("utf-8"),
        timeout=15,
    )

    response.raise_for_status()


def get_realtime_feed():
    response = requests.get(
        REALTIME_URL,
        timeout=30
    )
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    return feed


def get_static_trip_data():
    print("Téléchargement des données horaires SNCF...")

    response = requests.get(
        STATIC_URL,
        timeout=60
    )
    response.raise_for_status()

    trips = {}

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:

        with z.open("trips.txt") as f:
            text = io.TextIOWrapper(
                f,
                encoding="utf-8-sig",
                newline=""
            )

            reader = csv.DictReader(text)

            for row in reader:

                trip_id = row.get("trip_id", "")

                if not trip_id:
                    continue

                trips[trip_id] = {
                    "route_short_name": (
                        row.get("route_short_name", "")
                    ),
                    "trip_short_name": (
                        row.get("trip_short_name", "")
                    ),
                    "trip_headsign": (
                        row.get("trip_headsign", "")
                    ),
                }

    print(f"{len(trips)} trajets horaires chargés.")

    return trips


def get_train_information(trip_id, static_trips):

    data = static_trips.get(trip_id)

    if not data:
        return {
            "number": trip_id,
            "destination": ""
        }

    number = (
        data.get("trip_short_name")
        or data.get("route_short_name")
        or trip_id
    )

    destination = data.get("trip_headsign", "")

    return {
        "number": number,
        "destination": destination
    }


def get_delay(trip_update):

    delays = []

    for stop in trip_update.stop_time_update:

        delay = None

        if (
            stop.HasField("arrival")
            and stop.arrival.HasField("delay")
        ):
            delay = stop.arrival.delay

        elif (
            stop.HasField("departure")
            and stop.departure.HasField("delay")
        ):
            delay = stop.departure.delay

        if delay is not None:
            delays.append(delay)

    if not delays:
        return None

    # Le dernier retard fourni correspond au point
    # le plus avancé connu sur le trajet.
    return round(delays[-1] / 60)


def main():

    if not NTFY_TOPIC:
        raise RuntimeError(
            "Le secret NTFY_TOPIC est absent."
        )

    state = load_state()

    static_trips = get_static_trip_data()
    feed = get_realtime_feed()

    new_state = {}

    notifications = 0

    for entity in feed.entity:

        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip = trip_update.trip

        trip_id = trip.trip_id

        if not trip_id:
            continue

        delay_minutes = get_delay(trip_update)

        if delay_minutes is None:
            continue

        train = get_train_information(
            trip_id,
            static_trips
        )

        train_number = train["number"]
        destination = train["destination"]

        # On conserve le dernier état connu
        new_state[trip_id] = delay_minutes

        previous_delay = state.get(trip_id, 0)

        # Nouveau retard >= 10 minutes
        new_delay = (
            previous_delay < MIN_DELAY
            and delay_minutes >= MIN_DELAY
        )

        # Nouvelle aggravation d'au moins 10 minutes
        increased_delay = (
            previous_delay >= MIN_DELAY
            and delay_minutes >= previous_delay + 10
        )

        if not (new_delay or increased_delay):
            continue

        if increased_delay:
            title = (
                f"Retard SNCF : +{delay_minutes} min"
            )
        else:
            title = (
                f"Nouveau retard SNCF : +{delay_minutes} min"
            )

        message_lines = [
            f"Train : {train_number}"
        ]

        if destination:
            message_lines.append(
                f"Destination : {destination}"
            )

        message_lines.append(
            f"Retard : +{delay_minutes} min"
        )

        if increased_delay:
            message_lines.append(
                f"Précédent : +{previous_delay} min"
            )

        message = "\n".join(message_lines)

        try:

            send_notification(
                title,
                message
            )

            notifications += 1

            print(
                f"Notification envoyée : "
                f"{train_number} "
                f"+{delay_minutes} min"
            )

        except Exception as error:

            print(
                f"Erreur notification : {error}"
            )

    save_state(new_state)

    print(
        f"Surveillance terminée : "
        f"{notifications} notification(s)."
    )


if __name__ == "__main__":
    main()