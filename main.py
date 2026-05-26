import os
import httpx
import spotipy
import hashlib
import datetime
from typing import Optional
import models
from fastapi import FastAPI, Depends, Request, Query
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
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
            t_params = {"method": "track.gettoptags", "artist": artist, "track": track, "api_key": api_key,
                        "format": "json"}
            r = await client.get(url, params=t_params)
            tags = r.json().get("toptags", {}).get("tag", [])
            if not tags:
                a_params = {"method": "artist.gettoptags", "artist": artist, "api_key": api_key, "format": "json"}
                r_a = await client.get(url, params=a_params)
                tags = r_a.json().get("toptags", {}).get("tag", [])
            res["genres"] = ", ".join([t['name'] for t in tags[:5]])
            i_params = {"method": "track.getInfo", "artist": artist, "track": track, "api_key": api_key,
                        "format": "json"}
            r_i = await client.get(url, params=i_params)
            res["listeners"] = int(r_i.json().get("track", {}).get("listeners", 0))
        except:
            pass
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

    user = db.query(models.User).first()
    if not user:
        user = models.User(spotify_id=user_info['id'], display_name=user_info['display_name'])
        db.add(user)
    else:
        user.spotify_id = user_info['id']
        user.display_name = user_info['display_name']
    user.access_token = token_info['access_token']
    db.commit()
    return RedirectResponse(url="/dashboard")


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
            if not user:
                user = models.User(spotify_id=f"lfm_{data['session']['name']}", display_name=data['session']['name'])
                db.add(user)
            user.lastfm_username = data["session"]["name"]
            user.lastfm_session = data["session"]["key"]
            db.commit()
    return RedirectResponse(url="/dashboard")


# 🟢 СИНХРОНІЗАЦІЯ SPOTIFY З СИНХРОННИМ MD5-ХЕШУВАННЯМ ТРЕКІВ
@app.get("/sync")
async def sync_data(db: Session = Depends(get_db)):
    user = db.query(models.User).first()
    if not user or not user.access_token: return RedirectResponse(url="/dashboard")
    sp = spotipy.Spotify(auth=user.access_token)
    try:
        recent = sp.current_user_recently_played(limit=40)
    except:
        return RedirectResponse(url="/dashboard")

    for item in recent['items']:
        t = item['track']
        title_n, artist_n = t['name'], t['artists'][0]['name']
        played_at = item['played_at']

        # Генерація уніфікованого стабільного ID для уникнення дублів зі стрімінгами
        track_hash = hashlib.md5(f"{title_n.lower().strip()}{artist_n.lower().strip()}".encode('utf-8')).hexdigest()
        unified_id = f"id_{track_hash}"

        db_track = db.query(models.Track).filter(models.Track.spotify_id == unified_id).first()
        if not db_track:
            lfm = await get_enhanced_lastfm_data(artist_n, title_n)
            db_track = models.Track(spotify_id=unified_id, title=title_n, artist=artist_n, genres=lfm["genres"],
                                    global_listeners=lfm["listeners"])
            db.add(db_track);
            db.commit();
            db.refresh(db_track)

        p_at = datetime.datetime.strptime(played_at, "%Y-%m-%dT%H:%M:%S.%fZ")
        exists = db.query(models.UserTrack).filter(models.UserTrack.played_at == p_at,
                                                   models.UserTrack.track_id == db_track.id).first()
        if not exists:
            db.add(models.UserTrack(user_id=user.id, track_id=db_track.id, played_at=p_at))

    db.commit()
    return RedirectResponse(url="/dashboard")


