"""The real ORM models, checked without a database.

Unit tests import the real ``db.models`` (see tests/conftest.py), so a model
that is broken in a way only SQLAlchemy notices (a relationship naming a
missing class, a column type MySQL can't render) fails here in CI rather than
when a service first loads it.
"""
import importlib
import pkgutil
from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

import db.models
from db.models.base import Base

# Every model module, including the few db/models/__init__.py doesn't import
# (their users import them directly), so the checks below see all of them.
for _mod in pkgutil.iter_modules([str(Path(db.models.__file__).parent)]):
    importlib.import_module(f"db.models.{_mod.name}")


def test_every_mapper_configures():
    # Resolves every relationship() / backref across all models at once.
    configure_mappers()


def test_every_table_renders_mysql_ddl():
    dialect = mysql.dialect()
    for table in Base.metadata.sorted_tables:
        CreateTable(table).compile(dialect=dialect)
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_importing_the_models_opened_no_connection():
    """Model modules must not touch the database at import time (drop_split
    used to run CREATE TABLE on import, which is what kept the suite on a
    mocked db.models)."""
    from db.models.base import engine, xenforo_engine

    for eng in (engine, xenforo_engine):
        assert eng.pool.checkedout() == 0
        assert eng.pool.checkedin() == 0
