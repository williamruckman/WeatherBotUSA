import sys
import time
import threading
import meshtastic
import meshtastic.tcp_interface
import mysql.connector
import requests
import re
from pubsub import pub
from geopy.geocoders import Nominatim
from functools import wraps
import urllib.parse

# Define the hostname variable. The IP of the Meshtastic node to connect to.
hostname = '127.0.0.1'

# User Agent for api.weather.gov configuration
headers = {
    'User-Agent': 'Weather Bot USA for Meshtastic, william@nwomesh.org'
}

# User Agent for Nominatim configuration
user_agent = "Weather Bot USA for Meshtastic, william@nwomesh.org"

# MySQL Database configuration
DB_CONFIG = {
    'user': 'meshtastic_weather',
    'password': 'PASSWORD_CHANGEME',
    'host': '127.0.0.1',
    'database': 'meshtastic_weather'
}

# Banner to be printed when the script starts
banner = r"""
-----------------------------------------------
     __   ___     __ __  __  __ ___     __
|  ||_  /\ | |__||_ |__)|__)/  \ | /  \(_  /\
|/\||__/--\| |  ||__| \ |__)\__/ | \__/__)/--\

FOR: Meshtastic (https://meshtastic.org/)
BY: William Ruckman (KE8SRG)
CONTACT: William@NWOMesh.org

-----------------------------------------------
"""

# Print the banner
print(banner)

# Pause for 3 seconds for banner display at script start
time.sleep(3)

# define logic for retries to weather.gov API on failure
def retry(api_function):
    @wraps(api_function)
    def wrapper_with_retry(*args, **kwargs):
        max_retries = 3
        wait_time = 2  # seconds
        for attempt in range(max_retries):
            try:
                return api_function(*args, **kwargs)
            except requests.exceptions.RequestException as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                else:
                    raise
    return wrapper_with_retry

# Database functions
def connect_to_database():
    try:
        db = mysql.connector.connect(**DB_CONFIG)
        return db
    except mysql.connector.Error as err:
        print(f"Error connecting to database: {err}")
        return None

# Clean latitude and longitude formatting
def clean_coordinates(coord):
    """ Clean the latitude/longitude by removing non-numeric characters. """
    return float(coord.replace("°", "").replace("m", "").strip())

# Add node to the database and check for weather alerts immediately
def register_node(node_id, latitude, longitude):
    latitude = clean_coordinates(latitude)
    longitude = clean_coordinates(longitude)

    db = connect_to_database()
    if db:
        cursor = db.cursor()
        query = "INSERT INTO severe_weather_subscriptions (node_id, latitude, longitude) VALUES (%s, %s, %s)"
        cursor.execute(query, (node_id, latitude, longitude))
        db.commit()
        cursor.close()
        db.close()
        # Perform an immediate weather alert check upon registration
        send_weather_alerts(node_id, latitude, longitude, is_initial_check=True)

# Remove node from the subscription database
def remove_node(node_id):
    db = connect_to_database()
    if db:
        cursor = db.cursor()
        query = "DELETE FROM severe_weather_subscriptions WHERE node_id = %s"
        cursor.execute(query, (node_id,))
        db.commit()
        cursor.close()
        db.close()

# Weather alert retrieval
@retry
def get_weather_alerts(latitude, longitude):
    url = f"https://api.weather.gov/alerts/active?point={latitude}%2C{longitude}&limit=500"
    print(f"Requesting URL: {url}")
    try:
        response = requests.get(url, headers=headers)
        print(f"Response status code: {response.status_code}")
        response.raise_for_status()  # Raise an exception for HTTP errors (e.g., 500 errors)
        data = response.json()
        print(f"Number of alerts fetched: {len(data.get('features', []))}")
        return data.get('features', [])
    except requests.exceptions.RequestException as e:
        print(f"Exception occurred while fetching weather alerts: {e}")
        raise  # Required for the retry decorator to handle the exception

# Send message to node
def send_message(node_id, message):
    # Define the maximum size for each data packet (adjust based on Meshtastic limitations)
    max_packet_size = 220  # Example size, lower if you have issues; Estimated for longfast, 5 hops, and encryption

    # Split the message into chunks if it exceeds the maximum size
    message_parts = [message[i:i + max_packet_size] for i in range(0, len(message), max_packet_size)]

    # Send each part individually
    for part in message_parts:
        interface.sendText(part, node_id)
        print(f"Sent message to node {node_id}: {part}")

