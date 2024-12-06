CREATE DATABASE meshtastic_weather;

USE meshtastic_weather;

-- Table to store node subscriptions with the first_subscription column
CREATE TABLE severe_weather_subscriptions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    node_id VARCHAR(255) NOT NULL,
    latitude DOUBLE NOT NULL,
    longitude DOUBLE NOT NULL,
    first_subscription BOOLEAN DEFAULT TRUE  -- Add the first_subscription column
);

-- Table to track alerts for each node
CREATE TABLE node_alerts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    node_id BIGINT NOT NULL,
    alert_id VARCHAR(255) NOT NULL,
    headline VARCHAR(255), -- Add the headline column
    details TEXT,          -- Add the details column for detailed alert information
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(node_id, alert_id)
);

-- Index for faster lookups on node_alerts
CREATE INDEX idx_node_alerts_node_id ON node_alerts (node_id);

-- Create a new user and grant privileges
CREATE USER 'meshtastic_weather'@'localhost' IDENTIFIED BY 'PASSWORD_CHANGEME';

-- Grant all privileges on the entire database
GRANT ALL PRIVILEGES ON meshtastic_weather.* TO 'meshtastic_weather'@'localhost';

-- Apply changes
FLUSH PRIVILEGES;

QUIT;
