-----------------------------------------------
WEATHERBOT USA
FOR: Meshtastic (https://meshtastic.org/)
BY: William Ruckman (KE8SRG)
CONTACT: William@NWOMesh.org
-----------------------------------------------

This is a Weather bot for Meshtastic that takes advantage of the weather.gov api for the United States and the geopy package from Nominatum for some static location functions.
Nodes can also get dynamic weather and alerts based on their nodes current location advertised to the mesh.

Feel free to create a version for your area if you do not live in the United States. You will need to use an alternate API and modify the script to work with that API.

There may be bugs. If you find any, please let me know.

INSTALL MYSQL/MARIADB/PYTHON:

apt install mysql-common python3 python3-pip

CREATE DATABASE:

mysql -u root -p < CHANGME_Prepare_Database.sql

PYTHON PACKAGES:

pip3 install bleak certifi chardet charset-normalizer dbus-fast dotmap geographiclib geopy httplib2 idna meshtastic mysql-connector-python packaging pexpect print-color protobuf ptyprocess pycurl pyparsing Pypubsub PyQRCode pyserial PySimpleSOAP pytap2 python-apt python-debian python-debianbts PyYAML reportbug requests setuptools six tabulate typing_extensions ufw urllib3 webencodings wheel

UPDATE VARIABLES IN SCRIPT:

Make sure to read and update the important variables in the script like the database information, e-mail address for the weather.gov api, and node IP address.

# User Agent for api.weather.gov configuration
headers = {
    'User-Agent': 'Weather Bot USA for Meshtastic, william@nwomesh.org'
}

# User Agent for Nominatim configuration
user_agent = "Weather Bot USA for Meshtastic, william@nwomesh.org"

# Define the hostname variable. The IP of the Meshtastic node to connect to.
hostname = '127.0.0.1'

# MySQL Database configuration
DB_CONFIG = {
    'user': 'meshtastic_weather',
    'password': 'PASSWORD_CHANGEME',
    'host': '127.0.0.1',
    'database': 'meshtastic_weather'
}

MANUALLY RUN SCRIPT (Recommend tmux):

python3 WeatherBotUSA-Meshtastic.py

RUN AS A SERVICE:

Run your Python script as a daemon on a Debian Linux server:

sudo nano /etc/systemd/system/meshtastic_weather.service

Add the following content:

[Unit]
Description=Meshtastic Weather Daemon
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/your/script.py
WorkingDirectory=/path/to/your/script
StandardOutput=journal
StandardError=journal
Restart=always
User=your_user
Group=your_group

[Install]
WantedBy=multi-user.target

Replace /path/to/your/script.py with the actual path to your Python script.

Reload systemd and enable the service:

sudo systemctl daemon-reload
sudo systemctl enable meshtastic_weather.service

Start the service:

sudo systemctl start meshtastic_weather.service

Check the status:

sudo systemctl status meshtastic_weather.service

This will allow your script to run in the background and automatically restart if it crashes.

Enjoy,

William
