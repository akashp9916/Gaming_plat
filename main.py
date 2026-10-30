import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote

import bcrypt
import jwt
from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

import session_manager as sm

# ---------- config ----------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "cloudplay")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
JWT_HOURS = int(os.getenv("JWT_HOURS", "24"))
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "http://localhost")  # address players reach
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SESSIONS", "10"))   # = number of GPUs/slots
MAX_SESSION_MINUTES = int(os.getenv("MAX_SESSION_MINUTES", "240"))
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "60"))
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"   # no GPU/Docker needed: plays a built-in test game
DEMO_EMAIL = "demo@cloudplay.local"
DEMO_PASSWORD = "demo12345"

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
bearer = HTTPBearer()


def utcnow() -> datetime:
    return datetime.utcnow()


# ---------- app lifecycle ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.users.create_index("email", unique=True)
    await db.games.create_index("slug", unique=True)
    await db.sessions.create_index([("status", 1), ("last_heartbeat", 1)])
    await db.sessions.create_index("user_id")
    if DEMO_MODE:
        await seed_demo()
    reaper = asyncio.create_task(reaper_loop())
    yield
    reaper.cancel()


app = FastAPI(title="CloudPlay", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------- demo mode ----------
async def seed_demo():
    """Creates a demo admin account and sample games (only when DEMO_MODE=1)."""
    if not await db.users.find_one({"email": DEMO_EMAIL}):
        await db.users.insert_one({
            "username": "demo",
            "email": DEMO_EMAIL,
            "password_hash": bcrypt.hashpw(DEMO_PASSWORD.encode(), bcrypt.gensalt()).decode(),
            "role": "admin",
            "total_playtime_seconds": 0,
            "created_at": utcnow(),
        })
    samples = [  # title, slug, year, genres, size_gb, starting plays, description
        ("Cyberpunk 2077", "cyberpunk-2077", 2020, ["Action", "Adventure"], 70.0, 90, "An action role-playing game set in Night City, an open world in the Cyberpunk universe. Hack, fight and drive through a neon megacity."),
        ("Minecraft", "minecraft", 2011, ["Sandbox", "Adventure"], 1.0, 120, "Build, mine and survive in an endless world made of blocks."),
        ("Apex Legends", "apex-legends", 2019, ["Shooter", "Battle royale"], 56.0, 110, "Squad-based battle royale with unique legends and fast movement."),
        ("Need for Speed: Heat", "need-for-speed-heat", 2019, ["Racing", "Open world"], 50.0, 80, "Race by day, risk everything by night."),
        ("Red Dead Redemption 2", "red-dead-redemption-2", 2018, ["Action", "Open world"], 120.0, 100, "Outlaw life at the end of the Wild West era."),
        ("FIFA 22", "fifa-22", 2021, ["Sports", "Football"], 50.0, 95, "Play the world's game with realistic football action."),
        ("Spider-Man: Miles Morales", "spider-man-miles-morales", 2020, ["Action", "Adventure"], 50.0, 70, "Swing through a snowy New York as a new hero."),
    ]
    # replace the older sample lineup; real games (docker_image != "demo") are never touched
    await db.games.delete_many({"docker_image": "demo", "slug": {"$nin": [s[1] for s in samples]}})
    for i, (title, slug, year, genre, size, plays, desc) in enumerate(samples):
        await db.games.update_one(
            {"slug": slug},
            {"$set": {
                "title": title, "description": desc, "cover_url": f"/covers/{slug}.svg", "year": year,
                "genre": genre, "size_gb": size, "docker_image": "demo", "executable": "demo.exe",
                "cpus": 4, "memory_limit": "16g", "resolution": "1920x1080",
            },
             "$setOnInsert": {"created_at": utcnow() - timedelta(days=i), "play_count": plays}},
            upsert=True,
        )


def stream_url_for(host_port: int, game_title: str) -> str:
    if host_port == 0:  # demo session
        return f"/demo-stream.html?game={quote(game_title)}"
    return f"{PUBLIC_HOST}:{host_port}"


# ---------- helpers ----------
def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(400, "Invalid id")


def clean(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    doc.pop("password_hash", None)
    doc.pop("stream_token", None)
    return doc


async def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    try:
        payload = jwt.decode(cred.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = await db.users.find_one({"_id": oid(payload["sub"])})
    if not user:
        raise HTTPException(401, "User not found")
    return user


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    return user


# ---------- schemas ----------
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=30)
    email: str
    password: str = Field(min_length=8)


class LoginIn(BaseModel):
    email: str
    password: str


class GameIn(BaseModel):
    title: str
    slug: str                      # folder name inside GAMES_DIR
    description: str = ""
    cover_url: str = ""
    genre: list[str] = []
    size_gb: float = 0
    docker_image: str              # streaming image that can run this game
    executable: str                # path inside /games/<slug>, e.g. "bin/game.exe"
    cpus: int = 4
    memory_limit: str = "16g"
    resolution: str = "1920x1080"
    year: int = 0


# ---------- auth ----------
@app.post("/auth/register", status_code=201)
async def register(body: RegisterIn):
    email = body.email.lower()
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = {
        "username": body.username,
        "email": email,
        "password_hash": hashed,
        "role": "admin" if email in ADMIN_EMAILS else "player",
        "total_playtime_seconds": 0,
        "created_at": utcnow(),
    }
    try:
        res = await db.users.insert_one(user)
    except Exception:
        raise HTTPException(409, "Email already registered")
    return {"id": str(res.inserted_id)}


@app.post("/auth/login")
async def login(body: LoginIn):
    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Wrong email or password")
    token = jwt.encode(
        {"sub": str(user["_id"]), "exp": utcnow() + timedelta(hours=JWT_HOURS)},
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "token_type": "bearer"}


@app.get("/me")
async def me(user: dict = Depends(current_user)):
    return clean(user)


# ---------- game library ----------
@app.get("/games")
async def list_games(q: str = "", genre: str = "", skip: int = 0, limit: int = 20):
    query: dict = {}
    if q:
        query["title"] = {"$regex": q, "$options": "i"}
    if genre:
        query["genre"] = genre
    cursor = db.games.find(query).sort("title", 1).skip(skip).limit(min(limit, 100))
    return [clean(g) async for g in cursor]


@app.post("/games", status_code=201)
async def add_game(body: GameIn, _: dict = Depends(admin_user)):
    doc = body.model_dump() | {"created_at": utcnow(), "play_count": 0}
    try:
        res = await db.games.insert_one(doc)
    except Exception:
        raise HTTPException(409, "Slug already exists")
    return {"id": str(res.inserted_id)}


@app.delete("/games/{game_id}")
async def delete_game(game_id: str, _: dict = Depends(admin_user)):
    await db.games.delete_one({"_id": oid(game_id)})
    return {"ok": True}


# ---------- play sessions ----------
@app.post("/sessions/start/{game_id}")
async def start_session(game_id: str, user: dict = Depends(current_user)):
    game = await db.games.find_one({"_id": oid(game_id)})
    if not game:
        raise HTTPException(404, "Game not found")

    # one active session per user
    existing = await db.sessions.find_one({"user_id": str(user["_id"]), "status": "running"})
    if existing:
        raise HTTPException(409, "You already have a running session")

    # capacity check
    if await db.sessions.count_documents({"status": "running"}) >= MAX_CONCURRENT:
        raise HTTPException(503, "All gaming slots are busy, try again soon")

    session_id = str(ObjectId())
    stream_token = secrets.token_urlsafe(24)
    try:
        if DEMO_MODE:
            info = {"container_id": "demo", "host_port": 0}
        else:
            info = await sm.start_container(session_id, str(user["_id"]), game, stream_token)
    except Exception as e:
        raise HTTPException(500, f"Could not start game: {e}")

    now = utcnow()
    await db.sessions.insert_one({
        "_id": ObjectId(session_id),
        "user_id": str(user["_id"]),
        "game_id": str(game["_id"]),
        "game_title": game["title"],
        "container_id": info["container_id"],
        "host_port": info["host_port"],
        "stream_token": stream_token,
        "status": "running",
        "started_at": now,
        "last_heartbeat": now,
    })
    await db.games.update_one({"_id": game["_id"]}, {"$inc": {"play_count": 1}})

    return {
        "session_id": session_id,
        "stream_url": stream_url_for(info["host_port"], game["title"]),
        "stream_password": stream_token,
    }


async def end_session(session: dict, reason: str):
    if session["container_id"] != "demo":
        await sm.stop_container(session["container_id"])
    now = utcnow()
    seconds = int((now - session["started_at"]).total_seconds())
    await db.sessions.update_one(
        {"_id": session["_id"]},
        {"$set": {"status": "ended", "ended_at": now, "end_reason": reason, "duration_seconds": seconds}},
    )
    await db.users.update_one(
        {"_id": ObjectId(session["user_id"])}, {"$inc": {"total_playtime_seconds": seconds}}
    )


@app.get("/sessions/active")
async def active_session(user: dict = Depends(current_user)):
    s = await db.sessions.find_one({"user_id": str(user["_id"]), "status": "running"})
    if not s:
        return {"active": False}
    return {
        "active": True,
        "session_id": str(s["_id"]),
        "game_title": s["game_title"],
        "stream_url": stream_url_for(s["host_port"], s["game_title"]),
        "stream_password": s["stream_token"],
    }


@app.post("/sessions/{session_id}/heartbeat")
async def heartbeat(session_id: str, user: dict = Depends(current_user)):
    res = await db.sessions.update_one(
        {"_id": oid(session_id), "user_id": str(user["_id"]), "status": "running"},
        {"$set": {"last_heartbeat": utcnow()}},
    )
    if not res.matched_count:
        raise HTTPException(404, "No running session")
    return {"ok": True}


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, user: dict = Depends(current_user)):
    session = await db.sessions.find_one(
        {"_id": oid(session_id), "user_id": str(user["_id"]), "status": "running"}
    )
    if not session:
        raise HTTPException(404, "No running session")
    await end_session(session, "user_stopped")
    return {"ok": True}


@app.get("/sessions/history")
async def history(user: dict = Depends(current_user), limit: int = 20):
    cursor = db.sessions.find({"user_id": str(user["_id"])}).sort("started_at", -1).limit(limit)
    return [clean(s) async for s in cursor]


# ---------- background cleanup ----------
async def reaper_loop():
    """Stops sessions whose browser tab closed (no heartbeat) or ran too long."""
    while True:
        await asyncio.sleep(20)
        try:
            now = utcnow()
            stale = now - timedelta(seconds=HEARTBEAT_TIMEOUT)
            too_old = now - timedelta(minutes=MAX_SESSION_MINUTES)
            async for s in db.sessions.find(
                {"status": "running", "$or": [{"last_heartbeat": {"$lt": stale}}, {"started_at": {"$lt": too_old}}]}
            ):
                reason = "timeout" if s["started_at"] < too_old else "disconnected"
                await end_session(s, reason)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("reaper error:", e)


@app.get("/config")
async def config():
    """Lets the login page show the demo account (only when DEMO_MODE=1)."""
    return {"demo": DEMO_MODE, "email": DEMO_EMAIL if DEMO_MODE else "", "password": DEMO_PASSWORD if DEMO_MODE else ""}


@app.get("/health")
async def health():
    return {"ok": True}


# ---------- web frontend (must stay LAST so it doesn't shadow the API routes) ----------
app.mount(
    "/",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True),
    name="static",
)
