import os
from datetime import datetime, timedelta, UTC

from dotenv import load_dotenv
from jose import JWTError, jwt
import bcrypt
from fastapi import HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer


load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is missing from environment variables")


# bcrypt is used directly rather than through passlib's CryptContext.
# passlib 1.7.4 probes its bcrypt backend on first use with a 255-byte test
# secret; bcrypt >= 4.1 raises ValueError on secrets over 72 bytes instead of
# truncating, so the backend never initialises and every hash call blows up.
# Calling bcrypt directly produces the exact same $2b$ hashes, so existing
# passlib-generated hashes keep verifying.
BCRYPT_MAX_BYTES = 72


def _to_bcrypt_bytes(password: str) -> bytes:
    """
    Encode a password for bcrypt, which hashes at most 72 bytes and rejects
    anything longer. Truncation is on bytes, not characters, which is what
    bcrypt itself operates on.
    """
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    """
    Hash plain password before saving to database.
    """
    return bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Compare plain password from login with hashed password in DB.
    """
    try:
        return bcrypt.checkpw(
            _to_bcrypt_bytes(plain_password),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Malformed or non-bcrypt hash in the column -- treat as a failed login
        # rather than a 500.
        return False


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Create JWT access token.
    """
    to_encode = data.copy()

    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(
        to_encode,
        SECRET_KEY,
        algorithm=ALGORITHM
    )

    return encoded_jwt


def verify_token(token: str) -> dict:
    """
    Decode and validate JWT token.
    """
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        user_id = payload.get("user_id")

        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )

        return {"user_id": user_id}

    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )