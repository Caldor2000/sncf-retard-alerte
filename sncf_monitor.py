import os
import json
import requests
from google.transit import gtfs_realtime_pb2

FEED_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
NTFY_URL = "https://ntfy.sh"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

STATE_FILE = "state.json"
MIN_DELAY = 10  # retard minimum en minutes


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


def get_feed():
    response = requests.get(FEED_URL, timeout=30)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    return feed


def get_train_name(trip):
    route = trip.route_id or "SNCF"
    train_number = trip.trip_id

    return f"{route} — {train_number}"


def main():
    state = load_state()
    feed = get_feed()

    new_state = {}

    for entity in feed.entity:

        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip = trip_update.trip

        trip_id = trip.trip_id

        if not trip_id:
            continue

        # On cherche le retard maximal parmi les prochaines étapes
        delays = []

        for stop in trip_update.stop_time_update:

            if stop.HasField("arrival") and stop.arrival.HasField("delay"):
                delays.append(stop.arrival.delay)

            if stop.HasField("departure") and stop.departure.HasField("delay"):
                delays.append(stop.departure.delay)

        if not delays:
            continue

        max_delay_seconds = max(delays)
        delay_minutes = round(max_delay_seconds / 60)

        new_state[trip_id] = delay_minutes

        previous_delay = state.get(trip_id, 0)

        # Nouveau retard >= 10 min
        new_delay = (
            previous_delay < MIN_DELAY
            and delay_minutes >= MIN_DELAY
        )

        # Retard qui augmente fortement
        increased_delay = (
            previous_delay >= MIN_DELAY
            and delay_minutes >= previous_delay + 10
        )

        if new_delay or increased_delay:

            train_name = get_train_name(trip)

            if new_delay:
                title = f"Retard SNCF : +{delay_minutes} min"
            else:
                title = f"Retard SNCF : +{delay_minutes} min"

            message = (
                f"{train_name}\n"
                f"Retard détecté : +{delay_minutes} min"
            )

            try:
                send_notification(title, message)
                print(f"Notification envoyée : {train_name} +{delay_minutes} min")
            except Exception as e:
                print(f"Erreur notification : {e}")

    save_state(new_state)


if __name__ == "__main__":
    main()