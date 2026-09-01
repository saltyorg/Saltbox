#!/usr/bin/env python3

from __future__ import annotations

import fcntl
import importlib.util
import multiprocessing
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "library" / "saltbox_facts.py"


def load_saltbox_facts() -> Any:
    if "ansible.module_utils.basic" not in sys.modules:
        ansible = types.ModuleType("ansible")
        module_utils = types.ModuleType("ansible.module_utils")
        basic = types.ModuleType("ansible.module_utils.basic")
        basic.AnsibleModule = object
        ansible.module_utils = module_utils
        module_utils.basic = basic
        sys.modules["ansible"] = ansible
        sys.modules["ansible.module_utils"] = module_utils
        sys.modules["ansible.module_utils.basic"] = basic

    spec = importlib.util.spec_from_file_location("saltbox_facts_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact_operation_worker(
    operation: str,
    file_path: str,
    instance: str,
    connection: Any,
    start_event: Any | None = None,
) -> None:
    module = load_saltbox_facts()
    if start_event is not None:
        start_event.wait()
    try:
        if operation == "save":
            module.process_facts(
                file_path,
                instance,
                {"value": instance},
                os.getuid(),
                os.getgid(),
                0o640,
                True,
            )
        elif operation == "delete":
            module.delete_facts(file_path, "key", instance, {"value": ""})
        else:
            raise ValueError(f"Unknown operation: {operation}")
        connection.send(("ok", ""))
    except Exception as error:
        connection.send(("error", str(error)))
    finally:
        connection.close()


class SaltboxFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_saltbox_facts()
        self.process_context = multiprocessing.get_context("fork")

    def create_fact_file(self, directory: str) -> str:
        file_path = os.path.join(directory, "role.ini")
        self.module.process_facts(
            file_path,
            "default",
            {"value": "original"},
            os.getuid(),
            os.getgid(),
            0o640,
            True,
        )
        return file_path

    def test_save_and_delete_wait_for_shared_sidecar_lock(self) -> None:
        for operation in ("save", "delete"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                file_path = self.create_fact_file(directory)
                lock_file = open(file_path + ".lock", "a+", encoding="utf-8")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                receiver, sender = self.process_context.Pipe(duplex=False)
                process = self.process_context.Process(
                    target=fact_operation_worker,
                    args=(operation, file_path, "default", sender),
                )
                process.start()
                sender.close()

                completed_while_locked = receiver.poll(0.2)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

                self.assertTrue(receiver.poll(3), "fact operation did not resume after unlock")
                status, message = receiver.recv()
                process.join(timeout=3)
                self.assertFalse(completed_while_locked, f"{operation} ignored the shared sidecar lock")
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(status, "ok", message)

    def test_lock_wait_has_a_bounded_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "role.ini")
            with open(file_path + ".lock", "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                with self.assertRaisesRegex(TimeoutError, "Timed out"):
                    self.module.process_facts(
                        file_path,
                        "default",
                        {"value": "new"},
                        os.getuid(),
                        os.getgid(),
                        0o640,
                        True,
                        lock_timeout=0.05,
                    )

    def test_symlinked_sidecar_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "role.ini")
            victim = os.path.join(directory, "victim")
            Path(victim).write_text("unchanged", encoding="utf-8")
            os.symlink(victim, file_path + ".lock")

            with self.assertRaisesRegex(ValueError, "lock must not be a symbolic link"):
                self.module.process_facts(
                    file_path,
                    "default",
                    {"value": "new"},
                    os.getuid(),
                    os.getgid(),
                    0o640,
                    True,
                )
            self.assertEqual(Path(victim).read_text(encoding="utf-8"), "unchanged")

    def test_concurrent_writers_preserve_every_instance(self) -> None:
        writers = 24
        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "role.ini")
            start_event = self.process_context.Event()
            receivers: list[Any] = []
            processes: list[Any] = []
            for writer in range(writers):
                receiver, sender = self.process_context.Pipe(duplex=False)
                process = self.process_context.Process(
                    target=fact_operation_worker,
                    args=("save", file_path, f"instance-{writer}", sender, start_event),
                )
                process.start()
                sender.close()
                receivers.append(receiver)
                processes.append(process)

            start_event.set()
            for receiver in receivers:
                self.assertTrue(receiver.poll(5), "concurrent fact writer did not finish")
                status, message = receiver.recv()
                self.assertEqual(status, "ok", message)
            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)

            config = self.module.read_config(file_path)
            self.assertEqual(set(config.sections()), {f"instance-{writer}" for writer in range(writers)})

    def test_semicolon_comment_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be interpreted as a comment"):
            self.module.validate_key_name("  ;hidden")


if __name__ == "__main__":
    unittest.main()
