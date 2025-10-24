import subprocess
import sys
import tempfile
import os
import shutil
import threading
import time
import select
import re
from signal import signal, SIGTERM, SIGINT

hosts_file = '/etc/hosts'
hosts_backup = '/etc/hosts.backup'
begin_block = "# BEGIN DOCKER CONTAINERS"
end_block = "# END DOCKER CONTAINERS"

# Debounce configuration
DEBOUNCE_DELAY_SECONDS = 1  # Normal debounce delay
MAX_DEBOUNCE_SECONDS = 5    # Maximum time to wait before forcing update

# Debounce timer for event handling
debounce_timer = None
first_event_time = None  # Track when debounce window started
debounce_lock = threading.Lock()
shutdown_event = threading.Event()


class HostsFileManager:
    """Manages safe updates to /etc/hosts file with proper backup and validation."""

    def __init__(self):
        self.hosts_file = hosts_file
        self.hosts_backup = hosts_backup

    def create_backup(self):
        """Create a backup of the hosts file."""
        try:
            if os.path.exists(self.hosts_file):
                shutil.copy2(self.hosts_file, self.hosts_backup)
                print(f"Backup created: {self.hosts_backup}", flush=True)
                return True
        except Exception as e:
            print(f"Error creating backup: {e}", flush=True)
            return False

    def restore_backup(self):
        """Restore hosts file from backup."""
        try:
            if os.path.exists(self.hosts_backup):
                shutil.copy2(self.hosts_backup, self.hosts_file)
                print(f"Restored hosts file from backup", flush=True)
                return True
        except Exception as e:
            print(f"Error restoring backup: {e}", flush=True)
            return False

    def validate_hosts_file(self, filepath, allow_empty_managed_section=True):
        """
        Validate that a hosts file has proper structure.

        Args:
            filepath: Path to the hosts file to validate
            allow_empty_managed_section: If True, allow the managed section to be empty (no containers)
        """
        try:
            with open(filepath, 'r') as f:
                content = f.read()

            # Basic validation checks
            if not content.strip():
                print(f"Error: File {filepath} is completely empty", flush=True)
                return False

            # Check for essential entries (localhost should always be present)
            if '127.0.0.1' not in content and '::1' not in content:
                print(f"Error: File {filepath} missing required localhost entries", flush=True)
                return False

            # Check for our markers
            if begin_block in content and end_block not in content:
                print(f"Error: File {filepath} has BEGIN marker but no END marker", flush=True)
                return False

            if end_block in content and begin_block not in content:
                print(f"Error: File {filepath} has END marker but no BEGIN marker", flush=True)
                return False

            # Check if managed section exists and is properly formed
            if begin_block in content and end_block in content:
                # Extract the managed section
                begin_idx = content.find(begin_block)
                end_idx = content.find(end_block)

                if begin_idx > end_idx:
                    print(f"Error: File {filepath} has END marker before BEGIN marker", flush=True)
                    return False

                # Get content between markers
                managed_content = content[begin_idx + len(begin_block):end_idx].strip()

                # Empty managed section is OK if allowed (no containers on saltbox network)
                if not managed_content and allow_empty_managed_section:
                    print(f"Info: No Docker containers found on saltbox network", flush=True)

            return True

        except Exception as e:
            print(f"Error validating file {filepath}: {e}", flush=True)
            return False


hosts_manager = HostsFileManager()


def fix_non_breaking_spaces():
    """
    Fix non-breaking spaces in the hosts file using a simple sed command.
    This handles single-byte (A0) non-breaking spaces.
    """
    try:
        # Create a backup before modifying
        if not hosts_manager.create_backup():
            print("Warning: Could not create backup before fixing spaces", flush=True)

        # Direct in-place replacement with sed
        sed_cmd = f"sed -i 's/\\xA0/ /g' {hosts_file}"
        result = subprocess.run(sed_cmd, shell=True, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"Error running sed command: {result.stderr}", flush=True)
            return False

        return True
    except Exception as e:
        print(f"Error fixing non-breaking spaces: {e}", flush=True)
        return False


def parse_time_interval(interval_str):
    """Parse the time interval string into seconds."""
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    unit = interval_str[-1]
    if unit in units:
        try:
            value = int(interval_str[:-1])
            return value * units[unit]
        except ValueError:
            raise ValueError("Invalid time interval format.")
    else:
        raise ValueError("Unsupported time unit.")


