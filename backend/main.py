"""Asterion Sound: local-first music/project API; ACE-Step performs actual synthesis."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Asterion Sound", version="0.1.0")

def root() -> Path:
    path = Path(os.environ.get("ASTERION_SOUND_DATA", "./sound-data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path

def database() -> sqlite3.Connection:
    con = sqlite3.connect(root() / "sound.sqlite3", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS artists (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, music_style TEXT NOT NULL,
          lyric_style TEXT NOT NULL DEFAULT '', vocal_style TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tracks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, artist_id TEXT,
          parent_id TEXT, title TEXT NOT NULL, prompt TEXT NOT NULL,
          lyrics TEXT NOT NULL, language TEXT NOT NULL, instrumental INTEGER NOT NULL,
          duration INTEGER NOT NULL, format TEXT NOT NULL,
          engine_task_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
          file_path TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id));
    """)
    con.commit()
    return con

def require_auth(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("ASTERION_SOUND_TOKEN", "")
    if not token:
        raise HTTPException(503, "Set ASTERION_SOUND_TOKEN before accepting requests")
    if authorization != "Bearer " + token:
        raise HTTPException(401, "Missing or invalid bearer token")

class ArtistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    music_style: str = Field(min_length=1, max_length=2000)
    lyric_style: str = Field(default="", max_length=2000)
    vocal_style: str = Field(default="", max_length=2000)

class ProjectRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

class GenerateRequest(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=3, max_length=5000)
    lyrics: str = Field(default="", max_length=15000)
    language: Literal["ja", "en", "ko", "zh"] = "ja"
    instrumental: bool = False
    duration: int = Field(default=120, ge=10, le=600)
    format: Literal["mp3", "wav", "flac"] = "mp3"
    artist_id: str | None = None
    parent_id: str | None = None
    bpm: int | None = Field(default=None, ge=30, le=300)

class AceStep:
    def __init__(self) -> None:
        self.url = os.environ.get("ACESTEP_URL", "http://127.0.0.1:8001").rstrip("/")
        self.key = os.environ.get("ACESTEP_API_KEY", "")

    async def _call(self, method: str, path: str, *, payload=None) -> httpx.Response:
        headers = {"Authorization": "Bearer " + self.key} if self.key else {}
        try:
            async with httpx.AsyncClient(timeout=125, trust_env=False, headers=headers) as client:
                response = await client.request(method, self.url + path, json=payload)
                response.raise_for_status()
                return response
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f"ACE-Step connection failed: {exc}") from exc

    async def health(self) -> bool:
        try:
            result = await self._call("GET", "/health")
            return result.status_code == 200
        except HTTPException:
            return False

    async def submit(self, payload: dict) -> str:
        response = (await self._call("POST", "/release_task", payload=payload)).json()
        if response.get("code") != 200 or not isinstance(response.get("data"), dict):
            raise HTTPException(502, "ACE-Step rejected task: " + str(response.get("error")))
        task_id = response["data"].get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(502, "ACE-Step did not return a task ID")
        return task_id

    async def status(self, task_id: str) -> dict:
        response = (await self._call(
            "POST", "/query_result", payload={"task_id_list": [task_id]}
        )).json()
        data = response.get("data")
        if response.get("code") != 200 or not isinstance(data, list) or not data:
            raise HTTPException(502, "ACE-Step returned invalid task status")
        return data[0]

    async def audio(self, relative_url: str) -> bytes:
        parsed = urlsplit(relative_url)
        if parsed.scheme or parsed.netloc or parsed.path != "/v1/audio" or not parsed.query.startswith("path="):
            raise HTTPException(502, "Invalid ACE-Step audio path")
        response = await self._call("GET", relative_url)
        if not response.content:
            raise HTTPException(502, "ACE-Step returned an empty audio file")
        return response.content

def engine() -> AceStep:
    return getattr(app.state, "engine_override", None) or AceStep()

def row_dict(row: sqlite3.Row) -> dict:
    obj = dict(row)
    if "instrumental" in obj:
        obj["instrumental"] = bool(obj["instrumental"])
    return obj

def get_row(table: Literal["artists", "projects", "tracks"], row_id: str) -> dict:
    with database() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"{table[:-1].capitalize()} not found")
    return row_dict(row)

@app.get("/health")
def health():
    return {"service": "Asterion Sound", "status": "ok", "version": "0.1.0"}

@app.get("/v1/capabilities", dependencies=[Depends(require_auth)])
async def capabilities():
    return {"music": "ace-step-adapter", "voices": "not-implemented",
            "sfx": "not-implemented", "release": "not-implemented",
            "remote_cancellation": False, "ace_step_connected": await engine().health()}

@app.post("/v1/artists", dependencies=[Depends(require_auth)], status_code=201)
def add_artist(body: ArtistRequest):
    item = {"id": uuid.uuid4().hex, **body.model_dump(), "created_at": int(time.time())}
    with database() as con:
        con.execute("INSERT INTO artists VALUES (:id, :name, :music_style, :lyric_style, :vocal_style, :created_at)", item)
    return item

@app.get("/v1/artists", dependencies=[Depends(require_auth)])
def artists():
    with database() as con:
        rows = con.execute("SELECT * FROM artists ORDER BY created_at DESC").fetchall()
    return [row_dict(r) for r in rows]

