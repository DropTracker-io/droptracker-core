from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
import os
from dotenv import load_dotenv
import pymysql
#from sqlalchemy.orm import relationship

pymysql.install_as_MySQLdb()
load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

# Create base class for declarative models
Base = declarative_base()

# Pools are PER PROCESS and QueuePool never releases idle connections below
# pool_size, so every process that imports this module permanently holds up to
# pool_size connections after any burst. Size for the fleet (~15 processes),
# not for one process: 50/20 defaults exhausted max_connections in production.
DATA_POOL_SIZE = int(os.getenv("DATA_DB_POOL_SIZE", "5"))
DATA_POOL_OVERFLOW = int(os.getenv("DATA_DB_MAX_OVERFLOW", "25"))
DATA_POOL_TIMEOUT = int(os.getenv("DATA_DB_POOL_TIMEOUT", "20"))

# Create engine with improved connection handling.
# IMPORTANT: in multi-worker deployments, this pool is per-process.
engine = create_engine(
    f'mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data',
    pool_size=DATA_POOL_SIZE,
    max_overflow=DATA_POOL_OVERFLOW,
    pool_timeout=DATA_POOL_TIMEOUT,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={
        'connect_timeout': 10,
        'read_timeout': 30,
        'write_timeout': 30,
        'charset': 'utf8mb4',
        'autocommit': False
    }
)

# Create session factory and scoped session (hot-swappable parity with legacy)
Session = sessionmaker(bind=engine)
session = scoped_session(Session)

# Secondary XenForo connection (parity with legacy models)
XENFORO_POOL_SIZE = int(os.getenv("XENFORO_DB_POOL_SIZE", "4"))
XENFORO_POOL_OVERFLOW = int(os.getenv("XENFORO_DB_MAX_OVERFLOW", "2"))
XENFORO_POOL_TIMEOUT = int(os.getenv("XENFORO_DB_POOL_TIMEOUT", "10"))

xenforo_engine = create_engine(
    f"mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/xenforo",
    pool_size=XENFORO_POOL_SIZE,
    max_overflow=XENFORO_POOL_OVERFLOW,
    pool_timeout=XENFORO_POOL_TIMEOUT,
    pool_pre_ping=True,
    pool_recycle=3600,
    isolation_level="READ COMMITTED",
)

XenforoSession = sessionmaker(bind=xenforo_engine)

def get_fresh_session():
    return Session()

def get_fresh_xenforo_session():
    return XenforoSession()

# This will be called after all models are defined
def setup_relationships():
    pass
#     """
#     Set up relationships between models after all models are defined.
#     This avoids circular import issues.
#     """
#     from db import Group
#     #from events.models import EventModel
    
#     # Add relationships
#     Group.events = relationship("EventModel", back_populates="group")
#     #EventModel.group = relationship("Group", back_populates="events") 