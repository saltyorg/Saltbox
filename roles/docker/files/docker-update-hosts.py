import asyncio
import subprocess
import sys
import tempfile

hosts_file = '/etc/hosts'
begin_block = "# BEGIN DOCKER CONTAINERS"
end_block = "# END DOCKER CONTAINERS"

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
    with open(hosts_file, 'r+') as file:
        contents = file.read()
        # Check if both markers exist
        if begin_block not in contents or end_block not in contents:
            print(f"Appending managed section markers to {hosts_file}.", flush = True)
            # Move to the end of the file before appending
            file.write(f"\n{begin_block}\n{end_block}\n")

async def update_hosts_file_async():
    """
    Asynchronously run the synchronous update_hosts_file function
    to avoid blocking the asyncio event loop.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, update_hosts_file)

def update_hosts_file():
    print(f"Updating hosts file", flush = True)
    
    # Create a temporary file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        hosts_file_tmp = temp_file.name
    
    # Build and execute the shell command
    shell_command = (
        "docker container ls -q | xargs -r docker container inspect | "
        "jq -r '.[] | select(.NetworkSettings.Networks[].IPAddress | length > 0) | "
        "select(.NetworkSettings.Networks[].Aliases != null) | select(.NetworkSettings.Networks[].Aliases | length > 0) | "
        ".NetworkSettings.Networks[].IPAddress as $ip | .NetworkSettings.Networks[].Aliases | map(select(length > 0)) | "
        "unique | map(. + \" \" + . + \".saltbox\") | join(\" \") | \"\($ip) \(.)\"' | "
        f"sed -ne \"/^{begin_block}$/ {{p; r /dev/stdin\" -e \":a; n; /^{end_block}$/ {{p; b}}; ba}}; p\" {hosts_file} > {hosts_file_tmp}"
    )
    
    subprocess.run(shell_command, shell=True, check=True)
    
    # Change file permission
    subprocess.run(['chmod', '644', hosts_file_tmp], check=True)
    
    # Move the temporary file to replace the original hosts file
    subprocess.run(['mv', hosts_file_tmp, hosts_file], check=True)

async def periodic_update(interval_str):
    interval_seconds = parse_time_interval(interval_str)
    while True:
        # Schedule the synchronous function to run without blocking the asyncio event loop
        await update_hosts_file_async()
        await asyncio.sleep(interval_seconds)

async def monitor_docker_events():
    # Command to filter Docker events for container start and network disconnect
    cmd = "docker events --filter 'event=start' --filter 'event=disconnect'"
    # Start the subprocess
    process = await asyncio.create_subprocess_shell(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    print("Monitoring for Docker container start and network disconnect events", flush = True)
    async for line in process.stdout:
        print(f"Event received: {line.decode('utf-8').strip()}", flush = True)
        await update_hosts_file_async()

    # Wait for the process to exit (though in practice, this might run indefinitely)
    await process.wait()

async def main():
    # Ensure the managed section exists
    ensure_managed_section_exists()

    if len(sys.argv) < 2:
        print("Usage: python script.py <interval>", flush = True)
        return

    interval_str = sys.argv[1]
    # Start both the Docker event monitoring and the periodic update tasks
    await asyncio.gather(
        monitor_docker_events(),
        periodic_update(interval_str)
    )

if __name__ == '__main__':
    asyncio.run(main())
