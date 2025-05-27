from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

from db.base import Base
import db.models  # Import to register models with Base

# Import event models to register them with Base metadata
# Import individual modules to avoid executing package __init__ 
from events.models.EventModel import EventModel
from events.models.EventConfigModel import EventConfigModel
from events.models.EventTeamModel import EventTeamModel
from events.models.EventParticipant import EventParticipant
from events.models.EventShopItem import EventShopItem
from events.models.EventTeamInventory import EventTeamInventory
from events.models.EventTeamCooldown import EventTeamCooldown
from events.models.EventTeamEffect import EventTeamEffect
from events.models.tasks.EventTask import EventTask
from events.models.types.BoardGame import BoardGameModel

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
print("Alembic connection string:", config.get_main_option("sqlalchemy.url"))
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
