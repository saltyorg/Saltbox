#!/bin/bash

# Command to run at the start
echo "Starting Containers."
/usr/bin/curl -X POST http://127.0.0.1:3377/start >/dev/null 2>&1

# Function to run when the script receives a termination signal
cleanup() {
    echo "Stopping Containers."
    /usr/bin/curl -X POST http://127.0.0.1:3377/stop >/dev/null 2>&1
    exit
}

# Trap SIGTERM signal (sent by systemd stop command) and call cleanup function
trap cleanup SIGTERM

# Keep the script running in an infinite loop
while true; do
    sleep 60
done
