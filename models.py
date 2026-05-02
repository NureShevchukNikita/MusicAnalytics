from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
import datetime


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    spotify_id = Column(String(255), unique=True, index=True)
    display_name = Column(String(255))
    access_token = Column(String(500))
    refresh_token = Column(String(500))
    lastfm_username = Column(String(255), nullable=True)
    lastfm_session = Column(String(255), nullable=True)
    # Зв'язок з треками (один користувач - багато прослуховувань)
    tracks = relationship("UserTrack", back_populates="user")


class Track(Base):
    __tablename__ = "tracks"
    id = Column(Integer, primary_key=True, index=True)
    spotify_id = Column(String(255), unique=True)
    title = Column(String(255))
    artist = Column(String(255))
    genres = Column(String(500))
    global_listeners = Column(Integer, default=0)
    global_playcount = Column(Integer, default=0)
    artist_bio = Column(String(1000), nullable=True)
    energy = Column(Float, default=0.5)
    valence = Column(Float, default=0.5)

class UserTrack(Base):
    __tablename__ = "user_tracks"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    track_id = Column(Integer, ForeignKey("tracks.id"))
    played_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="tracks")