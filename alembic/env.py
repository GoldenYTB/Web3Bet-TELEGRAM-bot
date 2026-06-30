import asyncio
import os
import sys
import ssl
from logging.config import fileConfig
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaming_bot.models import Base

config = context.config

# Get URL and clean it up for asyncpg
database_url = os.environ.get("DATABASE_URL", "")

# Fix scheme
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif database_url.startswith("postgresql://") and "+asyncpg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# Strip sslmode and ssl query params — asyncpg handles SSL via connect_args
parsed = urlparse(database_url)
params = parse_qs(parsed.query)
params.pop("sslmode", None)
params.pop("ssl", None)
clean_query = urlencode({k: v[0] for k, v in params.items()})
clean_url = urlunparse(parsed._replace(query=clean_query))

config.set_main_option("sqlalchemy.url", clean_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=clean_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    ssl_ctx = ssl.create_default_context()
    engine = create_async_engine(
        clean_url,
        poolclass=pool.NullPool,
        connect_args={"ssl": ssl_ctx},
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
