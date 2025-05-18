#!/bin/bash

# Configuration
VENV_PATH="/store/droptracker/disc/venv/bin/activate"
APP_DIR="/store/droptracker/disc"
MAIN_APP="main.py"
UPDATE_APP="player_total_update.py"
MAIN_PORT=8080
UPDATE_PORT=21474
MAIN_SCREEN="DTcore"
UPDATE_SCREEN="DT-pu"
USER="droptracker"  # The user that should own the screens

# Check if running as root and re-execute as the correct user if needed
if [ "$(id -u)" = "0" ]; then
    echo "This script should not be run as root. Re-executing as user '$USER'..."
    exec su - $USER -c "cd $(pwd) && bash $(basename $0)"
    exit $?
fi

# Activate virtual environment
source $VENV_PATH

# Function to check if a screen session exists (case insensitive)
screen_exists() {
    screen -list | grep -i "$1" > /dev/null
    return $?
}

# Function to check if a port is in use
port_in_use() {
    ss -tuln | grep -q ":$1 "
    return $?
}

# Function to kill process using a specific port
kill_port_process() {
    local port=$1
    local pid=$(lsof -t -i:$port)
    if [ ! -z "$pid" ]; then
        echo "Killing process using port $port (PID: $pid)"
        kill -9 $pid 2>/dev/null || sudo kill -9 $pid
        sleep 2
    fi
}

# Function to start an application in a screen
start_app_in_screen() {
    local screen_name=$1
    local app_name=$2
    local port=$3
    
    # Check if screen already exists (case insensitive)
    if screen_exists "$screen_name"; then
        echo "Screen matching '$screen_name' is already running. Skipping..."
        return 0
    fi
    
    # Check if port is in use and kill the process if needed
    if port_in_use $port; then
        echo "Port $port is already in use. Attempting to kill the process..."
        kill_port_process $port
    fi
    
    # Create a new detached screen session and run the application
    echo "Starting $app_name in screen '$screen_name'..."
    screen -dmS "$screen_name" bash -c "cd $APP_DIR && source $VENV_PATH && python3 $app_name; exec bash"
    
    # Wait a moment to let the application start
    sleep 3
    
    # Verify the screen is running
    if screen_exists "$screen_name"; then
        echo "Successfully started $app_name in screen '$screen_name'"
    else
        echo "Failed to start screen '$screen_name'"
        return 1
    fi
    
    return 0
}

# Function to start the heartbeat application
start_heartbeat() {
    local screen_name="DT-heartbeat"
    local app_name="heartbeat.py"

    # Check if screen already exists (case insensitive)
    if screen_exists "$screen_name"; then
        echo "Screen matching '$screen_name' is already running. Skipping..."
        return 0
    fi  
    # Create a new detached screen session and run the application
    echo "Starting $app_name in screen '$screen_name'..."
    screen -dmS "$screen_name" bash -c "cd $APP_DIR && source $VENV_PATH && python3 $app_name; exec bash"
    
    # Wait a moment to let the application start
    sleep 3 
    # Verify the screen is running
    if screen_exists "$screen_name"; then
        echo "Successfully started $app_name in screen '$screen_name'"
    else
        echo "Failed to start screen '$screen_name'"
        return 1
    fi
}

# Main function
main() {
    echo "=== DropTracker System Startup ==="
    echo "Running as user: $(whoami)"
    echo "Checking for existing screens and starting applications if needed..."
    
    # Display current screens
    echo "Current screens:"
    screen -list
    
    # Start main application
    start_app_in_screen "$MAIN_SCREEN" "$MAIN_APP" $MAIN_PORT
    
    # Start player update application
    start_app_in_screen "$UPDATE_SCREEN" "$UPDATE_APP" $UPDATE_PORT
    
    echo "=== Startup Complete ==="
    
    # List all running screens
    echo "Running screens:"
    screen -list
}

# Run the main function
main
# Check if the heartbeater is active
start_heartbeat

exit 0