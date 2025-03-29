from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
import os
from dotenv import load_dotenv
import pymysql
from sqlalchemy.orm import relationship

pymysql.install_as_MySQLdb()
load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

# Create base class for declarative models
Base = declarative_base()

# Create engine
engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data', 
                      pool_size=20, max_overflow=10)

# Create session factory
Session = sessionmaker(bind=engine)
session = Session()

# This will be called after all models are defined
def setup_relationships():
    """
    Set up relationships between models after all models are defined.
    This avoids circular import issues.
    """
    from db.models import Group
    from db.eventmodels import EventModel
    
    # Add relationships
    Group.events = relationship("Event", back_populates="group")
    EventModel.group = relationship("Group", back_populates="events") 