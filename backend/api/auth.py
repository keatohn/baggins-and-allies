"""
Auth helpers: password hashing and JWT.
Bcrypt accepts at most 72 bytes; we truncate manually (e.g. my_password.encode("utf-8")[:72]) before hashing.
We use bcrypt directly so the truncated bytes are passed through with no extra encoding.
"""

import bcrypt
import os
import re
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .database import get_db
from .models import Player

# Username: alphanumeric and underscore only, 2–32 chars
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{2,32}$")

SECRET_KEY = os.environ.get("JWT_SECRET", "change-me-in-production-use-env")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

BCRYPT_MAX_BYTES = 72
# Lower rounds = faster register/login; 10 is still strong and ~instant
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "10"))
security = HTTPBearer(auto_error=False)


def _truncate_password(password: str) -> bytes:
    """Truncate to 72 bytes so bcrypt never raises. E.g. my_password[:72] in bytes: password.encode('utf-8')[:72]."""
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    pwd_bytes = _truncate_password(password)
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    pwd_bytes = _truncate_password(plain)
    return bcrypt.checkpw(pwd_bytes, hashed.encode("ascii"))


def create_access_token(player_id: str) -> str:
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {"sub": player_id, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


def validate_username(username: str) -> bool:
    return bool(USERNAME_PATTERN.match(username))


def get_current_player(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> Player:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    player_id = decode_token(credentials.credentials)
    if not player_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    player = db.query(Player).filter(Player.id == player_id).first()
    if not player:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Player not found")
    return player


def get_current_player_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> Player | None:
    if not credentials:
        return None
    player_id = decode_token(credentials.credentials)
    if not player_id:
        return None
    return db.query(Player).filter(Player.id == player_id).first()
