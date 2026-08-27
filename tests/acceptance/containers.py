"""Running the real image for the acceptance suite.

The docker command line rather than a client library: one fewer dependency to
keep patched in a project whose whole posture is that each one is a
responsibility, and the commands are the same ones a person would type.
"""

import json
import os
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field

IMAGE = "gpodsync:acceptance"
READY_TIMEOUT_SECONDS = 60


def build_image() -> None:
    """Build the image, unless one has deliberately been put there already.

    The release pipeline builds an image, audits it, and then runs this suite
    against it. Rebuilding here would quietly replace the audited bits with a
    second build from a different cache — so the audit would have examined one
    image and the suite exercised another, while the workflow claimed they were
    the same thing.
    """
    if os.environ.get("GPODSYNC_ACCEPTANCE_PREBUILT") == "1":
        present = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
        if present.returncode != 0:
            raise RuntimeError(
                f"GPODSYNC_ACCEPTANCE_PREBUILT is set but {IMAGE} is not present. "
                f"Refusing to build a different image than the one that was audited."
            )
        return

    subprocess.run(["docker", "build", "-q", "-t", IMAGE, "."], check=True, capture_output=True)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Container:
    name: str
    port: int
    volume: str
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            self.name,
            # The shape the documentation recommends, so the suite proves the
            # documented deployment rather than a more permissive one.
            "--user",
            "10001:10001",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            f"{self.volume}:/data",
            "-p",
            f"127.0.0.1:{self.port}:8000",
        ]
        for key, value in self.environment.items():
            command += ["-e", f"{key}={value}"]
        command.append(IMAGE)
        subprocess.run(command, check=True, capture_output=True)

    def wait_until_healthy(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{json .State}}", self.name],
                capture_output=True,
                text=True,
            )
            if state.returncode == 0:
                status = json.loads(state.stdout)
                if status.get("Health", {}).get("Status") == "healthy":
                    return
                if not status.get("Running"):
                    raise RuntimeError(f"{self.name} exited:\n{self.logs()}")
            time.sleep(0.5)
        raise RuntimeError(f"{self.name} never became healthy:\n{self.logs()}")

    def logs(self) -> str:
        return (
            subprocess.run(["docker", "logs", self.name], capture_output=True, text=True).stdout
            + subprocess.run(["docker", "logs", self.name], capture_output=True, text=True).stderr
        )

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", self.volume], capture_output=True)


def running(**environment: str) -> Container:
    suffix = uuid.uuid4().hex[:8]
    container = Container(
        name=f"gpodsync-acceptance-{suffix}",
        port=free_port(),
        volume=f"gpodsync-acceptance-{suffix}",
        environment={
            "GPODSYNC_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "GPODSYNC_SECRET_KEY": "acceptance-only-not-a-real-key",
            # The suite speaks http, as a LAN deployment does.
            "GPODSYNC_SESSION_COOKIE_SECURE": "false",
            **environment,
        },
    )
    container.start()
    return container
