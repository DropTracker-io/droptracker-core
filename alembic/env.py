from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

from db import Base
import db.models  # Import to register models with Base


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


def _mask_db_url(url: str) -> str:
    """Redact the password in a SQLAlchemy URL before logging it.

    alembic runs on every deploy and its output is captured to logs; printing
    the raw sqlalchemy.url leaks the DB (root) password. Mask the credential
    while keeping the host/db visible for diagnostics.
    """
    if not url:
        return url
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        # Fall back to a coarse regex mask if the URL can't be parsed.
        import re

        return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", url)


print("Alembic connection string:", _mask_db_url(config.get_main_option("sqlalchemy.url")))
# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

def include_object(object, name, type_, reflected, compare_to):
    """
    Decide whether to include an object in the autogenerate process.
    
    Return True to include the object, False to exclude it.
    """
    # Exclude tables that don't have models but exist in the database
    if type_ == "table" and object.schema == "data":
        excluded_tables = [
            'patreon_notification',
            'event_team_tasks',
            'migrations',
            'sessions',
            # Add any other tables you want to exclude
        ]
        if name in excluded_tables:
            return False
    
    # Exclude indexes on tables we're ignoring
    if type_ == "index" and object.table.name == "sessions":
        return False
    
    return True

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