def ensure_managed_section_exists():
    """Ensures the managed section exists in the hosts file."""
    try:
        with open(hosts_file, 'r') as file:
            contents = file.read()

        # Check if both markers exist
        if begin_block not in contents or end_block not in contents:
            print(f"Appending managed section markers to {hosts_file}.", flush=True)

            # Create backup before modification
            hosts_manager.create_backup()

            with open(hosts_file, 'a') as file:
                file.write(f"\n{begin_block}\n{end_block}\n")

        return True

    except Exception as e:
        print(f"Error ensuring managed section: {e}", flush=True)
        return False


def update_hosts_file():
    """Update the hosts file with Docker container information."""
    if shutdown_event.is_set():
        return False

    print(f"Updating hosts file", flush=True)

    try:
        # Create backup before any modifications
        if not hosts_manager.create_backup():
            print("Warning: Could not create backup, proceeding with caution", flush=True)

        # Ensure hosts file exists
        if not os.path.exists(hosts_file):
            print(f"Error: {hosts_file} does not exist!", flush=True)
            # Try to restore from backup
            if hosts_manager.restore_backup():
                print("Restored hosts file from backup", flush=True)
            else:
                print("CRITICAL: No hosts file and no backup available!", flush=True)
                return False

        # Create a temporary file with proper permissions
        temp_fd, hosts_file_tmp = tempfile.mkstemp(dir='/etc', prefix='hosts_', suffix='.tmp')
        os.close(temp_fd)  # Close the file descriptor

        try:
            # First, check if Docker is running
            docker_check = subprocess.run(
                "docker info > /dev/null 2>&1",
                shell=True,
                capture_output=True
            )

            if docker_check.returncode != 0:
                print("Warning: Docker daemon is not running or not accessible, skipping update", flush=True)
                os.unlink(hosts_file_tmp)
                return False

            # Build and execute the shell command
            shell_command = (
                "docker container ls -q | xargs -r docker container inspect | "
                "jq -r '.[] | select(.NetworkSettings.Networks.saltbox != null) | "
                "select(.NetworkSettings.Networks.saltbox.IPAddress != null and .NetworkSettings.Networks.saltbox.IPAddress != \"\") | "
                "select(.NetworkSettings.Networks.saltbox.Aliases != null and (.NetworkSettings.Networks.saltbox.Aliases | length) > 0) | "
                ".NetworkSettings.Networks.saltbox.IPAddress as $ip | .NetworkSettings.Networks.saltbox.Aliases | map(select(length > 0)) | "
                "unique | map(. + \" \" + . + \".saltbox\") | join(\" \") | \"\($ip) \(.)\"' | "
                f"sed -ne \"/^{begin_block}$/ {{p; r /dev/stdin\" -e \":a; n; /^{end_block}$/ {{p; b}}; ba}}; p\" {hosts_file} > {hosts_file_tmp}"
            )

            result = subprocess.run(
                shell_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30  # Add timeout to prevent hanging
            )

            if result.returncode != 0:
                # Provide more detailed error messages based on common failure scenarios
                stderr_lower = result.stderr.lower() if result.stderr else ""

                if "docker" in stderr_lower and ("not found" in stderr_lower or "command not found" in stderr_lower):
                    print("Error: Docker command not found. Is Docker installed?", flush=True)
                elif "permission denied" in stderr_lower:
                    print("Error: Permission denied accessing Docker. Check Docker socket permissions.", flush=True)
                elif "jq" in stderr_lower and ("not found" in stderr_lower or "command not found" in stderr_lower):
                    print("Error: jq command not found. Is jq installed?", flush=True)
                elif "cannot connect" in stderr_lower or "connection refused" in stderr_lower:
                    print("Error: Cannot connect to Docker daemon. Is Docker running?", flush=True)
                else:
                    print(f"Error executing update command (exit code {result.returncode})", flush=True)
                    if result.stderr:
                        print(f"Error details: {result.stderr.strip()}", flush=True)
                    if result.stdout:
                        print(f"Output: {result.stdout.strip()}", flush=True)

                os.unlink(hosts_file_tmp)
                return False

            # Check that the temp file is not completely empty
            if os.path.getsize(hosts_file_tmp) == 0:
                print("Error: Generated hosts file is completely empty, not updating", flush=True)
                os.unlink(hosts_file_tmp)
                return False

            # Validate the new file before replacing (allows empty managed section - no containers is OK)
            if not hosts_manager.validate_hosts_file(hosts_file_tmp, allow_empty_managed_section=True):
                print("Validation failed for new hosts file, not updating", flush=True)
                os.unlink(hosts_file_tmp)
                return False

            # Count container entries in the managed section for informative logging
            with open(hosts_file_tmp, 'r') as f:
                temp_content = f.read()

            container_count = 0
            if begin_block in temp_content and end_block in temp_content:
                begin_idx = temp_content.find(begin_block) + len(begin_block)
                end_idx = temp_content.find(end_block)
                managed_section = temp_content[begin_idx:end_idx].strip()

                # Count non-empty lines in managed section
                if managed_section:
                    container_count = len([line for line in managed_section.split('\n') if line.strip()])

            # Set proper permissions
            os.chmod(hosts_file_tmp, 0o644)

            # Atomic move
            os.replace(hosts_file_tmp, hosts_file)

            if container_count > 0:
                print(f"Hosts file updated successfully ({container_count} container entries)", flush=True)
            else:
                print("Hosts file updated successfully (no containers on saltbox network)", flush=True)

            return True

        except subprocess.TimeoutExpired:
            print("Error: Docker command timed out after 30 seconds", flush=True)
            # Clean up temp file if it exists
            if os.path.exists(hosts_file_tmp):
                try:
                    os.unlink(hosts_file_tmp)
                except:
                    pass
            return False

        except FileNotFoundError as e:
            print(f"Error: Required file or command not found: {e}", flush=True)
            # Clean up temp file if it exists
            if os.path.exists(hosts_file_tmp):
                try:
                    os.unlink(hosts_file_tmp)
                except:
                    pass
            return False

        except PermissionError as e:
            print(f"Error: Permission denied: {e}", flush=True)
            # Clean up temp file if it exists
            if os.path.exists(hosts_file_tmp):
                try:
                    os.unlink(hosts_file_tmp)
                except:
                    pass
            return False

        except Exception as e:
            print(f"Error during hosts file update: {type(e).__name__}: {e}", flush=True)
            # Clean up temp file if it exists
            if os.path.exists(hosts_file_tmp):
                try:
                    os.unlink(hosts_file_tmp)
                except:
                    pass

            # Try to restore from backup if main file is corrupted
            if not os.path.exists(hosts_file) or os.path.getsize(hosts_file) == 0:
                print("Main hosts file is missing or empty, attempting restore", flush=True)
                hosts_manager.restore_backup()

            return False

    except Exception as e:
        print(f"Error in update_hosts_file: {e}", flush=True)
        return False


