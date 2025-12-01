import docker
import asyncio
import time
import logging
import logging.config
from typing import List, Dict, Set, Optional
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import subprocess
import signal
from collections import defaultdict
import uuid
from enum import Enum

global graph
running = True
app_ready = False


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
    _graph = DependencyGraph()
    containers = client.containers.list(all=True)
    for container in containers:
        labels = container.labels
        if labels.get("com.github.saltbox.saltbox_managed") == "true":
            name = container.name
            delay = int(labels.get("com.github.saltbox.depends_on.delay", 0))
            healthchecks = labels.get("com.github.saltbox.depends_on.healthchecks", "false") == "true"
            node = ContainerNode(name, delay, healthchecks)
            _graph.add_container(node)

            # Initialize health status
            try:
                health_status = container.attrs['State']['Health']['Status']
            except KeyError:
                health_status = "unknown"  # If health status is not available
            _graph.update_health_status(name, health_status)

    _graph.set_dependencies()
    return _graph


def has_healthcheck_configured(client, container_name: str, _graph: DependencyGraph) -> bool:
    try:
        container = client.containers.get(container_name)
        is_configured = 'Healthcheck' in container.attrs['Config'] and container.attrs['Config']['Healthcheck'][
            'Test'] != ['NONE']
        _graph.set_healthcheck_configured(container_name, is_configured)
        return is_configured
    except Exception as e:
        logging.error(f"Error checking healthcheck configuration for container {container_name}: {e}")
        return False


def is_container_healthy(client, container_name: str) -> bool:
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


def start_containers_in_dependency_order(_graph: DependencyGraph, job_id: str, timeout: int = 600):
    client = docker.from_env()
    started_containers = set()
    containers_to_start = set(_graph.nodes.keys())
    logged_health_check_waiting = set()
    skip_start_due_to_placeholder = set()
    container_start_times = defaultdict(lambda: 0.0)
    
    start_time = time.time()

    while containers_to_start and running:
        if time.time() - start_time > timeout:
            logging.error(f"Container start operation timed out after {timeout} seconds")
            job_manager.update_job(job_id, JobStatus.FAILED)
            return

        current_time = time.time()
        ready_to_start = []

        for container_name in list(containers_to_start):
            container = _graph.nodes[container_name]

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
                        logging.warning(
                            f"Skipping start of '{container_name}' due to placeholder dependency '{parent.name}'.")
                        skip_start_due_to_placeholder.add(container_name)
                    break

                if has_healthcheck_configured(client, parent.name, _graph) and not is_container_healthy(client, parent.name):
                    dependencies_ready = False
                    if container_name not in logged_health_check_waiting:
                        logging.info(
                            f"Container '{container_name}' is waiting for the health check of dependency '{parent.name}'.")
                        logged_health_check_waiting.add(container_name)
                    break
                elif parent.name not in started_containers:
                    dependencies_ready = False
                    break

            if dependencies_ready:
                if container.delay > 0:
                    if container_start_times[container_name] == 0.0:
                        container_start_times[container_name] = current_time + float(container.delay)
                        logging.info(f"Container '{container_name}' is scheduled to start in {container.delay} seconds")
                    elif current_time >= container_start_times[container_name]:
                        ready_to_start.append(container_name)
                else:
                    ready_to_start.append(container_name)

        start_containers_with_shell(ready_to_start)
        for container_name in ready_to_start:
            started_containers.add(container_name)
            containers_to_start.remove(container_name)
            if container_name in logged_health_check_waiting:
                logged_health_check_waiting.remove(container_name)
            if container_name in container_start_times:
                del container_start_times[container_name]
    
        time.sleep(1)

    job_manager.update_job(job_id, JobStatus.COMPLETED)


def stop_containers_with_shell(containers: List[str], ignore_containers: Set[str] = None):
    if ignore_containers is None:
        ignore_containers = set()
    if containers:
        containers_to_stop = [c for c in containers if c not in ignore_containers]
        if containers_to_stop:
            try:
                logging.info(f"Stopping containers: {', '.join(containers_to_stop)}")
                subprocess.run(['docker', 'stop', *containers_to_stop], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=True)
                logging.info(f"Stopped containers: {', '.join(containers_to_stop)}")
            except subprocess.CalledProcessError as err:
                logging.error(f"Failed to stop containers with error: {err}")


