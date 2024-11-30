CREATE DATABASE meshtastic_weather;

USE meshtastic_weather;

CREATE TABLE severe_weather_subscriptions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    node_id VARCHAR(255) NOT NULL,
    latitude DOUBLE NOT NULL,
    longitude DOUBLE NOT NULL
);

CREATE USER 'meshtastic_weather'@'localhost' IDENTIFIED BY 'PASSWORD_CHANGEME';

GRANT ALL PRIVILEGES ON meshtastic_weather.severe_weather_subscriptions TO 'meshtastic_weather'@'localhost';

FLUSH PRIVILEGES;

QUIT;

