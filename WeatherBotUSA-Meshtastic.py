import sys
import time
import threading
import meshtastic
import meshtastic.tcp_interface
import meshtastic.util
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

# Maximum packet size for messages sent to nodes
# If some packets fail to send, lower this by 10 until they do.
MAX_PACKET_SIZE = 210

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

def custom_exit(message):
    print(f"Custom exit override: {message}")
    return  # Prevent termination

meshtastic.util.our_exit = custom_exit

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
    """
    Registers a node in the database with the given location.
    """
    latitude = clean_coordinates(latitude)
    longitude = clean_coordinates(longitude)

    # Convert node_id to hexadecimal format for Meshtastic
    hex_node_id = convert_to_hex(node_id)
    print(f"Converted node_id {node_id} to Meshtastic-compatible hex_node_id {hex_node_id}")

    # Validate the node in Meshtastic's node database
    if not is_node_in_meshtastic(hex_node_id, node_id):
        print(f"Warning: NodeId {hex_node_id} not found in Meshtastic nodes. Proceeding with registration.")

    db = connect_to_database()
    if db:
        cursor = db.cursor()
        # Check if the node already exists in the database
        check_query = "SELECT COUNT(*) FROM severe_weather_subscriptions WHERE node_id = %s"
        cursor.execute(check_query, (node_id,))
        (count,) = cursor.fetchone()

        if count > 0:
            print(f"Node {node_id} is already subscribed.")
            send_message(node_id, "You are already subscribed to weather alerts.")
        else:
            # Insert the node if it does not exist
            query = "INSERT INTO severe_weather_subscriptions (node_id, latitude, longitude) VALUES (%s, %s, %s)"
            cursor.execute(query, (node_id, latitude, longitude))
            db.commit()
            print(f"Node {node_id} successfully subscribed.")
            send_message(node_id, "You are now subscribed to weather alerts.")
            send_message(node_id, "If your location updates on your node at a later time, it will dynamically update.")

        cursor.close()
        db.close()

    # Perform an immediate weather alert check upon registration (only if newly added)
    if count == 0:
        send_weather_alerts(node_id, latitude, longitude, is_initial_check=True)

# Remove node from the subscription database
def remove_node(node_id):
    """
    Remove a node from the subscription database and purge associated alerts.
    """
    db = connect_to_database()
    if db:
        cursor = None
        try:
            cursor = db.cursor()

            # Remove active alerts for the node
            print(f"Debug: Removing active alerts for node {node_id}.")
            delete_alerts_query = "DELETE FROM node_alerts WHERE node_id = %s"
            cursor.execute(delete_alerts_query, (node_id,))

            # Remove the node from subscriptions
            print(f"Debug: Removing subscription for node {node_id}.")
            delete_subscription_query = "DELETE FROM severe_weather_subscriptions WHERE node_id = %s"
            cursor.execute(delete_subscription_query, (node_id,))

            db.commit()
            print(f"Debug: Successfully removed node {node_id} and its active alerts.")
        except mysql.connector.Error as err:
            print(f"Error removing node {node_id}: {err}")
        finally:
            if cursor:
                cursor.close()
            if db.is_connected():
                db.close()
    else:
        print(f"Database connection failed while attempting to remove node {node_id}.")

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
    """
    Sends a message to the specified node.
    """
    # Convert node_id to hexadecimal format
    hex_node_id = convert_to_hex(node_id)
    print(f"Converted node_id {node_id} to Meshtastic-compatible hex_node_id {hex_node_id}")

    # Validate the node in Meshtastic's node database
    if not is_node_in_meshtastic(hex_node_id, node_id):
        print(f"Warning: NodeId {hex_node_id} not found in Meshtastic nodes. Skipping message.")
        return

    # Define the maximum size for each data packet
    max_packet_size = MAX_PACKET_SIZE  # Use the global variable

    # Split the message into chunks if it exceeds the maximum size
    message_parts = [message[i:i + max_packet_size] for i in range(0, len(message), max_packet_size)]

    # Send each part individually
    for part in message_parts:
        try:
            interface.sendText(part, hex_node_id)
            print(f"Sent message to node {hex_node_id}: {part}")
            time.sleep(0.1)  # Add a 100ms delay between packets
        except Exception as e:
            print(f"Error sending message to node {hex_node_id}: {e}")