# 🔴 СИНХРОНІЗАЦІЯ LAST.FM ЗА ОБРАНИМ ЧАСОВИМ ДІАПАЗОНОМ (BIG DATA IMPORT)
# 🔴 ОНОВЛЕНИЙ ІМПОРТ LAST.FM (БЕЗ ДУБЛІВ, З АВТО-ЛІМІТОМ ДО 200 ТРЕКІВ)
@app.get("/sync/history")
async def sync_history(import_from: Optional[str] = None, import_to: Optional[str] = None,
                       db: Session = Depends(get_db)):
    user = db.query(models.User).first()
    # Якщо користувач розлогінився в Last.fm, примусово штовхаємо його на авторизацію
    if not user or not user.lastfm_username:
        return RedirectResponse(url="/login/lastfm")

    api_key = os.getenv("LASTFM_API_KEY")
    # Базовий URL з лімітом 200 треків згідно з ТЗ ККП
    url = f"http://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user={user.lastfm_username}&api_key={api_key}&format=json&limit=200"

    # Якщо дати передані — конвертуємо в UNIX Timestamp для API Last.fm
    if import_from and import_from != "":
        ts_from = int(datetime.datetime.strptime(import_from, "%Y-%m-%d").timestamp())
        url += f"&from={ts_from}"
    if import_to and import_to != "":
        ts_to = int((datetime.datetime.strptime(import_to, "%Y-%m-%d") + datetime.timedelta(days=1)).timestamp())
        url += f"&to={ts_to}"

    async with httpx.AsyncClient() as client:
        res = await client.get(url, timeout=10.0)
        if res.status_code == 200:
            data = res.json()
            tracks = data.get("recenttracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]

            for t in tracks:
                if t.get("@attr", {}).get("nowplaying") == "true":
                    continue
                title_n = t.get("name")
                artist_n = t.get("artist", {}).get("#text")
                uts = t.get("date", {}).get("uts")

                if title_n and artist_n and uts:
                    p_at = datetime.datetime.fromtimestamp(int(uts))

                    # ГЕНЕРАЦІЯ ЄДИНОГО СТАБІЛЬНОГО ID (Запобігає дублюванню на 100%)
                    track_hash = hashlib.md5(
                        f"{title_n.lower().strip()}{artist_n.lower().strip()}".encode('utf-8')).hexdigest()
                    unified_id = f"id_{track_hash}"

                    db_track = db.query(models.Track).filter(models.Track.spotify_id == unified_id).first()
                    if not db_track:
                        lfm = await get_enhanced_lastfm_data(artist_n, title_n)
                        db_track = models.Track(
                            spotify_id=unified_id,
                            title=title_n,
                            artist=artist_n,
                            genres=lfm["genres"],
                            global_listeners=lfm["listeners"]
                        )
                        db.add(db_track)
                        db.commit()
                        db.refresh(db_track)

                    # Захист від дублів самого факту прослуховування
                    exists = db.query(models.UserTrack).filter(
                        models.UserTrack.user_id == user.id,
                        models.UserTrack.track_id == db_track.id,
                        models.UserTrack.played_at == p_at
                    ).first()

                    if not exists:
                        db.add(models.UserTrack(user_id=user.id, track_id=db_track.id, played_at=p_at))

    db.commit()
    return RedirectResponse(url="/dashboard")
# 📊 ОБРАХУНОК ВСЬОГО ДАШБОРДУ З ГНУЧКИМИ ЧАСОВИМИ ЗРІЗАМИ
@app.get("/dashboard")
async def dashboard(request: Request, start_date: Optional[str] = None, end_date: Optional[str] = None,
                    db: Session = Depends(get_db)):
    user = db.query(models.User).first()
    if not user: return RedirectResponse(url="/")

    start_dt, end_dt = None, None
    if start_date and start_date != "":
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    if end_date and end_date != "":
        end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d") + datetime.timedelta(days=1) - datetime.timedelta(
            seconds=1)

    # Топ Жанрів
    genres_query = db.query(models.Track.genres).join(models.UserTrack).filter(models.UserTrack.user_id == user.id)
    if start_dt: genres_query = genres_query.filter(models.UserTrack.played_at >= start_dt)
    if end_dt: genres_query = genres_query.filter(models.UserTrack.played_at <= end_dt)

    genres_data = genres_query.all()
    genre_counts = {}
    for row in genres_data:
        if row[0]:
            for g in str(row[0]).split(", "):
                if g not in ["unknown", ""]: genre_counts[g] = genre_counts.get(g, 0) + 1
    sorted_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:15]

    # Глобальні гіганти
    mainstream_query = db.query(models.Track.artist, func.max(models.Track.global_listeners)).join(
        models.UserTrack).filter(models.UserTrack.user_id == user.id, models.Track.global_listeners > 100000)
    if start_dt: mainstream_query = mainstream_query.filter(models.UserTrack.played_at >= start_dt)
    if end_dt: mainstream_query = mainstream_query.filter(models.UserTrack.played_at <= end_dt)
    mainstream = mainstream_query.group_by(models.Track.artist).order_by(
        func.max(models.Track.global_listeners).desc()).limit(5).all()

    # Топ Андеграунд
    underground_query = db.query(models.Track.title, models.Track.artist, models.Track.global_listeners,
                                 func.count(models.UserTrack.id).label("plays")).join(models.UserTrack).filter(
        models.UserTrack.user_id == user.id, models.Track.global_listeners > 0, models.Track.global_listeners < 50000)
    if start_dt: underground_query = underground_query.filter(models.UserTrack.played_at >= start_dt)
    if end_dt: underground_query = underground_query.filter(models.UserTrack.played_at <= end_dt)
    underground = underground_query.group_by(models.Track.id, models.Track.title, models.Track.artist,
                                             models.Track.global_listeners).order_by(
        func.count(models.UserTrack.id).desc()).limit(5).all()

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "genres": sorted_genres, "top_artists": mainstream,
        "underground_tracks": underground,
        "start_date": start_date or "", "end_date": end_date or ""
    })


@app.get("/logout")
def logout(): return RedirectResponse(url="/")


@app.get("/clear")
def clear_all(db: Session = Depends(get_db)):
    db.query(models.UserTrack).delete();
    db.query(models.User).delete();
    db.commit()
    return RedirectResponse(url="/")