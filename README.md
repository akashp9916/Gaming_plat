# CloudPlay – play games in the browser, nothing installed on the player's PC

## How it works

```
Player's browser  <--- WebRTC video/audio + keyboard/mouse/gamepad --->  GPU server
      |                                                                     |
      |  login, game list, start/stop (HTTPS)                               |  Docker container per session
      v                                                                     v
  FastAPI backend (this code)  <---------- MongoDB (users, games, sessions)
```

1. The game runs on **your server's GPU**, inside a Docker container.
2. The container captures the screen, encodes it (NVENC) and streams it to the browser (WebRTC).
3. Keyboard / mouse / controller input travels back. The player installs nothing.
4. This Python backend handles accounts, the game library, starting/stopping containers,
   limits, and playtime tracking. MongoDB stores everything.

Game size is not a problem for players: games are stored once on the server (`/data/games/<slug>`)
and mounted read-only into every session. Saves go to `/data/saves/<user>/<game>`.

## Server requirements

- Linux with an NVIDIA GPU, NVIDIA driver and `nvidia-container-toolkit`
- Docker + Docker Compose
- Fast uplink: about 15-25 Mbps per player at 1080p60. Keep servers close to players (latency under 40 ms).
- One GPU per concurrent player, or use GPUs that can be shared (e.g. vGPU / MIG-like setups).

## The streaming image (the one piece Python does not replace)

Use an existing open-source streaming stack as `docker_image` for each game:

- **Selkies-GStreamer**: WebRTC desktop/game streaming in a plain browser
- **Sunshine + Moonlight Web**: very low latency, popular for gaming
- **Kasm Workspaces**: container streaming with a ready-made admin UI

The image must: listen on port **8080**, read env vars `STREAM_PASSWORD`, `GAME_EXECUTABLE`,
`RESOLUTION`, mount `/games` (read-only) and `/saves`, and launch the game
(Windows games via Wine/Proton).

## Run

```bash
sudo mkdir -p /data/games /data/saves
# put games in /data/games/<slug>/
docker compose up -d --build
```

API docs: `http://SERVER:8000/docs`

## Quick test (curl)

```bash
# 1. register + login (use the email listed in ADMIN_EMAILS to become admin)
curl -X POST :8000/auth/register -H 'Content-Type: application/json' \
  -d '{"username":"raj","email":"you@example.com","password":"password123"}'
TOKEN=$(curl -s -X POST :8000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"password123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2. admin adds a game
curl -X POST :8000/games -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"My Game","slug":"my-game","size_gb":80,"genre":["action"],
       "docker_image":"your-selkies-image:latest","executable":"MyGame.exe"}'

# 3. list games and start playing
curl :8000/games -H "Authorization: Bearer $TOKEN"
curl -X POST :8000/sessions/start/<GAME_ID> -H "Authorization: Bearer $TOKEN"
# -> returns stream_url + stream_password; open stream_url in the browser

# 4. the web page calls this every 20 s, otherwise the session is auto-stopped after 60 s
curl -X POST :8000/sessions/<SESSION_ID>/heartbeat -H "Authorization: Bearer $TOKEN"
curl -X POST :8000/sessions/<SESSION_ID>/stop -H "Authorization: Bearer $TOKEN"
```

## MongoDB collections

- `users`: username, email, password_hash, role, total_playtime_seconds
- `games`: title, slug, genre, size_gb, docker_image, executable, play_count
- `sessions`: user_id, game_id, container_id, host_port, status, started_at, last_heartbeat, duration_seconds

## Before going public

- Put HTTPS (Caddy/Nginx) in front, and set a strong `JWT_SECRET`
- Restrict CORS origins
- Only host games you own or have a licence to stream
- Add payments, queueing, and a React/Vue front end for the game library


"""
CloudPlay backend: FastAPI + MongoDB (Motor).

Run:  uvicorn main:app --host 127.0.0.1 --port 9000 --reload
Docs: http://localhost:8000/docs
"""