# Send weather alerts
def send_weather_alerts(node_id, latitude, longitude, is_initial_check=False):
    print(f"Fetching weather alerts for node {node_id} at ({latitude}, {longitude})...")
    alerts = get_weather_alerts(latitude, longitude)

    if alerts:
        print(f"Number of alerts for node {node_id}: {len(alerts)}")
        for alert in alerts:
            headline = alert['properties'].get('headline', 'No headline available')
            print(f"Alert for node {node_id}: {headline}")
            send_message(node_id, f"⚠️Weather alert⚠️: {headline}")
    elif is_initial_check:
        print(f"No active weather alerts for node {node_id} at ({latitude}, {longitude}).")
        send_message(node_id, "No active weather alerts for your location.")

# Periodic weather alert check
def check_weather_alerts():
    db = connect_to_database()
    if db:
        cursor = db.cursor(dictionary=True)
        query = "SELECT node_id, latitude, longitude FROM severe_weather_subscriptions"
        cursor.execute(query)
        nodes = cursor.fetchall()

        for node in nodes:
            node_id = node['node_id']
            db_latitude = node['latitude']
            db_longitude = node['longitude']

            # Fetch updated location
            updated_latitude, updated_longitude = get_node_info(node_id)

            # Validate updated coordinates
            if (
                updated_latitude is not None and updated_longitude is not None
                and is_valid_coordinates(updated_latitude, updated_longitude)
                and (
                    float(updated_latitude) != float(db_latitude)
                    or float(updated_longitude) != float(db_longitude)
                )
            ):
                print(f"Updating location for node {node_id} to ({updated_latitude}, {updated_longitude})...")
                # Update database with new coordinates
                update_query = "UPDATE severe_weather_subscriptions SET latitude = %s, longitude = %s WHERE node_id = %s"
                cursor.execute(update_query, (updated_latitude, updated_longitude, node_id))
                db.commit()
                latitude, longitude = updated_latitude, updated_longitude
            else:
                # Keep existing coordinates
                print(f"Using existing location for node {node_id} ({db_latitude}, {db_longitude})...")
                latitude, longitude = db_latitude, db_longitude

            # Send weather alerts
            send_weather_alerts(node_id, latitude, longitude)

        cursor.close()
        db.close()

    # Schedule the next execution in 15 minutes
    threading.Timer(900, check_weather_alerts).start()

# Parse node information data returned from Meshtastic API
def parse_nodes_data(nodes_data):
    # Split the raw data by new lines to get each row
    rows = nodes_data.split("\n")

    # Initialize a list to hold the nodes as dictionaries
    nodes = []

    for row in rows:
        # Skip the separator rows
        if row.startswith("╒") or row.startswith("╞"):
            continue

        # Split each row into columns (use '│' as delimiter)
        columns = [col.strip() for col in row.split("│") if col.strip()]

        # Ensure there are enough columns to process a node
        if len(columns) >= 12:
            node = {
                "ID": columns[2],
                "User": columns[1],
                "Latitude": columns[6],
                "Longitude": columns[7],
                "Altitude": columns[8],
                # Add more columns as needed
            }
            nodes.append(node)

    return nodes

# Get node information
def get_node_info(node_id):
    try:
        # Convert node_id to integer
        int_node_id = int(node_id)
        hex_node_id = f"!{hex(int_node_id)[2:]}"  # Convert node ID to hex format with '!' prefix
        nodes_data = interface.showNodes()  # Get nodes data
        nodes = parse_nodes_data(nodes_data)  # Parse the data into a list of dictionaries

        for n in nodes:
            if n.get('ID', '') == hex_node_id:
                latitude = n.get('Latitude')
                longitude = n.get('Longitude')
                # Clean up and validate coordinates before returning
                if latitude and longitude:
                    latitude = clean_coordinates(latitude)
                    longitude = clean_coordinates(longitude)
                    if is_valid_coordinates(latitude, longitude):
                        return latitude, longitude
                    else:
                        print(f"Node {hex_node_id} has invalid coordinates: ({latitude}, {longitude}).")
                        return None, None
                else:
                    print(f"Node {hex_node_id} has missing coordinates.")
                    return None, None

        print(f"Node {node_id} not found.")
        return None, None
    except ValueError:
        print(f"Invalid node ID: {node_id}")
        return None, None

# Verify that lonitude and latitude are within range
def is_valid_coordinates(latitude, longitude):
    """Validate latitude and longitude values."""
    try:
        lat = float(latitude)
        lon = float(longitude)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return True
    except ValueError:
        pass
    return False