def debounced_update():
    """Execute the hosts file update after debounce period."""
    global debounce_timer, first_event_time

    with debounce_lock:
        debounce_timer = None
        first_event_time = None  # Reset debounce window

    update_hosts_file()


def trigger_update():
    """
    Trigger a debounced update with maximum debounce window protection.

    Normal behavior: 1 second delay, resets on new events
    Protection: Forces update after MAX_DEBOUNCE_SECONDS even if events keep coming
    """
    global debounce_timer, first_event_time

    if shutdown_event.is_set():
        return

    current_time = time.time()

    with debounce_lock:
        # Check if we need to force update due to max debounce window
        if first_event_time is not None:
            time_since_first_event = current_time - first_event_time

            if time_since_first_event >= MAX_DEBOUNCE_SECONDS:
                # Max debounce window reached - force immediate update
                print(f"Forcing update (max debounce window of {MAX_DEBOUNCE_SECONDS}s reached)", flush=True)

                # Cancel existing timer if running
                if debounce_timer is not None:
                    debounce_timer.cancel()
                    debounce_timer = None

                # Reset tracking
                first_event_time = None

                # Execute update immediately (outside lock to avoid blocking)
                # Release lock before calling update
                pass  # Will execute after lock is released
            else:
                # Still within max window - reset debounce timer
                if debounce_timer is not None:
                    debounce_timer.cancel()

                debounce_timer = threading.Timer(DEBOUNCE_DELAY_SECONDS, debounced_update)
                debounce_timer.start()
                return
        else:
            # First event in this debounce window - record start time
            first_event_time = current_time

            # Cancel existing timer if running
            if debounce_timer is not None:
                debounce_timer.cancel()

            # Start new timer
            debounce_timer = threading.Timer(DEBOUNCE_DELAY_SECONDS, debounced_update)
            debounce_timer.start()
            return

    # If we reach here, we need to force immediate update (max window exceeded)
    update_hosts_file()


def periodic_update(interval_seconds):
    """Periodically update the hosts file on a fixed schedule."""
    # Do an initial update
    update_hosts_file()

    while not shutdown_event.is_set():
        # Sleep in small increments to be responsive to shutdown
        for _ in range(interval_seconds):
            if shutdown_event.is_set():
                break
            time.sleep(1)

        if not shutdown_event.is_set():
            update_hosts_file()

    print("Periodic update thread stopped", flush=True)


