from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class TrackBase(BaseModel):
    title: str
    artist: str
    spotify_id: str
    energy: Optional[float] = None
    valence: Optional[float] = None

class TrackCreate(TrackBase):
    pass

class Track(TrackBase):
    id: int
    class Config:
        from_attributes = True

class UserBase(BaseModel):
    display_name: str
    spotify_id: str

class User(UserBase):
    id: int
    class Config:
        from_attributes = True