# Get latutude and longitude from city, state
def get_coordinates_from_city_state(city_state):
    geolocator = Nominatim(user_agent=user_agent)
    location = geolocator.geocode(f"{city_state}, USA")  # Append 'USA'

    if location:
        if "United States" in location.address:
            print(f"Found location: {location.address} at ({location.latitude}, {location.longitude})")
            return location.latitude, location.longitude, city_state  # Include full input
        else:
            print(f"Location is not in the United States: {location.address}")
            return None, None, city_state
    else:
        print(f"Unable to resolve coordinates for {city_state}.")
        return None, None, city_state

# Get todays current weather
@retry
def get_current_weather(location):
    try:
        print(f"Received location: {location}")  # Debugging line
        if ',' in location:  # City, State format
            latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location)
            print(f"Resolved city/state: {resolved_city_state}, Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
            if latitude and longitude:
                location_str = resolved_city_state
            else:
                return f"Could not resolve location '{location}'. Please provide valid coordinates."
        else:  # Latitude, Longitude format
            try:
                # Remove any non-numeric characters (like '°') before splitting
                location = location.replace('°', '').strip()
                latitude, longitude = map(float, location.split())
                print(f"Parsed coordinates: Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                latitude = clean_coordinates(str(latitude))
                longitude = clean_coordinates(str(longitude))
                print(f"Cleaned coordinates: Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                if is_valid_coordinates(latitude, longitude):
                    location_str = f"Coordinates: {latitude}, {longitude}"
                else:
                    return "Invalid latitude/longitude format. Please provide valid coordinates."
            except ValueError:
                return "Invalid input. Please use 'City, State' or 'latitude longitude'. This may be due to no GPS location."

        # Construct the API URL for points
        points_url = f"https://api.weather.gov/points/{latitude},{longitude}"
        print(f"Fetching points data from: {points_url}")

        # Make the API request to get the point data
        points_response = requests.get(points_url, headers=headers)
        points_response.raise_for_status()  # Check for errors

        # Extract the forecast URL
        points_data = points_response.json()
        forecast_url = points_data['properties']['forecast']
        print(f"Forecast URL: {forecast_url}")

        # Fetch the current weather data from the forecast URL
        weather_response = requests.get(forecast_url, headers=headers)
        weather_response.raise_for_status()  # Check for errors

        weather_data = weather_response.json()
        current_conditions = weather_data['properties']['periods'][0]['detailedForecast']

        return f"{current_conditions}"

    except requests.exceptions.RequestException as e:
        print(f"Error fetching current weather: {e}")
        return f"Error fetching weather for {location}. Please try again later. Sometimes this is due to the weather.gov API."

# Get tomorrows weather
@retry
def get_tomorrow_weather(location):
    try:
        print(f"Received location: {location}")  # Debugging line
        if ',' in location:  # City, State format
            latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location)
            print(f"Resolved city/state: {resolved_city_state}, Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
            if latitude and longitude:
                location_str = resolved_city_state
            else:
                return f"Could not resolve location '{location}'. Please provide valid coordinates."
        else:  # Latitude, Longitude format
            try:
                location = location.replace('°', '').strip()
                latitude, longitude = map(float, location.split())
                print(f"Parsed coordinates: Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                latitude = clean_coordinates(str(latitude))
                longitude = clean_coordinates(str(longitude))
                print(f"Cleaned coordinates: Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                if is_valid_coordinates(latitude, longitude):
                    location_str = f"Coordinates: {latitude}, {longitude}"
                else:
                    return "Invalid latitude/longitude format. Please provide valid coordinates."
            except ValueError:
                return "Invalid input. Please use 'City, State' or 'latitude longitude'. This may be due to no GPS location."

        # Construct the API URL for points
        points_url = f"https://api.weather.gov/points/{latitude},{longitude}"
        print(f"Fetching points data from: {points_url}")

        # Make the API request to get the point data
        points_response = requests.get(points_url, headers=headers)
        points_response.raise_for_status()

        # Extract the forecast URL
        points_data = points_response.json()
        forecast_url = points_data['properties']['forecast']
        print(f"Forecast URL: {forecast_url}")

        # Fetch the weather data from the forecast URL
        weather_response = requests.get(forecast_url, headers=headers)
        weather_response.raise_for_status()

        weather_data = weather_response.json()
        tomorrow_conditions = weather_data['properties']['periods'][1]['detailedForecast']

        return f"{tomorrow_conditions}"

    except requests.exceptions.RequestException as e:
        print(f"Error fetching tomorrow's weather: {e}")
        raise  # Required for the retry decorator to handle the exception

# Get the 5 day forecast
@retry
def get_five_day_forecast(location):
    try:
        print(f"Received location: {location}")  # Debugging line
        if ',' in location:  # City, State format
            latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location)
            print(f"Resolved city/state: {resolved_city_state}, Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
            if not (latitude and longitude):
                return f"Could not resolve location '{location}'. Please provide valid coordinates."
        else:  # Latitude, Longitude format
            try:
                location = location.replace('°', '').strip()
                latitude, longitude = map(float, location.split())
                print(f"Parsed coordinates: Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                latitude = clean_coordinates(str(latitude))
                longitude = clean_coordinates(str(longitude))
                print(f"Cleaned coordinates: Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                if not is_valid_coordinates(latitude, longitude):
                    return "Invalid latitude/longitude format. Please provide valid coordinates."
            except ValueError:
                return "Invalid input. Please use 'City, State' or 'latitude longitude'. This may be due to no GPS location."

        # Construct the API URL for points
        points_url = f"https://api.weather.gov/points/{latitude},{longitude}"
        print(f"Fetching points data from: {points_url}")

        # Fetch point data
        points_response = requests.get(points_url, headers=headers)
        points_response.raise_for_status()

        # Extract the forecast URL
        points_data = points_response.json()
        forecast_url = points_data['properties']['forecast']
        print(f"Forecast URL: {forecast_url}")

        # Fetch the weather data from the forecast URL
        weather_response = requests.get(forecast_url, headers=headers)
        weather_response.raise_for_status()

        weather_data = weather_response.json()
        periods = weather_data['properties']['periods']

        # Build the 5-day forecast messages
        forecast_messages = []
        for period in periods[:10]:  # First 10 periods (5 days: day and night)
            header = f"{period['name']}:\n"
            message = f"{header}{period['detailedForecast']}"
            forecast_messages.append(message)

        return forecast_messages

    except requests.exceptions.RequestException as e:
        print(f"Error fetching 5-day forecast: {e}")
        return f"Error fetching forecast for {location}. Please try again later."

# Handle incoming message
def handle_message(packet):
    try:
        my_node_info = interface.getMyNodeInfo()
        my_node_id = my_node_info['num']  # Get the node's numeric ID

        if 'to' in packet and packet['to'] == my_node_id:
            if 'decoded' in packet and 'text' in packet['decoded']:
                message_text = packet['decoded']['text'].lower()
                node_id = packet['from']

                if message_text in ["subscribe", "s"]:
                    print(f"Node {node_id} sent 'subscribe'. Subscribing with current location.")
                    latitude, longitude = get_node_info(node_id)
                    if latitude and longitude:
                        try:
                            register_node(node_id, str(latitude), str(longitude))
                            send_message(node_id, "You are now subscribed to weather alerts.")
                            send_message(node_id, "If your location updates on your node at a later time, it will dynamically update.")
                        except Exception as e:
                            print(f"Error during subscription: {e}")
                            send_message(node_id, "There was an error during the subscription process. Please check your coordinates and try again.")
                    else:
                        send_message(node_id, "Unable to retrieve location. Please provide valid coordinates.")

                elif message_text.startswith(("subscribe ", "s ")):
                    parts = message_text.split(maxsplit=1)
                    if len(parts) == 2:
                        location_input = parts[1].strip()

                        # Check if the input is likely a city, state (contains a comma)
                        if ',' in location_input:
                            print(f"Looking up city/state: {location_input}")
                            latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location_input)
                            if latitude and longitude:
                                try:
                                    # Check if weather data is valid
                                    if get_weather_alerts(latitude, longitude) is None:
                                        send_message(node_id, f"Error: Unable to fetch weather data for {resolved_city_state}. Please check the coordinates.")
                                    else:
                                        register_node(node_id, str(latitude), str(longitude))
                                        send_message(node_id, f"You are now subscribed to weather alerts for {resolved_city_state}.")
                                        send_message(node_id, "If you enable GPS or location on your node at a later time, it will dynamically update.")
                                except Exception as e:
                                    send_message(node_id, f"An error occurred: {e}")
                                    print(f"Error during subscription: {e}")
                            else:
                                send_message(node_id, f"Could not resolve location '{location_input}'. Please use 'subscribe <City, State>' or 'subscribe <latitude> <longitude>'")
                        else:
                            # Check if it's a valid latitude/longitude format
                            try:
                                lat, lon = map(float, location_input.split())
                                if is_valid_coordinates(lat, lon):
                                    print(f"Using coordinates: {lat}, {lon}")
                                    if get_weather_alerts(lat, lon) is None:
                                        send_message(node_id, f"Error: Unable to fetch weather data for coordinates ({lat}, {lon}). Please check the coordinates.")
                                    else:
                                        register_node(node_id, str(lat), str(lon))
                                        send_message(node_id, f"You are now subscribed to weather alerts for coordinates ({lat}, {lon}).")
                                        send_message(node_id, "If you enable GPS or location on your node at a later time, it will dynamically update.")
                                else:
                                    send_message(node_id, "Invalid latitude/longitude format. Please try again.")
                            except ValueError:
                                send_message(node_id, "Invalid city/state or coordinates format. Please ensure you use 'City, State' or 'latitude longitude'.")

                elif message_text in ["current", "c"]:
                    latitude, longitude = get_node_info(node_id)
                    print(f"Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                    if latitude and longitude:
                        current_weather = get_current_weather(f"{latitude} {longitude}")
                        if current_weather:
                            send_message(node_id, f"{current_weather}")
                        else:
                            send_message(node_id, "Unable to fetch current weather. Please try again later.")
                    else:
                        send_message(node_id, "Unable to retrieve your location. Please provide coordinates or a city/state.")

                elif message_text.startswith(("current ", "c ")):
                    location_input = message_text.split(maxsplit=1)[1].strip()

                    if ',' in location_input:  # City, State format
                        latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location_input)
                        if latitude and longitude:
                            current_weather = get_current_weather(f"{latitude} {longitude}")
                            if current_weather:
                                send_message(node_id, f"{current_weather}")
                            else:
                                send_message(node_id, "Unable to fetch current weather for that location.")
                        else:
                            send_message(node_id, f"Unable to resolve location '{location_input}'. Please check the format and try again.")
                    else:  # Latitude, Longitude format
                        try:
                            lat, lon = map(float, location_input.split())
                            if is_valid_coordinates(lat, lon):
                                current_weather = get_current_weather(f"{lat} {lon}")
                                if current_weather:
                                    send_message(node_id, f"{current_weather}")
                                else:
                                    send_message(node_id, "Unable to fetch current weather for that location.")
                            else:
                                send_message(node_id, "Invalid latitude/longitude format. Please check and try again.")
                        except ValueError:
                            send_message(node_id, "Invalid latitude/longitude format. Please ensure you use 'latitude longitude'.")

                elif message_text in ["tomorrow", "t"]:
                    latitude, longitude = get_node_info(node_id)
                    print(f"Latitude: {latitude}, Longitude: {longitude}")  # Debugging line
                    if latitude and longitude:
                        tomorrow_weather = get_tomorrow_weather(f"{latitude} {longitude}")
                        if tomorrow_weather:
                            send_message(node_id, f"{tomorrow_weather}")
                        else:
                            send_message(node_id, "Unable to fetch tomorrow's weather. Please try again later.")
                    else:
                        send_message(node_id, "Unable to retrieve your location. Please provide coordinates or a city/state.")

                elif message_text.startswith(("tomorrow ", "t ")):
                    location_input = message_text.split(maxsplit=1)[1].strip()

                    if ',' in location_input:  # City, State format
                        latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location_input)
                        if latitude and longitude:
                            tomorrow_weather = get_tomorrow_weather(f"{latitude} {longitude}")
                            if tomorrow_weather:
                                send_message(node_id, f"{tomorrow_weather}")
                            else:
                                send_message(node_id, "Unable to fetch tomorrow's weather for that location.")
                        else:
                            send_message(node_id, f"Unable to resolve location '{location_input}'. Please check the format and try again.")
                    else:  # Latitude, Longitude format
                        try:
                            lat, lon = map(float, location_input.split())
                            if is_valid_coordinates(lat, lon):
                                tomorrow_weather = get_tomorrow_weather(f"{lat} {lon}")
                                if tomorrow_weather:
                                    send_message(node_id, f"{tomorrow_weather}")
                                else:
                                    send_message(node_id, "Unable to fetch tomorrow's weather for that location.")
                            else:
                                send_message(node_id, "Invalid latitude/longitude format. Please check and try again.")
                        except ValueError:
                            send_message(node_id, "Invalid latitude/longitude format. Please ensure you use 'latitude longitude'.")

                elif message_text in ["5day", "5"]:
                    print(f"Node {node_id} requested '5-day forecast'.")
                    latitude, longitude = get_node_info(node_id)
                    if latitude and longitude:
                        try:
                            five_day_forecast = get_five_day_forecast(f"{latitude} {longitude}")
                            if five_day_forecast:
                                for day_forecast in five_day_forecast:
                                    send_message(node_id, day_forecast)
                            else:
                                send_message(node_id, "Unable to fetch 5-day forecast. Please try again later.")
                        except Exception as e:
                            print(f"Error during 5-day forecast fetch: {e}")
                            send_message(node_id, "An error occurred while fetching the 5-day forecast. Please check and try again.")
                    else:
                        send_message(node_id, "Unable to retrieve your location. Please provide valid coordinates.")

                elif message_text.startswith(("5day ", "5 ")):
                    parts = message_text.split(maxsplit=1)
                    if len(parts) == 2:
                        location_input = parts[1].strip()

                        # Check if the input is likely a city, state (contains a comma)
                        if ',' in location_input:
                            print(f"Looking up city/state: {location_input}")
                            latitude, longitude, resolved_city_state = get_coordinates_from_city_state(location_input)
                            if latitude and longitude:
                                try:
                                    # Get the 5-day forecast
                                    five_day_forecast = get_five_day_forecast(f"{latitude} {longitude}")
                                    if five_day_forecast:
                                        for day_forecast in five_day_forecast:
                                            send_message(node_id, day_forecast)
                                        send_message(node_id, f"You've received the 5-day forecast for {resolved_city_state}.")
                                    else:
                                        send_message(node_id, f"Error: Unable to fetch 5-day forecast for {resolved_city_state}. Please check the coordinates.")
                                except Exception as e:
                                    send_message(node_id, f"An error occurred: {e}")
                                    print(f"Error during 5-day forecast fetch: {e}")
                            else:
                                send_message(node_id, f"Could not resolve location '{location_input}'. Please use '5day <City, State>' or '5day <latitude> <longitude>'")
                        else:
                            # Check if it's a valid latitude/longitude format
                            try:
                                lat, lon = map(float, location_input.split())
                                if is_valid_coordinates(lat, lon):
                                    print(f"Using coordinates: {lat}, {lon}")
                                    five_day_forecast = get_five_day_forecast(f"{lat} {lon}")
                                    if five_day_forecast:
                                        for day_forecast in five_day_forecast:
                                            send_message(node_id, day_forecast)
                                        send_message(node_id, f"You've received the 5-day forecast for coordinates ({lat}, {lon}).")
                                    else:
                                        send_message(node_id, f"Error: Unable to fetch 5-day forecast for coordinates ({lat}, {lon}). Please check the coordinates.")
                                else:
                                    send_message(node_id, "Invalid latitude/longitude format. Please try again.")
                            except ValueError:
                                send_message(node_id, "Invalid city/state or coordinates format. Please ensure you use 'City, State' or 'latitude longitude'.")

                elif message_text in ["unsubscribe", "u"]:
                    print(f"Processing unsubscribe request for node {node_id}.")
                    remove_node(node_id)
                    send_message(node_id, "You have been unsubscribed from weather alerts.")

                else:
                    print(f"Unrecognized message from node {node_id}: {message_text}")
                    send_message(node_id, "DESCRIPTION\n\n☀️Weather Bot USA☀️\n\nI allow you to receive weather and alerts for your location.")
                    send_message(node_id, "OPTIONS\n\nBased on nodes GPS\n\n(C) Current Weather\n(T) Tomorrows Weather\n(5) 5-day forecast\n(S) Subscribe to Weather Alerts\n(U) Unsubscribe")
                    send_message(node_id, "EXPANDED\n\nFor Static Location / No GPS\n\n(C,T,S,5) <latitude> <longitude>\n(C,T,S,5) <City, State>\n\nYou can also send 'current', 'subscribe', '5-day'")

    except Exception as e:
        print(f"Error handling message: {e}")

# Callbacks
def onReceive(packet, interface):
    handle_message(packet)

def onConnection(interface, topic=pub.AUTO_TOPIC):
    print("Connection established.")

# Initialize TCP interface and subscribe to events
interface = meshtastic.tcp_interface.TCPInterface(hostname=hostname)
pub.subscribe(onReceive, "meshtastic.receive")
pub.subscribe(onConnection, "meshtastic.connection.established")

# Start the periodic weather alert check
check_weather_alerts()

# Run loop
try:
    while True:
        time.sleep(1)
finally:
    interface.close()