@app.post("/v1/projects", dependencies=[Depends(require_auth)], status_code=201)
def add_project(body: ProjectRequest):
    item = {"id": uuid.uuid4().hex, "title": body.title, "created_at": int(time.time())}
    with database() as con:
        con.execute("INSERT INTO projects VALUES (:id, :title, :created_at)", item)
    return item

@app.get("/v1/projects", dependencies=[Depends(require_auth)])
def projects():
    with database() as con:
        rows = con.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [row_dict(r) for r in rows]

@app.post("/v1/generate", dependencies=[Depends(require_auth)], status_code=202)
async def generate(body: GenerateRequest):
    get_row("projects", body.project_id)
    artist = get_row("artists", body.artist_id) if body.artist_id else None
    if body.parent_id:
        parent = get_row("tracks", body.parent_id)
        if parent["project_id"] != body.project_id:
            raise HTTPException(422, "A variation must belong to its parent project")
    description = body.prompt
    if artist:
        description += "\nMusic style: " + artist["music_style"]
        if artist["vocal_style"] and not body.instrumental:
            description += "\nVocal characteristics: " + artist["vocal_style"]
        if artist["lyric_style"] and not body.lyrics and not body.instrumental:
            description += "\nLyric characteristics: " + artist["lyric_style"]
    payload = {
        "prompt": description,
        "lyrics": "[Instrumental]" if body.instrumental else body.lyrics,
        "instrumental": body.instrumental,
        "thinking": True,
        "vocal_language": body.language,
        "audio_duration": body.duration,
        "audio_format": body.format,
        "task_type": "text2music",
        "use_format": False,
    }
    if body.bpm is not None:
        payload["bpm"] = body.bpm
    if not body.instrumental and not body.lyrics:
        payload["sample_query"] = description
    task_id = await engine().submit(payload)
    now = int(time.time())
    item = dict(id=uuid.uuid4().hex, project_id=body.project_id,
                artist_id=body.artist_id, parent_id=body.parent_id,
                title=body.title, prompt=body.prompt, lyrics=body.lyrics,
                language=body.language, instrumental=int(body.instrumental),
                duration=body.duration, format=body.format,
                engine_task_id=task_id, status="queued", error=None,
                file_path=None, created_at=now, updated_at=now)
    with database() as con:
        con.execute("""INSERT INTO tracks VALUES
        (:id,:project_id,:artist_id,:parent_id,:title,:prompt,:lyrics,:language,
        :instrumental,:duration,:format,:engine_task_id,:status,:error,
        :file_path,:created_at,:updated_at)""", item)
    item["instrumental"] = bool(item["instrumental"])
    return item

@app.get("/v1/tracks", dependencies=[Depends(require_auth)])
def tracks(project_id: str | None = None):
    with database() as con:
        if project_id is None:
            rows = con.execute("SELECT * FROM tracks ORDER BY created_at DESC").fetchall()
        else:
            rows = con.execute("SELECT * FROM tracks WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
    return [row_dict(r) for r in rows]

@app.get("/v1/tracks/{track_id}", dependencies=[Depends(require_auth)])
async def track_status(track_id: str):
    track = get_row("tracks", track_id)
    if track["status"] in ("completed", "failed"):
        return track
    record = await engine().status(track["engine_task_id"])
    code = record.get("status")
    if code == 0:
        status, error, file_path = "generating", None, None
    elif code == 2:
        status, error, file_path = "failed", str(record.get("result") or "Generation failed"), None
    elif code == 1:
        try:
            result = record.get("result", "[]")
            if isinstance(result, str):
                result = json.loads(result)
            file_url = result[0]["file"]
            audio = await engine().audio(file_url)
            extension = track["format"]
            destination = root() / "tracks" / f"{track_id}.{extension}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_suffix("." + extension + ".tmp")
            temp.write_bytes(audio)
            temp.replace(destination)
            status, error, file_path = "completed", None, str(destination)
        except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            status, error, file_path = "failed", "Failed to retrieve generated audio: " + str(exc), None
    else:
        raise HTTPException(502, "Unknown ACE-Step task status")
    with database() as con:
        con.execute("UPDATE tracks SET status=?, error=?, file_path=?, updated_at=? WHERE id=?",
                    (status, error, file_path, int(time.time()), track_id))
    return get_row("tracks", track_id)

@app.get("/v1/tracks/{track_id}/audio", dependencies=[Depends(require_auth)])
def get_audio(track_id: str):
    track = get_row("tracks", track_id)
    if track["status"] != "completed" or not track["file_path"]:
        raise HTTPException(409, "Audio not available; poll track status first")
    path = Path(track["file_path"])
    expected = root() / "tracks" / f"{track_id}.{track['format']}"
    if path != expected or not path.is_file():
        raise HTTPException(404, "Track audio file is missing")
    return FileResponse(str(path), media_type="audio/" + ("mpeg" if track["format"] == "mp3" else track["format"]),
                        filename=f"{track['title']}.{track['format']}")

@app.post("/v1/tracks/{track_id}/cancel", dependencies=[Depends(require_auth)])
def cancel(track_id: str):
    track = get_row("tracks", track_id)
    if track["status"] in ("completed", "failed"):
        raise HTTPException(409, "This task already finished")
    raise HTTPException(409, "ACE-Step remote task cancellation is not exposed by its documented API; no cancellation was claimed")