def validate_node_id(node_id):
    # Debug available nodes
    print("Validating node_id against Meshtastic nodes:")
    for node in interface.nodes.values():
        print(f"Node ID: {node['num']}, User: {node.get('user', 'Unknown')}")

    # Check if node_id is in the list
    if str(node_id) in [str(node['num']) for node in interface.nodes.values()]:
        return True
    print(f"Warning: Node {node_id} not found in Meshtastic nodes.")
    return False

# Send weather alerts
def send_weather_alerts(node_id, latitude, longitude, is_initial_check=False):
    """
    Sends weather alerts to the specified node based on its location.
    """
    print(f"Fetching weather alerts for node {node_id} at ({latitude}, {longitude})...")
    alerts = get_weather_alerts(latitude, longitude)

    # Convert node_id to hexadecimal format
    hex_node_id = convert_to_hex(node_id)

    # Validate the node in Meshtastic
    if not is_node_in_meshtastic(hex_node_id, node_id):
        print(f"Warning: NodeId {hex_node_id} not found in Meshtastic nodes. Skipping alert delivery.")
        return

    db = connect_to_database()
    if not db:
        print("Database connection failed. Cannot process weather alerts.")
        return

    try:
        cursor = db.cursor(dictionary=True)
        # Fetch existing alerts for the node
        cursor.execute("SELECT alert_id, headline FROM node_alerts WHERE node_id = %s", (node_id,))
        existing_alerts = {row['alert_id']: row['headline'] for row in cursor.fetchall()}
        print(f"Existing alerts for node {node_id}: {existing_alerts}")

        current_alerts = {}
        new_alerts_sent = False

        for alert in alerts:
            alert_id = alert['id']
            headline = alert['properties'].get('headline', 'No headline available')
            details = alert['properties'].get('description', 'No additional details available.')
            print(f"Processing alert for node {node_id}: {headline}")
            current_alerts[alert_id] = headline

            if alert_id not in existing_alerts:
                # New alert detected
                message = f"🚨 Weather alert:\n{headline}\nSend 'M' for more details."
                send_message(node_id, message)
                new_alerts_sent = True
                # Insert the alert into the database
                cursor.execute(
                    "INSERT INTO node_alerts (node_id, alert_id, headline, details) VALUES (%s, %s, %s, %s)",
                    (node_id, alert_id, headline, details),
                )
                db.commit()

        # Identify alerts that are no longer active
        canceled_alerts = set(existing_alerts.keys()) - set(current_alerts.keys())
        for alert_id in canceled_alerts:
            canceled_headline = existing_alerts[alert_id]
            print(f"Removed alert {alert_id} for node {node_id}")
            send_message(node_id, f"⚠️ Weather alert canceled: {canceled_headline}")
            # Remove the alert from the database
            cursor.execute(
                "DELETE FROM node_alerts WHERE node_id = %s AND alert_id = %s",
                (node_id, alert_id),
            )
            db.commit()

        # Check if there are new active alerts after cancellations
        if canceled_alerts and current_alerts:
            active_alerts_summary = "\n".join(
                [f"🚨 Active alert: {headline}" for headline in current_alerts.values()]
            )
            send_message(node_id, f"Following the cancellation, these alerts remain active:\n{active_alerts_summary}")
        elif canceled_alerts and not current_alerts:
            send_message(node_id, "✅ All clear! No active weather alerts for your location.")

    except mysql.connector.Error as err:
        print(f"Error processing weather alerts for node {node_id}: {err}")
    finally:
        cursor.close()
        db.close()