def get_container_name(container_id):
    """
    Look up container name from Docker using container ID.
    Returns container name or None if lookup fails.
    """
    try:
        result = subprocess.run(
            f"docker inspect --format '{{{{.Name}}}}' {container_id}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0 and result.stdout.strip():
            # Docker returns /containername, strip the leading slash
            name = result.stdout.strip().lstrip('/')
            return name
    except Exception:
        pass
    return None


def parse_docker_event(event_line):
    """
    Parse a Docker event line to extract relevant information.

    Docker events format examples:
    - Container: 2025-01-15T... container start abc123 (name=myapp, image=nginx)
    - Network: 2025-01-15T... network disconnect abc123 (container=def456, name=saltbox, type=bridge)

    Returns a concise string with relevant info
    """
    try:
        # Match pattern: timestamp object_type event_type object_id (attributes...)
        match = re.search(
            r'(\S+)\s+(container|network)\s+(\w+)\s+([a-f0-9]+)\s*(?:\((.*?)\))?',
            event_line
        )

        if not match:
            return None

        object_type = match.group(2)  # container or network
        event_type = match.group(3)   # start, stop, disconnect, etc.
        object_id = match.group(4)[:12]  # First 12 chars of ID
        attributes = match.group(5) if match.group(5) else ""

        # For network events, extract container info
        if object_type == "network" and attributes:
            container_match = re.search(r'container=([a-f0-9]+)', attributes)
            name_match = re.search(r'name=([^,\)]+)', attributes)
            network_name = name_match.group(1) if name_match else None

            # For network disconnect, show which container disconnected from which network
            if container_match and network_name:
                container_id = container_match.group(1)[:12]

                # Try to get container name
                container_name = get_container_name(container_id)

                if container_name:
                    return f"container '{container_name}' ({container_id}) -> {event_type} from '{network_name}'"
                else:
                    return f"container {container_id} -> {event_type} from '{network_name}'"
            elif network_name:
                return f"network '{network_name}' -> {event_type}"

        # For container events, extract container name
        elif object_type == "container" and attributes:
            name_match = re.search(r'name=([^,\)]+)', attributes)
            if name_match:
                container_name = name_match.group(1)
                return f"container '{container_name}' ({object_id}) -> {event_type}"

        # Fallback format
        return f"{object_type} {object_id} -> {event_type}"

    except Exception:
        # If parsing fails, return None to fall back to raw event
        return None


def monitor_docker_events():
    """Monitor Docker events with automatic retry and robust error handling."""
    max_retries = 5
    retry_count = 0
    base_retry_delay = 5  # seconds
    process = None

    while not shutdown_event.is_set() and retry_count < max_retries:
        try:
            # Check if Docker is available before starting
            docker_check = subprocess.run(
                "docker info > /dev/null 2>&1",
                shell=True,
                capture_output=True,
                timeout=10
            )

            if docker_check.returncode != 0:
                print("Warning: Docker daemon not accessible, waiting before retry...", flush=True)
                # Use exponential backoff for retries
                retry_delay = min(base_retry_delay * (2 ** retry_count), 300)  # max 5 minutes
                for _ in range(retry_delay):
                    if shutdown_event.is_set():
                        return
                    time.sleep(1)
                retry_count += 1
                continue

            # Reset retry count on successful connection
            retry_count = 0

            # Command to filter Docker events for container start and network disconnect
            cmd = "docker events --filter 'event=start' --filter 'event=disconnect'"

            print("Monitoring for Docker container start and network disconnect events", flush=True)

            # Start the subprocess
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            # Track if we've seen any events (to detect silent failures)
            last_event_time = time.time()
            heartbeat_interval = 300  # 5 minutes - check if process is still alive

            # Read events line by line
            while not shutdown_event.is_set():
                # Check if process is still running
                if process.poll() is not None:
                    # Process died unexpectedly
                    returncode = process.returncode
                    stderr_output = process.stderr.read() if process.stderr else ""

                    print(f"Docker events process exited unexpectedly (code {returncode})", flush=True)
                    if stderr_output:
                        print(f"Error output: {stderr_output.strip()}", flush=True)

                    # Break to trigger retry logic
                    break

                # Use select-like behavior with timeout to allow periodic checks
                if process.stdout in select.select([process.stdout], [], [], 1.0)[0]:
                    line = process.stdout.readline()
                    if line:
                        event_line = line.strip()
                        last_event_time = time.time()

                        # Parse the Docker event to extract relevant information
                        # Docker events format: timestamp event_type container/network/... attrs
                        event_info = parse_docker_event(event_line)
                        if event_info:
                            print(f"Event: {event_info}", flush=True)
                        else:
                            # Fallback to showing raw event if parsing fails
                            print(f"Event received: {event_line}", flush=True)

                        # Trigger debounced update
                        trigger_update()
                else:
                    # Check for heartbeat timeout (process might be hung)
                    current_time = time.time()
                    if current_time - last_event_time > heartbeat_interval:
                        # Reset heartbeat - Docker events can be silent for long periods
                        last_event_time = current_time

            # Clean up process if it's still running
            if process and process.poll() is None:
                print("Terminating Docker events monitoring process...", flush=True)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print("Force killing Docker events process...", flush=True)
                    process.kill()
                    process.wait()

            # If we're shutting down, exit cleanly
            if shutdown_event.is_set():
                print("Docker event monitoring stopped", flush=True)
                return

            # Otherwise, this was an unexpected termination - retry
            print("Docker event stream ended unexpectedly, retrying...", flush=True)
            retry_count += 1

            # Brief delay before retry
            retry_delay = min(base_retry_delay * (2 ** retry_count), 60)
            for _ in range(retry_delay):
                if shutdown_event.is_set():
                    return
                time.sleep(1)

        except subprocess.TimeoutExpired:
            print("Timeout checking Docker daemon availability", flush=True)
            retry_count += 1
            time.sleep(base_retry_delay)

        except KeyboardInterrupt:
            # Pass through keyboard interrupt for clean shutdown
            raise

        except Exception as e:
            print(f"Error in Docker event monitoring: {type(e).__name__}: {e}", flush=True)
            retry_count += 1

            # Clean up process if it exists
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except:
                    try:
                        process.kill()
                        process.wait()
                    except:
                        pass

            # Wait before retrying
            retry_delay = min(base_retry_delay * (2 ** retry_count), 60)
            for _ in range(retry_delay):
                if shutdown_event.is_set():
                    return
                time.sleep(1)

    # Max retries exceeded
    if retry_count >= max_retries:
        print(f"Docker event monitoring failed after {max_retries} retries, giving up", flush=True)
    else:
        print("Docker event monitoring stopped", flush=True)


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global debounce_timer, first_event_time

    print(f"\nReceived signal {signum}, shutting down gracefully...", flush=True)
    shutdown_event.set()

    # Cancel any pending debounce timer
    with debounce_lock:
        if debounce_timer is not None:
            debounce_timer.cancel()
            debounce_timer = None
        first_event_time = None


def main():
    """Main function."""
    try:
        # Check if running as root
        if os.geteuid() != 0:
            print("Warning: This script should be run as root to modify /etc/hosts", flush=True)

        # Ensure the hosts file exists
        if not os.path.exists(hosts_file):
            print(f"CRITICAL: {hosts_file} does not exist!", flush=True)
            if hosts_manager.restore_backup():
                print("Restored hosts file from backup", flush=True)
            else:
                print("Creating minimal hosts file", flush=True)
                with open(hosts_file, 'w') as f:
                    f.write("127.0.0.1\tlocalhost\n")
                    f.write("::1\tlocalhost ip6-localhost ip6-loopback\n")

        # Fix non-breaking spaces
        fix_non_breaking_spaces()

        # Ensure our section exists
        if not ensure_managed_section_exists():
            print("Warning: Could not ensure managed section exists", flush=True)

        if len(sys.argv) < 2:
            print("Usage: python script.py <interval>", flush=True)
            return

        interval_str = sys.argv[1]
        interval_seconds = parse_time_interval(interval_str)

        # Set up signal handlers
        signal(SIGTERM, handle_shutdown)
        signal(SIGINT, handle_shutdown)

        # Start periodic update thread
        periodic_thread = threading.Thread(
            target=periodic_update,
            args=(interval_seconds,),
            daemon=True
        )
        periodic_thread.start()

        # Run event monitoring in main thread (blocking)
        monitor_docker_events()

        # Wait for periodic thread to finish
        periodic_thread.join(timeout=5)

        print("Shutdown complete", flush=True)

    except Exception as e:
        print(f"Fatal error in main: {e}", flush=True)
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutdown requested", flush=True)
    except Exception as e:
        print(f"Fatal error: {e}", flush=True)
        sys.exit(1)
