#!/bin/bash

# Activate the virtual environment
source /store/droptracker/disc/venv/bin/activate

# Function to check if port is in use
check_port() {
    ss -tuln | grep :8080 > /dev/null 2>&1
}

# Function to kill process using the port
kill_port_process() {
    pid=$(lsof -t -i:8080)
    if [ ! -z "$pid" ]; then
        echo "Killing process using port 8080"
        kill -9 $pid
    fi
}

while true; do
    # Check if port is in use
    if check_port; then
        echo "Port 8080 is already in use. Attempting to kill the process..."
        kill_port_process
        sleep 5
    fi

    # Run the application
    python3 /store/droptracker/disc/main.py
    exit_code=$?

    # Check exit code
    if [ $exit_code -eq 98 ]; then
        echo "Address already in use error. Attempting to kill the process..."
        kill_port_process
        sleep 10
    elif [ $exit_code -ne 0 ]; then
        echo "DropTracker system has crashed with exit code $exit_code. Restarting in 5 seconds..."
        sleep 5
    else
        echo "DropTracker system has exited normally. Restarting in 5 seconds..."
        sleep 5
    fi
done
