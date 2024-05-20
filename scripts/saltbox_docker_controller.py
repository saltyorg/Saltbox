import docker
import asyncio
import time
import logging
import logging.config
from typing import List, Dict
from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
import subprocess
import signal

# Global flag to indicate shutdown
shutdown_flag = False
app_ready = False

# Signal handler
def signal_handler(signum, frame):
    global shutdown_flag
    shutdown_flag = True

# Register the signal handler
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

class ColorLogFormatter(logging.Formatter):
    """ Custom formatter to add colors to log level names. """

    COLOR_CODES = {
        logging.DEBUG: "\033[0;36m",  # Cyan for DEBUG
        logging.INFO: "\033[0;32m",  # Green for INFO
        logging.WARNING: "\033[0;33m",  # Yellow for WARNING
        logging.ERROR: "\033[0;31m",  # Red for ERROR
        logging.CRITICAL: "\033[1;31m"  # Bright Red for CRITICAL
    }

    RESET_CODE = "\033[0m"

    def format(self, record):
        color_code = self.COLOR_CODES.get(record.levelno, self.RESET_CODE)
        record.levelname = f"{color_code}{record.levelname}{self.RESET_CODE}"
        return super().format(record)


# Define a logging configuration
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'colored': {
            '()': ColorLogFormatter,
            'format': '%(asctime)s - %(levelname)s - %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'colored',
            'stream': 'ext://sys.stdout',
        },
    },
    'loggers': {
        '': {
            'handlers': ['console'],
            'level': 'INFO',
        },
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
start_containers_lock = asyncio.Lock()


# Container Node Class
class ContainerNode:
    def __init__(self, name: str, delay: int = 0, healthcheck_enabled: bool = False, is_placeholder: bool = False):
        self.name = name
        self.delay = delay
        self.healthcheck_enabled = healthcheck_enabled
        self.is_placeholder = is_placeholder
        self.children: List['ContainerNode'] = []  # Containers that depend on this container
        self.parents: List['ContainerNode'] = []  # Containers that this container depends on

    def add_child(self, child: 'ContainerNode'):
        if child not in self.children:
            self.children.append(child)

    def add_parent(self, parent: 'ContainerNode'):
        if parent not in self.parents:
            self.parents.append(parent)


# Dependency Graph Class
class DependencyGraph:
    def __init__(self):
        self.nodes: Dict[str, ContainerNode] = {}
        self.health_status = {}
        self.healthcheck_config_cache = {}

    def set_healthcheck_configured(self, container_name: str, is_configured: bool):
        self.healthcheck_config_cache[container_name] = is_configured

    def is_healthcheck_configured(self, container_name: str) -> bool:
        return self.healthcheck_config_cache.get(container_name, False)

    def update_health_status(self, container_name: str, status: str):
        self.health_status[container_name] = status

    def get_health_status(self, container_name: str) -> str:
        return self.health_status.get(container_name, "unknown")

    def add_container(self, container: ContainerNode):
        self.nodes[container.name] = container

    def set_dependencies(self):
        # Create a static list of keys to iterate over
        container_names = list(self.nodes.keys())
        for container_name in container_names:
            container = self.nodes[container_name]
            depends_on = self.parse_depends_on_label(container_name)
            for dep_name in depends_on:
                if dep_name not in self.nodes:
                    placeholder_node = ContainerNode(dep_name, is_placeholder=True)
                    self.nodes[dep_name] = placeholder_node
                    logging.warning(f"Created placeholder node for missing dependency: {dep_name}")

                # Add the current container as a child of each of its dependencies
                self.nodes[dep_name].add_child(container)
                # And also add each dependency as a parent of the current container
                container.add_parent(self.nodes[dep_name])

    def parse_depends_on_label(self, container_name: str) -> List[str]:
        client = docker.from_env()
        container = client.containers.get(container_name)
        depends_on_label = container.labels.get("com.github.saltbox.depends_on")
        # Splitting the label by comma and stripping spaces to get all names
        if depends_on_label:
            return [name.strip() for name in depends_on_label.split(',')]
        return []


# Function to parse Docker container labels and populate the graph
def parse_container_labels(client):
    graph = DependencyGraph()
    containers = client.containers.list(all=True)
    for container in containers:
        labels = container.labels
        if labels.get("com.github.saltbox.saltbox_managed") == "true":
            name = container.name
            delay = int(labels.get("com.github.saltbox.depends_on.delay", 0))
            healthchecks = labels.get("com.github.saltbox.depends_on.healthchecks", "false") == "true"
            node = ContainerNode(name, delay, healthchecks)
            graph.add_container(node)

            # Initialize health status
            try:
                health_status = container.attrs['State']['Health']['Status']
            except KeyError:
                health_status = "unknown"  # If health status is not available
            graph.update_health_status(name, health_status)

    graph.set_dependencies()
    return graph


def has_healthcheck_configured(client, container_name: str, graph: DependencyGraph) -> bool:
    try:
        container = client.containers.get(container_name)
        is_configured = 'Healthcheck' in container.attrs['Config'] and container.attrs['Config']['Healthcheck']['Test'] != ['NONE']
        graph.set_healthcheck_configured(container_name, is_configured)
        return is_configured
    except Exception as e:
        logging.error(f"Error checking healthcheck configuration for container {container_name}: {e}")
        return False


def is_container_healthy(client, container_name: str, graph: DependencyGraph) -> bool:
    # Add a delay before checking the container health
    wait_for_delay(1)

    try:
        container = client.containers.get(container_name)
        health_status = container.attrs['State']['Health']['Status']
        logging.debug(f"Container {container_name} health status: '{health_status}'")
        return health_status == 'healthy'
    except Exception as e:
        logging.error(f"Error checking health for container {container_name}: {e}")
        return False


def wait_for_delay(delay: int):
    time.sleep(delay)


def start_containers_with_shell(containers: List[str]):
    if containers:
        try:
            logging.info(f"Starting containers: {', '.join(containers)}")
            subprocess.run(['docker', 'start', *containers], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
            logging.info(f"Started containers: {', '.join(containers)}")
        except subprocess.CalledProcessError as err:
            logging.error(f"Failed to start containers with error: {err}")


def start_containers_in_dependency_order(graph: DependencyGraph):
    client = docker.from_env()
    started_containers = set()
    containers_to_start = set(graph.nodes.keys())
    logged_health_check_waiting = set()
    skip_start_due_to_placeholder = set()

    while containers_to_start and not shutdown_flag:
        ready_to_start = []

        for container_name in list(containers_to_start):
            container = graph.nodes[container_name]

            if container_name in skip_start_due_to_placeholder:
                containers_to_start.remove(container_name)
                continue

            if container.is_placeholder:
                logging.info(f"Skipping start of '{container_name}' because it is a placeholder.")
                containers_to_start.remove(container_name)
                skip_start_due_to_placeholder.add(container_name)
                continue

            dependencies_ready = True
            for parent in container.parents:
                if parent.is_placeholder:
                    dependencies_ready = False
                    if container_name not in skip_start_due_to_placeholder:
                        logging.warning(f"Skipping start of '{container_name}' due to placeholder dependency '{parent.name}'.")
                        skip_start_due_to_placeholder.add(container_name)
                    break

                if has_healthcheck_configured(client, parent.name, graph) and not is_container_healthy(client, parent.name, graph):
                    dependencies_ready = False
                    if container_name not in logged_health_check_waiting:
                        logging.info(f"Container '{container_name}' is waiting for the health check of dependency '{parent.name}'.")
                        logged_health_check_waiting.add(container_name)
                    break
                elif parent.name not in started_containers:
                    dependencies_ready = False
                    break

            if dependencies_ready:
                if container.delay > 0:
                    logging.info(f"Container '{container_name}' is waiting for delay: {container.delay} seconds")
                    wait_for_delay(container.delay)
                ready_to_start.append(container_name)

        start_containers_with_shell(ready_to_start)
        for container_name in ready_to_start:
            started_containers.add(container_name)
            containers_to_start.remove(container_name)
            if container_name in logged_health_check_waiting:
                logged_health_check_waiting.remove(container_name)


def stop_containers_with_shell(containers: List[str]):
    if containers:
        try:
            logging.info(f"Stopping containers: {', '.join(containers)}")
            subprocess.run(['docker', 'stop', *containers], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
            logging.info(f"Stopped containers: {', '.join(containers)}")
        except subprocess.CalledProcessError as err:
            logging.error(f"Failed to stop containers with error: {err}")


def stop_containers_in_dependency_order(graph: DependencyGraph):
    stopped_containers = set()
    containers_to_stop = set(graph.nodes.keys())

    while containers_to_stop:
        ready_to_stop = []

        for container_name in list(containers_to_stop):
            container = graph.nodes[container_name]
            if container.is_placeholder or container_name in stopped_containers:
                containers_to_stop.remove(container_name)
                continue

            # A container is ready to stop if all its children are already stopped
            if all(child.name in stopped_containers for child in container.children):
                ready_to_stop.append(container_name)

        stop_containers_with_shell(ready_to_stop)
        for container_name in ready_to_stop:
            stopped_containers.add(container_name)
            containers_to_stop.remove(container_name)


# FastAPI Application and API Endpoints
@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_ready
    retry_attempts = 10
    retry_delay = 5

    for attempt in range(1, retry_attempts + 1):
        try:
            client = docker.from_env()
            docker_version = client.version()
            logging.info(f"Using Docker version: {docker_version['Components'][0]['Version']}")
            
            # Initialize DependencyGraph
            global graph  # Declare graph as global if it's used elsewhere outside this context
            graph = DependencyGraph()
            app_ready = True  # Indicate that the app is now ready
            break  # Exit the loop if successful

        except Exception as e:
            logging.error(f"Attempt {attempt} - An error occurred during Docker initialization: {e}")
            if attempt < retry_attempts:
                await asyncio.sleep(retry_delay)
            else:
                logging.critical("Failed to initialize Docker after multiple attempts. Exiting.")
                raise SystemExit("Failed to initialize Docker. Exiting.")

    yield
    logging.info("Application shutdown complete")


app = FastAPI(lifespan=lifespan)
is_blocked = False
unblock_task = None


@app.get("/ping")
async def ping():
    if app_ready:
        return {"message": "pong"}
    raise HTTPException(status_code=503, detail="Application not ready")


@app.post("/start")
async def start_containers():
    if is_blocked:
        raise HTTPException(status_code=200, detail="Operation blocked")
    client = docker.from_env()
    graph = parse_container_labels(client)
    start_containers_in_dependency_order(graph)
    return {"message": "Containers are starting"}


@app.post("/stop")
async def stop_containers():
    if is_blocked:
        raise HTTPException(status_code=200, detail="Operation blocked")
    client = docker.from_env()
    graph = parse_container_labels(client)
    stop_containers_in_dependency_order(graph)
    return {"message": "Containers are stopping"}


async def auto_unblock(delay: int):
    await asyncio.sleep(delay)
    global is_blocked
    is_blocked = False
    logging.info("Auto unblock complete")


@app.post("/block/{duration_minutes}")
async def block_operations(duration_minutes: int = 10):
    global is_blocked, unblock_task
    is_blocked = True
    if unblock_task:
        unblock_task.cancel()
    unblock_task = asyncio.create_task(auto_unblock(duration_minutes * 60))
    logging.info(f"Operations are now blocked for {duration_minutes} minutes")
    return {"message": f"Operations are now blocked for {duration_minutes} minutes"}


@app.post("/unblock")
async def unblock_operations():
    global is_blocked, unblock_task
    is_blocked = False
    if unblock_task:
        unblock_task.cancel()
        unblock_task = None
    logging.info("Operations are now unblocked")
    return {"message": "Operations are now unblocked"}


# if __name__ == "__main__":
#     import uvicorn

#     uvicorn.run(app, host="127.0.0.1", port=8000, log_config=LOGGING_CONFIG)
