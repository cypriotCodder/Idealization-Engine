"""Database connection helpers. The URL comes from DATABASE_URL in `.env`."""

import os
from typing import Any

import psycopg
from dotenv import load_dotenv

type Connection = psycopg.Connection[tuple[Any, ...]]


def database_url() -> str:
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env and fill it in")
    return url


def connect() -> Connection:
    return psycopg.connect(database_url())
