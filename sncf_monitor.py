import os
import json
import csv
import io
import zipfile
import re
from datetime import datetime, timedelta
import requests
from google.transit import gtfs_realtime_pb2
TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
SERVICE_ALERTS_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"
GTFS_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = "state.json"
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"delays": {}, "cancellations": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if "delays" not in state:
            state = {
                "delays": state,
                "cancellations": {}
            }
        state.setdefault("delays", {})
        state.setdefault("cancellations", {})
        return state
    except Exception:
        return {"delays": {}, "cancellations": {}}
def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
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
def download_feed(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed
def load_static_data():
    print("Téléchargement du GTFS statique...")
    response = requests.get(GTFS_URL, timeout=60)
    response.raise_for_status()
    routes = {}
    trips = {}
    first_departures = {}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        with archive.open("routes.txt") as file:
            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )
            for row in csv.DictReader(text):
                routes[row["route_id"]] = {
                    "short_name": row.get("route_short_name", ""),
                    "long_name": row.get("route_long_name", "")
                }
        with archive.open("trips.txt") as file:
            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )
            for row in csv.DictReader(text):
                trip_id = row.get("trip_id", "")
                if trip_id:
                    trips[trip_id] = {
                        "route_id": row.get("route_id", ""),
                        "trip_short_name": row.get("trip_short_name", ""),
                        "trip_headsign": row.get("trip_headsign", ""),
                        "departure_time": ""
                    }
        with archive.open("stop_times.txt") as file:
            text = io.TextIOWrapper(
                file,
                encoding="utf-8-sig",
                newline=""
            )
            for row in csv.DictReader(text):
                trip_id = row.get("trip_id", "")
                if trip_id not in trips:
                    continue
                try:
                    sequence = int(row.get("stop_sequence", ""))
                except Exception:
                    continue
                departure = row.get("departure_time", "")
                if not departure:
                    continue
                old = first_departures.get(trip_id)
                if old is None or sequence < old["sequence"]:
                    first_departures[trip_id] = {
                        "sequence": sequence,
                        "departure_time": departure
                    }
    for trip_id, data in first_departures.items():
        if trip_id in trips:
            trips[trip_id]["departure_time"] = data["departure_time"]
    print(f"{len(routes)} lignes chargées")
    print(f"{len(trips)} trains chargés")
    return routes, trips
def identify_train(trip_id, routes, trips):
    data = trips.get(trip_id)
    if not data:
        return None
    route = routes.get(data["route_id"], {})
    route_short = route.get("short_name", "")
    route_long = route.get("long_name", "")
    trip_short = data.get("trip_short_name", "")
    trip_headsign = data.get("trip_headsign", "")
    text = " ".join([
        route_short,
        route_long,
        trip_short,
        trip_headsign
    ]).upper()
    if "TER" in text or "TRANSILIEN" in text or "RER" in text:
        return None
    if "OUIGO" in text:
        train_type = "OUIGO"
    elif "INTERCITÉS" in text or "INTERCITES" in text:
        train_type = "INTERCITÉS"
    elif "INOUI" in text or "TGV INOUI" in text:
        train_type = "TGV INOUI"
    else:
        return None
    number = ""
    if trip_headsign.strip().isdigit():
        number = trip_headsign.strip()
    elif trip_short.strip().isdigit():
        number = trip_short.strip()
    else:
        numbers = re.findall(r"\b\d{3,5}\b", trip_headsign)
        if numbers:
            number = numbers[0]
    if not number:
        number = "numéro non dispo"
    destination = route_long.strip()
    if not destination:
        destination = "Destination non disponible"
    return {
        "type": train_type,
        "number": number,
        "destination": destination,
        "departure_time": data.get("departure_time", "")
    }
def get_delay(trip_update):
    delay = None
    for stop in trip_update.stop_time_update:
        if stop.HasField("departure"):
            if stop.departure.HasField("delay"):
                delay = stop.departure.delay
        if stop.HasField("arrival"):
            if stop.arrival.HasField("delay"):
                delay = stop.arrival.delay
    return delay if delay is not None else 0
def format_time(value):
    if not value:
        return "Non disponible"
    return value[:5]
def estimated_time(scheduled, delay_minutes):
    if not scheduled:
        return "Non disponible"
    try:
        hour = int(scheduled[:2])
        minute = int(scheduled[3:5])
        base = datetime(2000, 1, 1, hour, minute)
        result = base + timedelta(minutes=delay_minutes)
        return result.strftime("%H:%M")
    except Exception:
        return "Non disponible"
def process_delays(feed, routes, trips, state):
    old_delays = state["delays"]
    new_delays = {}
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
        delay_minutes = int(round(delay_seconds / 60))
        new_delays[trip_id] = delay_minutes
        if delay_minutes < 10:
            continue
        previous = old_delays.get(trip_id, 0)
        first_alert = previous < 10
        increased = delay_minutes >= previous + 10
        if not first_alert and not increased:
            continue
        scheduled = format_time(train["departure_time"])
        estimated = estimated_time(
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
        if previous >= 10:
            message += f"\nRetard précédent : +{previous} min"
        notify(
            f"{train['type']} {train['number']} en retard",
            message
        )
    state["delays"] = new_delays
def process_cancellations(feed, routes, trips, state):
    old_cancellations = state["cancellations"]
    new_cancellations = {}
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        if alert.effect != gtfs_realtime_pb2.Alert.NO_SERVICE:
            continue
        for informed in alert.informed_entity:
            if not informed.HasField("trip"):
                continue
            trip_id = informed.trip.trip_id
            if not trip_id:
                continue
            train = identify_train(
                trip_id,
                routes,
                trips
            )
            if not train:
                continue
            key = f"{entity.id}:{trip_id}"
            new_cancellations[key] = True
            if key in old_cancellations:
                continue
            scheduled = format_time(
                train["departure_time"]
            )
            message = (
                f"{train['type']} {train['number']} SUPPRIMÉ\n"
                f"Départ prévu : {scheduled}\n"
                f"Destination : {train['destination']}\n"
                f"Le train est indiqué comme supprimé par SNCF."
            )
            notify(
                f"{train['type']} {train['number']} SUPPRIMÉ",
                message
            )
    state["cancellations"] = new_cancellations
def main():
    print("================================")
    print("SNCF MONITOR")
    print("================================")
    state = load_state()
    routes, trips = load_static_data()
    print("Téléchargement des retards...")
    trip_feed = download_feed(TRIP_UPDATES_URL)
    print(
        f"{len(trip_feed.entity)} mises à jour reçues"
    )
    process_delays(
        trip_feed,
        routes,
        trips,
        state
    )
    print("Téléchargement des suppressions...")
    alert_feed = download_feed(SERVICE_ALERTS_URL)
    print(
        f"{len(alert_feed.entity)} alertes reçues"
    )
    process_cancellations(
        alert_feed,
        routes,
        trips,
        state
    )
    save_state(state)
    print("Surveillance terminée.")
if __name__ == "__main__":
    main()