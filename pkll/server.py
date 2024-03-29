import atexit
import os
import platform
import select
import signal
import stat
import subprocess
import warnings
from pathlib import Path
from typing import List

import msgpack
import requests

BINARIES = {
    ("darwin", "64bit"): "pkl-macos-amd64",
    ("darwin", "aarch64"): "pkl-macos-aarch64",
    ("linux", "64bit"): "pkl-linux-amd64",
    ("linux", "aarch64"): "pkl-linux-aarch64",
    ("linux", "64bit", "alpine"): "pkl-alpine-linux-amd64",
}
PKL_VERSION = "0.25.2"
BASE_PATH = "https://github.com/apple/pkl/releases/download/"


def preexec_function():
    # Cause the child process to be terminated when the parent exits
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def detect_system():
    os_name = platform.system().lower()
    arch, _ = platform.architecture()
    return os_name, arch


def is_alpine_linux():
    return os.path.isfile("/etc/alpine-release")


def execute_binary(binary_path):
    subprocess.run([binary_path, "server"], check=True)


def download_binary(file, target_fp):
    url = BASE_PATH + PKL_VERSION + "/" + file
    response = requests.get(url)
    with open(target_fp, "wb") as f:
        f.write(response.content)


def get_binary_path():
    os_name, arch = detect_system()
    if os_name == "linux" and is_alpine_linux():
        os_name = "alpine"
    binary_key = (os_name, arch)
    if binary_key == ("linux", "64bit") and is_alpine_linux():
        binary_key += ("alpine",)
    bin_file = BINARIES.get(binary_key)

    if bin_file is None:
        raise OSError("No compatible binary found for your system.")

    bin_parent_path = (Path("~/.pkl/bin/") / PKL_VERSION).expanduser()
    binary_path = bin_parent_path / bin_file

    if not binary_path.exists():
        binary_path.parent.mkdir(exist_ok=True, parents=True)
        download_binary(bin_file, binary_path)

    current_permissions = os.stat(binary_path).st_mode
    new_permissions = current_permissions | stat.S_IXUSR
    os.chmod(binary_path, new_permissions)

    return binary_path


_PROCESSES = []


def terminate_processes():
    for process in _PROCESSES:
        process.terminate()
        process.wait()


atexit.register(terminate_processes)


class PKLServer:
    def __init__(self):
        self._process: subprocess.Popen = None
        self._cmd = [get_binary_path(), "server"]
        self.next_request_id = 1

    def get_request_id(self):
        ret = self.next_request_id
        self.next_request_id += 1
        return ret

    def start_process(self, debug=False):
        if self._process:
            return

        env = os.environ.copy()
        if debug:
            env = {"PKL_DEBUG": "1"}

        self._process = subprocess.Popen(
            self._cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            preexec_fn=preexec_function,
            env=env,
        )
        stdout_fd = self._process.stdout.fileno()
        stderr_fd = self._process.stderr.fileno()
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        _PROCESSES.append(self._process)

    def check_process(self, is_raise=False):
        if self._process is None:
            if is_raise:
                raise ValueError("Start server first with 'start_process'")
            return False
        return True

    def send_message(self, msg_obj):
        self.check_process(is_raise=True)

        encoded_message = msgpack.packb(msg_obj)
        self._process.stdin.write(encoded_message)
        self._process.stdin.flush()

    def receive_message(self, timeout=None, empty_break=False) -> List:
        self.check_process(is_raise=True)

        stdout = self._process.stdout
        stderr = self._process.stderr
        outputs = []

        while True:
            readable, _, _ = select.select([stdout, stderr], [], [], timeout)
            if empty_break and len(readable) == 0:
                return

            for stream in readable:
                output = stream.read()

                if stream == stdout:
                    outputs.append(output)
                else:  # stderr
                    print(output.decode(), end="")

            if len(outputs) > 0:
                break

            # Check if the process has terminated
            if self._process.poll() is not None:
                print("Process has terminated")
                break

        assert len(outputs) == 1
        unpacker = msgpack.Unpacker()
        unpacker.feed(outputs[0])
        responses = list(unpacker)
        return responses

    def receive_with_retry(self, max_retry=5) -> List:
        response = None
        retry = 0
        while response is None and retry <= max_retry:
            response = self.receive_message()
            retry += 1
        if retry == max_retry:
            warnings.warn("Max retry reached.")
        return response

    def send_and_receive(self, msg_obj) -> List:
        self.send_message(msg_obj)
        response = self.receive_message()
        return response

    def terminate(self):
        self._process.terminate()
        self._process.wait()
        self._process = None
