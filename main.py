import os
import httpx
import spotipy
import hashlib
import datetime
import models
from fastapi import FastAPI, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from database import engine, get_db

load_dotenv()
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="MusicAnalytics System")
templates = Jinja2Templates(directory="templates")

sp_oauth = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri="http://127.0.0.1:8000/callback",
    scope="user-read-recently-played user-top-read"
)

def get_lfm_signature(params, secret):
    keys = sorted(params.keys())
    sig_str = "".join([f"{k}{params[k]}" for k in keys if k != 'format'])
    sig_str += secret
    return hashlib.md5(sig_str.encode('utf-8')).hexdigest()

async def get_enhanced_lastfm_data(artist: str, track: str):
    api_key = os.getenv("LASTFM_API_KEY")
    url = "http://ws.audioscrobbler.com/2.0/"
    res = {"genres": "unknown", "listeners": 0}
    async with httpx.AsyncClient() as client:
        try:
            t_params = {"method": "track.gettoptags", "artist": artist, "track": track, "api_key": api_key, "format": "json"}
            r = await client.get(url, params=t_params)
            tags = r.json().get("toptags", {}).get("tag", [])
            if not tags:
                a_params = {"method": "artist.gettoptags", "artist": artist, "api_key": api_key, "format": "json"}
                r_a = await client.get(url, params=a_params)
                tags = r_a.json().get("toptags", {}).get("tag", [])
            res["genres"] = ", ".join([t['name'] for t in tags[:5]])
            i_params = {"method": "track.getInfo", "artist": artist, "track": track, "api_key": api_key, "format": "json"}
            r_i = await client.get(url, params=i_params)
            res["listeners"] = int(r_i.json().get("track", {}).get("listeners", 0))
        except: pass
    return res

@app.get("/")
async def landing(request: Request, db: Session = Depends(get_db)):
    user = db.query(models.User).first()
    return templates.TemplateResponse("landing.html", {"request": request, "user": user})

@app.get("/login")
def login_spotify():
    return RedirectResponse(sp_oauth.get_authorize_url())

@app.get("/callback")
async def spotify_callback(code: str, db: Session = Depends(get_db)):
    token_info = sp_oauth.get_access_token(code)
    sp = spotipy.Spotify(auth=token_info['access_token'])
    user_info = sp.current_user()
    user = db.query(models.User).filter(models.User.spotify_id == user_info['id']).first()
    if not user:
        user = models.User(spotify_id=user_info['id'], display_name=user_info['display_name'])
        db.add(user)
    user.access_token = token_info['access_token']
    db.commit()
    await sync_data(db)
    return RedirectResponse(url="/")

@app.get("/login/lastfm")
def login_lastfm():
    api_key = os.getenv("LASTFM_API_KEY")
    cb = "http://127.0.0.1:8000/callback/lastfm"
    return RedirectResponse(f"http://www.last.fm/api/auth/?api_key={api_key}&cb={cb}")

@app.get("/callback/lastfm")
async def lastfm_callback(token: str, db: Session = Depends(get_db)):
    params = {"api_key": os.getenv("LASTFM_API_KEY"), "method": "auth.getSession", "token": token}
    params["api_sig"] = get_lfm_signature(params, os.getenv("LASTFM_SECRET"))
    params["format"] = "json"
    async with httpx.AsyncClient() as client:
        r = await client.get("http://ws.audioscrobbler.com/2.0/", params=params)
        data = r.json()
        if "session" in data:
            user = db.query(models.User).first()
            user.lastfm_username = data["session"]["name"]
            user.lastfm_session = data["session"]["key"]
            db.commit()
    return RedirectResponse(url="/")

