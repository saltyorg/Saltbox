import requests
import signal
import time
import sys

def start_containers():
    """Sends a POST request to start containers."""
    print("Starting Containers.")
    try:
        requests.post("http://127.0.0.1:3377/start")
    except Exception as e:
        print(f"Error starting containers: {e}")

def stop_containers(signum, frame):
    """Handles termination signal by stopping containers."""
    print("Stopping Containers.")
    try:
        requests.post("http://127.0.0.1:3377/stop")
    except Exception as e:
        print(f"Error stopping containers: {e}")
    sys.exit()

def main():
    """Main function to handle the script's execution."""
    start_containers()
    
    # Trap SIGTERM signal and assign it to stop_containers function
    signal.signal(signal.SIGTERM, stop_containers)
    
    # Keep the script running in an infinite loop
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
