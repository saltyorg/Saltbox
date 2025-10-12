import asyncio
import subprocess
import sys
import tempfile
import os
import shutil
from pathlib import Path
from signal import SIGTERM, SIGINT
import fcntl
import time

hosts_file = '/etc/hosts'
hosts_backup = '/etc/hosts.backup'
begin_block = "# BEGIN DOCKER CONTAINERS"
end_block = "# END DOCKER CONTAINERS"

# Lock file to prevent concurrent modifications
lock_file = '/var/lock/docker_hosts_update.lock'


class HostsFileManager:
    """Manages safe updates to /etc/hosts file with proper locking and backup."""
    
    def __init__(self):
        self.hosts_file = hosts_file
        self.hosts_backup = hosts_backup
        self.lock_fd = None
    
    def acquire_lock(self, timeout=10):
        """Acquire exclusive lock with timeout."""
        self.lock_fd = os.open(lock_file, os.O_CREAT | os.O_WRONLY)
        start_time = time.time()
        
        while True:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except IOError:
                if time.time() - start_time > timeout:
                    print(f"Failed to acquire lock after {timeout} seconds", flush=True)
                    os.close(self.lock_fd)
                    self.lock_fd = None
                    return False
                time.sleep(0.1)
    
    def release_lock(self):
        """Release the exclusive lock."""
        if self.lock_fd:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
            except:
                pass
            self.lock_fd = None
    
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
    
    def validate_hosts_file(self, filepath):
        """Validate that a hosts file has proper structure."""
        try:
            with open(filepath, 'r') as f:
                content = f.read()
                
            # Basic validation checks
            if not content.strip():
                print(f"Error: File {filepath} is empty", flush=True)
                return False
            
            # Check for essential entries
            if '127.0.0.1' not in content and '::1' not in content:
                print(f"Warning: File {filepath} missing localhost entries", flush=True)
            
            # Check for our markers
            if begin_block in content and end_block not in content:
                print(f"Error: File {filepath} has BEGIN marker but no END marker", flush=True)
                return False
            
            if end_block in content and begin_block not in content:
                print(f"Error: File {filepath} has END marker but no BEGIN marker", flush=True)
                return False
            
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
        # Acquire lock before modifying
        if not hosts_manager.acquire_lock():
            print("Could not acquire lock for ensuring managed section", flush=True)
            return False
        
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
            
        finally:
            hosts_manager.release_lock()
            
    except Exception as e:
        print(f"Error ensuring managed section: {e}", flush=True)
        return False


async def update_hosts_file_async():
    """
    Asynchronously run the synchronous update_hosts_file function
    to avoid blocking the asyncio event loop.
    """
    loop = asyncio.get_event_loop()
    print("Updating hosts file asynchronously", flush=True)
    await loop.run_in_executor(None, update_hosts_file)


def update_hosts_file():
    """Update the hosts file with Docker container information."""
    print(f"Updating hosts file", flush=True)
    
    # Acquire lock
    if not hosts_manager.acquire_lock():
        print("Could not acquire lock for update, skipping", flush=True)
        return False
    
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
            
            result = subprocess.run(shell_command, shell=True, capture_output=True, text=True)
            
            if result.returncode != 0:
                print(f"Error executing update command: {result.stderr}", flush=True)
                os.unlink(hosts_file_tmp)
                return False
            
            # Validate the new file before replacing
            if not hosts_manager.validate_hosts_file(hosts_file_tmp):
                print("Validation failed for new hosts file, not updating", flush=True)
                os.unlink(hosts_file_tmp)
                return False
            
            # Check that the temp file is not empty
            if os.path.getsize(hosts_file_tmp) == 0:
                print("Error: Generated hosts file is empty, not updating", flush=True)
                os.unlink(hosts_file_tmp)
                return False
            
            # Set proper permissions
            os.chmod(hosts_file_tmp, 0o644)
            
            # Atomic move
            os.replace(hosts_file_tmp, hosts_file)
            
            print("Hosts file updated successfully", flush=True)
            return True
            
        except Exception as e:
            print(f"Error during hosts file update: {e}", flush=True)
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
            
    finally:
        hosts_manager.release_lock()


async def periodic_update(interval_str):
    """Periodically update the hosts file."""
    interval_seconds = parse_time_interval(interval_str)
    
    # Do an initial update
    await update_hosts_file_async()
    
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await update_hosts_file_async()
        except asyncio.CancelledError:
            print("Periodic update task cancelled", flush=True)
            break
        except Exception as e:
            print(f"Error in periodic update: {e}", flush=True)
            # Continue running even if there's an error


async def monitor_docker_events():
    """Monitor Docker events and update hosts file on relevant events."""
    try:
        # Command to filter Docker events for container start and network disconnect
        cmd = "docker events --filter 'event=start' --filter 'event=disconnect'"
        # Start the subprocess
        process = await asyncio.create_subprocess_shell(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE
        )

        print("Monitoring for Docker container start and network disconnect events", flush=True)
        
        async for line in process.stdout:
            event = line.decode('utf-8').strip()
            print(f"Event received: {event}", flush=True)
            
            # Add a small delay to allow Docker to fully update container state
            await asyncio.sleep(0.5)
            await update_hosts_file_async()

        # Wait for the process to exit
        await process.wait()
        
    except asyncio.CancelledError:
        print("Docker event monitoring task cancelled", flush=True)
        if 'process' in locals():
            process.terminate()
            await process.wait()
    except Exception as e:
        print(f"Error in Docker event monitoring: {e}", flush=True)


async def main():
    """Main async function."""
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
        
        loop = asyncio.get_running_loop()
        
        # Fix non-breaking spaces
        fix_non_breaking_spaces()
        
        # Ensure our section exists
        if not ensure_managed_section_exists():
            print("Warning: Could not ensure managed section exists", flush=True)
        
        if len(sys.argv) < 2:
            print("Usage: python script.py <interval>", flush=True)
            return
        
        interval_str = sys.argv[1]
        
        # Start both the Docker event monitoring and the periodic update tasks
        events_task = asyncio.create_task(monitor_docker_events())
        update_task = asyncio.create_task(periodic_update(interval_str))
        
        def _cancel():
            events_task.cancel()
            update_task.cancel()
        
        loop.add_signal_handler(SIGTERM, _cancel)
        loop.add_signal_handler(SIGINT, _cancel)
        
        await asyncio.gather(
            events_task,
            update_task,
            return_exceptions=True
        )
        
    except Exception as e:
        print(f"Fatal error in main: {e}", flush=True)
        sys.exit(1)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested", flush=True)
    except Exception as e:
        print(f"Fatal error: {e}", flush=True)
        sys.exit(1)