def is_node_in_meshtastic(hex_node_id, decimal_node_id=None):
    """
    Check if the given node_id exists in Meshtastic's known nodes.
    Tries both hexadecimal and decimal representations for robustness.
    """
    # Refresh the Meshtastic node list to ensure it's up-to-date
    interface.showNodes()

    # Debugging: Print all nodes for verification
    #print("Debugging Meshtastic nodes:")
    #for node_key, node_info in interface.nodes.items():
    #    print(f"Node Key: {node_key}, Node Info: {node_info}")

    # Check for the node in both hexadecimal and decimal formats
    for node_key, node in interface.nodes.items():
        if node.get("user", {}).get("id") == hex_node_id or node_key == decimal_node_id:
            #print(f"Found node in Meshtastic: {node}")
            return True

    print(f"Node {hex_node_id} or {decimal_node_id} not found in Meshtastic nodes.")
    return False

# Periodic weather alert check
def check_weather_alerts():
    db = connect_to_database()
    if db:
        #print("Database connection established.")  # Debugging
        cursor = db.cursor(dictionary=True)
        query = "SELECT node_id, latitude, longitude, first_subscription FROM severe_weather_subscriptions"
        cursor.execute(query)
        nodes = cursor.fetchall()

        for node in nodes:
            node_id = str(node['node_id']).strip()  # Ensure node_id is treated as a string and stripped of extra spaces
            db_latitude = node['latitude']
            db_longitude = node['longitude']
            first_subscription = node['first_subscription']  # Get the first_subscription flag

            print(f"Checking for node {node_id} (first_subscription={first_subscription})")

            # Fetch updated location for the node
            updated_latitude, updated_longitude = get_node_info(node_id)

            # Validate updated coordinates and compare with database
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
                db.commit()  # Commit the update to ensure it is saved in the database
                print(f"Committed update to database for node {node_id}.")
                latitude, longitude = updated_latitude, updated_longitude
            else:
                # Use existing coordinates from the database
                print(f"Using existing location for node {node_id} ({db_latitude}, {db_longitude})...")
                latitude, longitude = db_latitude, db_longitude

            # Check if the node is already subscribed (exists in the database)
            print(f"Executing query to check for node_id: {node_id}")
            cursor.execute("SELECT node_id FROM severe_weather_subscriptions WHERE node_id = %s", (node_id,))
            node_check = cursor.fetchone()
            print(f"Query result for node_id {node_id}: {node_check}")  # Debugging

            if not node_check:
                print(f"Warning: NodeId {node_id} not found in DB")  # Debugging
                # First time subscription, send the "no alerts" message
                send_message(node_id, "There are no active weather alerts for your current location.")

                # Commit and update the first_subscription flag to False
                cursor.execute("UPDATE severe_weather_subscriptions SET first_subscription = FALSE WHERE node_id = %s", (node_id,))
                db.commit()  # Commit the update to ensure changes are visible
                print(f"Committed update to first_subscription for node {node_id}.")
                continue  # Skip processing alerts for this node as it's not properly subscribed

            # Process alerts for the node
            try:
                print(f"Processing alerts for node {node_id} at ({latitude}, {longitude})...")
                send_weather_alerts(node_id, latitude, longitude)
            except Exception as e:
                print(f"Error processing weather alerts for node {node_id}: {e}")

        cursor.close()
        db.close()
    else:
        print("Database connection failed. Cannot check weather alerts.")

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
        # Convert node_id to integer and then to hex format
        int_node_id = int(node_id)
        hex_node_id = f"!{hex(int_node_id)[2:]}"  # Convert node ID to hex format with '!' prefix
        nodes_data = interface.showNodes()  # Get nodes data
        nodes = parse_nodes_data(nodes_data)  # Parse the data into a list of dictionaries

        for n in nodes:
            if n.get('ID', '').strip() == hex_node_id:
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

def convert_to_hex(node_id):
    """
    Convert a decimal node_id to Meshtastic-compatible hexadecimal format.
    """
    return f"!{hex(int(node_id))[2:]}"

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

