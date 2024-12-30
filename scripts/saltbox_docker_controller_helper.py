import sys
import os

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import requests
import signal
import time
from typing import Optional
from enum import Enum

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

def wait_for_controller(retries: int = 12, delay: int = 10) -> bool:
    """
    Waits for the controller to be ready by polling the ping endpoint.
    
    Args:
        retries: Number of times to retry
        delay: Delay between retries in seconds
    
    Returns:
        bool: True if controller is ready, False otherwise
    """
    print("Waiting for controller to be ready...")
    for attempt in range(retries):
        try:
            response = requests.get("http://127.0.0.1:3377/ping")
            if response.status_code == 200:
                print("Controller is ready")
                return True
        except requests.exceptions.RequestException:
            pass
        
        if attempt < retries - 1:
            print(f"Controller not ready, waiting {delay} seconds... (attempt {attempt + 1}/{retries})")
            time.sleep(delay)
    
    print("Controller failed to become ready")
    return False

def poll_job_status(job_id: str, timeout: int = 600) -> bool:
    """
    Polls the job status endpoint until completion or timeout.
    
    Args:
        job_id: The ID of the job to poll
        timeout: Maximum time to wait in seconds
    
    Returns:
        bool: True if job completed successfully, False otherwise
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"http://127.0.0.1:3377/job_status/{job_id}")
            if response.status_code == 200:
                status = JobStatus(response.json()["status"])
                if status == JobStatus.COMPLETED:
                    print(f"Job {job_id} completed successfully")
                    return True
                elif status == JobStatus.FAILED:
                    print(f"Job {job_id} failed")
                    return False
                print(f"Job {job_id} status: {status}")
            else:
                print(f"Error checking job status: {response.status_code}")
                return False
        except Exception as e:
            print(f"Error polling job status: {e}")
            return False
        time.sleep(5)  # Poll every 5 seconds
    
    print(f"Job {job_id} timed out after {timeout} seconds")
    return False

def start_containers() -> Optional[str]:
    """
    Sends a POST request to start containers.
    
    Returns:
        Optional[str]: Job ID if successful, None otherwise
    """
    print("Starting Containers.")
    try:
        response = requests.post("http://127.0.0.1:3377/start")
        if response.status_code == 200:
            return response.json().get("job_id")
        print(f"Error starting containers: {response.status_code}")
    except Exception as e:
        print(f"Error starting containers: {e}")
    return None

def stop_containers(signum, frame) -> None:
    """Handles termination signal by stopping containers and waiting for completion."""
    print("Stopping Containers.")
    try:
        response = requests.post("http://127.0.0.1:3377/stop")
        if response.status_code == 200:
            job_id = response.json().get("job_id")
            if job_id:
                success = poll_job_status(job_id)
                if not success:
                    print("Warning: Containers may not have stopped cleanly")
        else:
            print(f"Error stopping containers: {response.status_code}")
    except Exception as e:
        print(f"Error stopping containers: {e}")
    sys.exit()

def main():
    """Main function to handle the script's execution."""
    # Wait for controller to be ready before proceeding
    if not wait_for_controller():
        print("Controller not available, exiting")
        sys.exit(1)
    
    job_id = start_containers()
    if job_id:
        success = poll_job_status(job_id)
        if not success:
            print("Warning: Container startup may not have completed successfully")
    
    # Trap SIGTERM signal and assign it to stop_containers function
    signal.signal(signal.SIGTERM, stop_containers)
    
    # Keep the script running in an infinite loop
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
