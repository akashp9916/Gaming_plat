"""
Starts / stops one Docker container per play session.

Each container is a "streaming image" (see README): it runs the game on a GPU,
encodes the screen with NVENC and serves it to the browser over WebRTC on
port 8080. Examples: Selkies-GStreamer, Sunshine + Moonlight-Web, Kasm.

Big games are NOT copied per user. They live once on the server in
GAMES_DIR and are mounted read-only into every container. Each user gets a
private, writable save folder per game.
"""
import asyncio
import os

import docker
from docker.types import DeviceRequest

GAMES_DIR = os.getenv("GAMES_DIR", "/data/games")
SAVES_DIR = os.getenv("SAVES_DIR", "/data/saves")
CONTAINER_PORT = "8080/tcp"

_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _start(session_id: str, user_id: str, game: dict, stream_token: str) -> dict:
    save_path = os.path.join(SAVES_DIR, user_id, game["slug"])
    os.makedirs(save_path, exist_ok=True)

    container = client().containers.run(
        game["docker_image"],
        detach=True,
        name=f"play-{session_id}",
        auto_remove=True,
        ports={CONTAINER_PORT: None},  # Docker picks a free host port
        device_requests=[DeviceRequest(count=1, capabilities=[["gpu"]])],
        mem_limit=game.get("memory_limit", "16g"),
        nano_cpus=int(game.get("cpus", 4) * 1e9),
        shm_size="2g",
        environment={
            "STREAM_PASSWORD": stream_token,   # one-time password for the stream
            "GAME_EXECUTABLE": game["executable"],
            "RESOLUTION": game.get("resolution", "1920x1080"),
        },
        volumes={
            os.path.join(GAMES_DIR, game["slug"]): {"bind": "/games", "mode": "ro"},
            save_path: {"bind": "/saves", "mode": "rw"},
        },
        labels={"cloudplay": "1", "user": user_id},
    )
    container.reload()
    host_port = container.attrs["NetworkSettings"]["Ports"][CONTAINER_PORT][0]["HostPort"]
    return {"container_id": container.id, "host_port": int(host_port)}


def _stop(container_id: str) -> None:
    try:
        client().containers.get(container_id).stop(timeout=10)
    except docker.errors.NotFound:
        pass  # already gone (auto_remove)


async def start_container(session_id, user_id, game, stream_token):
    return await asyncio.to_thread(_start, session_id, user_id, game, stream_token)


async def stop_container(container_id):
    await asyncio.to_thread(_stop, container_id)