@app.get("/sync")
async def sync_data(db: Session = Depends(get_db)):
    user = db.query(models.User).first()
    if not user: return {"error": "No user"}
    sp = spotipy.Spotify(auth=user.access_token)
    recent = sp.current_user_recently_played(limit=40)
    for item in recent['items']:
        t = item['track']
        sid, played_at = t['id'], item['played_at']
        db_track = db.query(models.Track).filter(models.Track.spotify_id == sid).first()
        if not db_track:
            lfm = await get_enhanced_lastfm_data(t['artists'][0]['name'], t['name'])
            db_track = models.Track(spotify_id=sid, title=t['name'], artist=t['artists'][0]['name'], genres=lfm["genres"], global_listeners=lfm["listeners"])
            db.add(db_track); db.commit(); db.refresh(db_track)
        exists = db.query(models.UserTrack).filter(models.UserTrack.played_at == played_at, models.UserTrack.track_id == db_track.id).first()
        if not exists:
            db.add(models.UserTrack(user_id=user.id, track_id=db_track.id, played_at=played_at))
    db.commit()
    return {"status": "success"}


@app.get("/sync/history")
async def sync_full_history(db: Session = Depends(get_db)):
    """Викачування до 200 останніх треків з Last.fm для наповнення бази"""
    user = db.query(models.User).first()
    if not user or not user.lastfm_username:
        return {"error": "Спочатку підключи Last.fm"}

    api_key = os.getenv("LASTFM_API_KEY")
    # Параметри для запиту останніх прослуховувань
    params = {
        "method": "user.getrecenttracks",
        "user": user.lastfm_username,
        "api_key": api_key,
        "format": "json",
        "limit": 200
    }

    async with httpx.AsyncClient() as client:
        r = await client.get("http://ws.audioscrobbler.com/2.0/", params=params)
        data = r.json()
        tracks_data = data.get("recenttracks", {}).get("track", [])

        for t in tracks_data:
            artist_n = t['artist']['#text']
            title_n = t['name']

            # Перевіряємо, чи є такий трек у базі
            db_track = db.query(models.Track).filter(
                models.Track.title == title_n,
                models.Track.artist == artist_n
            ).first()

            if not db_track:
                # Якщо треку немає, підтягуємо дані (слухачі, жанри)
                lfm = await get_enhanced_lastfm_data(artist_n, title_n)
                db_track = models.Track(
                    spotify_id=f"lfm_{hash(title_n + artist_n)}",
                    title=title_n,
                    artist=artist_n,
                    genres=lfm["genres"],
                    global_listeners=lfm["listeners"]
                )
                db.add(db_track)
                db.commit()
                db.refresh(db_track)

            # Додаємо запис про прослуховування, якщо є дата
            ts = t.get('date', {}).get('uts')
            if ts:
                p_at = datetime.datetime.fromtimestamp(int(ts))
                exists = db.query(models.UserTrack).filter(
                    models.UserTrack.played_at == p_at,
                    models.UserTrack.track_id == db_track.id
                ).first()
                if not exists:
                    db.add(models.UserTrack(user_id=user.id, track_id=db_track.id, played_at=p_at))

    db.commit()
    return {"status": "success"}

@app.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = db.query(models.User).first()
    if not user: return RedirectResponse(url="/")
    genres_data = db.query(models.Track.genres).all()
    genre_counts = {}
    for row in genres_data:
        if row.genres:
            for g in row.genres.split(", "):
                if g not in ["unknown", ""]: genre_counts[g] = genre_counts.get(g, 0) + 1
    sorted_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    mainstream = db.query(models.Track.artist, func.max(models.Track.global_listeners)).filter(models.Track.global_listeners > 100000).group_by(models.Track.artist).order_by(func.max(models.Track.global_listeners).desc()).limit(5).all()
    underground = db.query(models.Track.title, models.Track.artist, models.Track.global_listeners, func.count(models.UserTrack.id)).join(models.UserTrack).filter(models.Track.global_listeners > 0, models.Track.global_listeners < 50000).group_by(models.Track.id).order_by(func.count(models.UserTrack.id).desc()).limit(5).all()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "genres": sorted_genres, "top_artists": mainstream, "underground_tracks": underground})