def get_active_alerts_for_node(node_id):
    """
    Retrieve active alerts for a specific node from the database.
    """
    db = connect_to_database()
    if db:
        cursor = None  # Initialize cursor
        try:
            cursor = db.cursor(dictionary=True)
            query = "SELECT alert_id, headline FROM node_alerts WHERE node_id = %s"
            cursor.execute(query, (node_id,))
            
            # Fetch all results to ensure the cursor is emptied
            results = cursor.fetchall()

            if results:
                print(f"Debug: Active alerts for node {node_id}: {results}")
                return {row["alert_id"]: row["headline"] for row in results}
            else:
                print(f"Debug: No active alerts found for node {node_id}. Query result: {results}")
                return {}
        except mysql.connector.Error as err:
            print(f"Error in get_active_alerts_for_node: {err}")
            return {}
        finally:
            # Ensure cursor and connection are closed even if an exception occurs
            if cursor:
                cursor.close()
            if db.is_connected():
                db.close()
    else:
        print("Database connection failed while fetching active alerts.")
        return {}

def get_all_alert_details():
    """
    Fetch all active alerts and their details for all nodes.
    """
    db = connect_to_database()
    if db:
        cursor = None
        try:
            cursor = db.cursor(dictionary=True)
            query = "SELECT alert_id, details FROM node_alerts"
            cursor.execute(query)
            results = cursor.fetchall()  # Fetch all alerts once
            
            # Close cursor and connection immediately
            cursor.close()
            db.close()

            if results:
                print(f"Debug: All active alerts fetched: {results}")
                return {row["alert_id"]: row["details"] for row in results}
            else:
                print("Debug: No active alerts found.")
                return {}
        except mysql.connector.Error as err:
            print(f"Error in get_all_alert_details: {err}")
            return {}
        finally:
            if cursor:
                cursor.close()
            if db.is_connected():
                db.close()
    else:
        print("Database connection failed while fetching all alert details.")
        return {}

def process_node_alerts(node_id, all_alert_details):
    """
    Process alerts for a single node based on pre-fetched alert details.
    Sends a message if no active alerts are available.
    """
    db = connect_to_database()
    if db:
        cursor = None
        try:
            cursor = db.cursor(dictionary=True)
            query = "SELECT alert_id, headline FROM node_alerts WHERE node_id = %s"
            cursor.execute(query, (node_id,))
            node_alerts = cursor.fetchall()

            print(f"Debug: Active alerts for node {node_id}: {node_alerts}")

            if not node_alerts:
                # No active alerts for the node
                send_message(node_id, "There are no active weather alerts for your location at this time.")
                print(f"Debug: No active alerts for node {node_id}. Message sent.")
                return

            # Process and send details for active alerts
            for alert in node_alerts:
                alert_id = alert["alert_id"]
                headline = alert["headline"]
                details = all_alert_details.get(alert_id, "Details unavailable")

                print(f"Debug: Processing alert {alert_id} for node {node_id}")
                send_message(node_id, f"🚨 {headline}\n{details}")
        except mysql.connector.Error as err:
            print(f"Error processing alerts for node {node_id}: {err}")
        finally:
            if cursor:
                cursor.close()
            if db.is_connected():
                db.close()
    else:
        print(f"Database connection failed while processing alerts for node {node_id}.")
        send_message(node_id, "There are no active weather alerts for your location at this time.")

def process_all_nodes():
    """
    Process alerts for all nodes in the severe_weather_subscriptions table.
    """
    db = connect_to_database()
    if db:
        cursor = None
        try:
            cursor = db.cursor(dictionary=True)
            query = "SELECT node_id FROM severe_weather_subscriptions"
            cursor.execute(query)
            nodes = cursor.fetchall()

            print(f"Debug: Nodes fetched for processing: {nodes}")

            # Fetch all alert details once
            all_alert_details = get_all_alert_details()

            for node in nodes:
                process_node_alerts(node["node_id"], all_alert_details)
        except mysql.connector.Error as err:
            print(f"Error fetching nodes for processing: {err}")
        finally:
            if cursor:
                cursor.close()
            if db.is_connected():
                db.close()
    else:
        print("Database connection failed while fetching nodes.")

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

                # "More Info" or "M"
                elif message_text in ["more", "m"]:
                    print(f"Debug: 'More info' command received from node {node_id}.")
                    all_alert_details = get_all_alert_details()  # Fetch all alert details once
                    if all_alert_details:
                        process_node_alerts(node_id, all_alert_details)  # Process for the specific node
                    else:
                        send_message(node_id, "No active weather alerts to provide more information on.")

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