def stop_containers_in_dependency_order(_graph: DependencyGraph, ignore_containers: Set[str] = None, job_id: str = None, timeout: int = 600):
    if ignore_containers is None:
        ignore_containers = set()
    stopped_containers = set()
    containers_to_stop = set(_graph.nodes.keys()) - ignore_containers
    
    start_time = time.time()

    while containers_to_stop:
        # Check if we've exceeded the timeout
        if time.time() - start_time > timeout:
            logging.error(f"Container stop operation timed out after {timeout} seconds")
            if job_id:
                job_manager.update_job(job_id, JobStatus.FAILED)
            return

        ready_to_stop = []

        for container_name in list(containers_to_stop):
            container = _graph.nodes[container_name]
            if container.is_placeholder or container_name in stopped_containers:
                containers_to_stop.remove(container_name)
                continue

            # A container is ready to stop if all its children are already stopped
            if all(child.name in stopped_containers for child in container.children):
                ready_to_stop.append(container_name)

        stop_containers_with_shell(ready_to_stop, ignore_containers)
        for container_name in ready_to_stop:
            stopped_containers.add(container_name)
            containers_to_stop.remove(container_name)

        time.sleep(1)

    if job_id:
        job_manager.update_job(job_id, JobStatus.COMPLETED)


def stop_server(*args):
    global running
    running = False


# Job Manager Classes
class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobManager:
    def __init__(self):
        self.jobs: Dict[str, JobStatus] = {}

    def create_job(self) -> str:
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = JobStatus.PENDING
        return job_id

    def update_job(self, job_id: str, status: JobStatus):
        if job_id in self.jobs:
            self.jobs[job_id] = status

    def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        return self.jobs.get(job_id)

job_manager = JobManager()


# FastAPI Application and API Endpoints
@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_ready
    retry_attempts = 10
    retry_delay = 5

    for attempt in range(1, retry_attempts + 1):
        try:
            client = docker.from_env(timeout=10)
            docker_version = client.version()
            logging.info(f"Using Docker version: {docker_version['Components'][0]['Version']}")

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


@app.on_event("startup")
def startup_event():
    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)


@app.get("/ping")
async def ping():
    if app_ready:
        return {"message": "pong"}
    raise HTTPException(status_code=503, detail="Application not ready")


@app.post("/start")
async def start_containers(background_tasks: BackgroundTasks, timeout: int = Query(default=600, description="Timeout in seconds")):
    if is_blocked:
        raise HTTPException(status_code=200, detail="Operation blocked")
    
    job_id = job_manager.create_job()
    client = docker.from_env()
    _graph = parse_container_labels(client)

    def start_containers_task():
        job_manager.update_job(job_id, JobStatus.RUNNING)
        try:
            start_containers_in_dependency_order(_graph, job_id, timeout)
        except Exception as e:
            logging.error(f"Failed to start containers: {str(e)}")
            job_manager.update_job(job_id, JobStatus.FAILED)
    
    background_tasks.add_task(start_containers_task)
    return {"job_id": job_id}


@app.post("/stop")
async def stop_containers(background_tasks: BackgroundTasks, ignore: List[str] = Query(None), timeout: int = Query(default=600, description="Timeout in seconds")
):
    if is_blocked:
        raise HTTPException(status_code=200, detail="Operation blocked")

    job_id = job_manager.create_job()
    client = docker.from_env()
    _graph = parse_container_labels(client)
    ignore_containers = set(ignore) if ignore else set()

    def stop_containers_task():
        job_manager.update_job(job_id, JobStatus.RUNNING)
        try:
            stop_containers_in_dependency_order(_graph, ignore_containers, job_id, timeout)
        except Exception as e:
            logging.error(f"Failed to stop containers: {str(e)}")
            job_manager.update_job(job_id, JobStatus.FAILED)
    
    background_tasks.add_task(stop_containers_task)
    return {"job_id": job_id}


@app.get("/job_status/{job_id}")
async def get_job_status(job_id: str):
    status = job_manager.get_job_status(job_id)
    if status:
        return JSONResponse(status_code=200, content={"status": status})
    else:
        return JSONResponse(status_code=404, content={"status": "not_found"})


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
