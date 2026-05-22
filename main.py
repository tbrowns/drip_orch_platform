from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

import os
from dotenv import load_dotenv

from db.models import init_db, StockQuote, User, UserKYC
from core.security import (
    create_access_token,
    verify_token,
    hash_password,
    verify_password,
    oauth2_scheme,
)

app = FastAPI()

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_, session_factory = init_db(DATABASE_URL)


class UserCreate(BaseModel):
    full_name: str
    username: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    full_name: str
    username: str
    email: str
    

    class Config:
        from_attributes = True


def get_current_user(token: str = Depends(oauth2_scheme)):
    token_data = verify_token(token)

    with session_factory() as session:
        user = (
            session.query(User)
            .filter(User.id == token_data["user_id"])
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )

        return user


@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.post("/auth/signup")
def signup(user: UserCreate):
    with session_factory() as session:
        existing_email = (
            session.query(User)
            .filter(User.email == user.email)
            .first()
        )

        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        existing_id = (
            session.query(User)
            .filter(User.id_number == user.id_number)
            .first()
        )

        if existing_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ID number already registered"
            )

        new_user = User(
            full_name=user.full_name,
            username=user.username,
            email=user.email,
            password_hash=hash_password(user.password),
            
        )

        session.add(new_user)
        session.commit()
        session.refresh(new_user)

        return {
            "message": "User signed up successfully",
            "user": {
                "id": new_user.id,
                "full_name": new_user.full_name,
                "email": new_user.email,
                "username": new_user.username
            }
        }

@app.post("/auth/login")
def login(user: UserLogin):
    with session_factory() as session:
        existing_user = (
            session.query(User)
            .filter(User.email == user.email)
            .first()
        )

        if not existing_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        password_is_valid = verify_password(
            user.password,
            existing_user.password_hash
        )

        if not password_is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        access_token = create_access_token(
            data={"user_id": existing_user.id}
        )

        return {
            "message": "User logged in successfully",
            "access_token": access_token,
            "token_type": "bearer"
        }

@app.get("/users/me")
def read_me(current_user: User = Depends(get_current_user)):
    return current_user

@app.get("/kyc/me")
def get_current_kyc(current_user: User = Depends(get_current_user)):
    with session_factory() as session:
        current_kyc = (
            session.query(UserKYC)
            .filter(UserKYC.user_id == current_user.id)
            .first()
        )

        if not current_kyc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="KYC information not found"
            )

        return {
            "id": current_kyc.id,
            "user_id": current_kyc.user_id,
            "id_number": current_kyc.id_number,
            "kra_pin": current_kyc.kra_pin,
            "verification_status": current_kyc.verification_status
        }

@app.get("/quotes")
def get_quotes_from_db():
    with session_factory() as session:
        quotes = session.query(
            StockQuote.ticker,
            StockQuote.name,
            StockQuote.price,
            StockQuote.change_pct,
            StockQuote.volume
        ).all()
        return {
            "Quotes": [
                {
                    "ticker": q[0],
                    "name": q[1],
                    "price": q[2],
                    "change_pct": q[3],
                    "volume": q[4]
                }
                for q in quotes
            ]
        }

@app.get("/all-quotes")
def get_all_quotes():
    with session_factory() as session:
        quotes = session.query(StockQuote).all()
        return {"All Quotes": quotes}