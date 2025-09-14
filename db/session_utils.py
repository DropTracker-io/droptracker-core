"""
Database session utilities for improved connection handling and error recovery
"""

import functools
import asyncio
from typing import Callable, Any
from sqlalchemy.exc import OperationalError, DisconnectionError
from sqlalchemy.orm import Session as SQLSession
from db.base import Session


def with_database_session(func: Callable) -> Callable:
    """
    Decorator that provides a database session to a function and handles cleanup.
    Use this for functions that need a database session but don't already have one.
    
    The decorated function should accept a 'session' parameter.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        session = Session()
        try:
            # Add session to kwargs
            kwargs['session'] = session
            result = func(*args, **kwargs)
            session.commit()
            return result
        except Exception as e:
            session.rollback()
            raise
        finally:
            session.close()
    return wrapper


def with_database_session_async(func: Callable) -> Callable:
    """
    Async version of with_database_session decorator.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        session = Session()
        try:
            # Add session to kwargs
            kwargs['session'] = session
            result = await func(*args, **kwargs)
            session.commit()
            return result
        except Exception as e:
            session.rollback()
            raise
        finally:
            session.close()
    return wrapper


def retry_on_connection_error(max_retries: int = 3, delay: float = 1.0):
    """
    Decorator to retry database operations on connection failures.
    
    Args:
        max_retries: Maximum number of retry attempts
        delay: Delay between retry attempts in seconds
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (OperationalError, DisconnectionError) as e:
                    last_exception = e
                    error_msg = str(e).lower()
                    if any(msg in error_msg for msg in ["server has gone away", "connection reset", "lost connection"]):
                        print(f"Database connection lost on attempt {attempt + 1}/{max_retries}, retrying in {delay}s...")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(delay)
                        continue
                    else:
                        raise  # Re-raise if it's not a connection issue
                except Exception as e:
                    # For non-database connection errors, don't retry
                    raise
            
            # If we get here, all retries failed
            print(f"All {max_retries} database retry attempts failed")
            raise last_exception
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, DisconnectionError) as e:
                    last_exception = e
                    error_msg = str(e).lower()
                    if any(msg in error_msg for msg in ["server has gone away", "connection reset", "lost connection"]):
                        print(f"Database connection lost on attempt {attempt + 1}/{max_retries}, retrying in {delay}s...")
                        if attempt < max_retries - 1:
                            import time
                            time.sleep(delay)
                        continue
                    else:
                        raise  # Re-raise if it's not a connection issue
                except Exception as e:
                    # For non-database connection errors, don't retry
                    raise
            
            # If we get here, all retries failed
            print(f"All {max_retries} database retry attempts failed")
            raise last_exception
        
        # Return the appropriate wrapper based on whether the function is async
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


class DatabaseSessionManager:
    """
    Context manager for database sessions with automatic error handling and cleanup.
    
    Usage:
        async with DatabaseSessionManager() as session:
            # Use session here
            player = session.query(Player).first()
    """
    
    def __init__(self, auto_commit: bool = True):
        self.auto_commit = auto_commit
        self.session: SQLSession = None
    
    def __enter__(self):
        self.session = Session()
        return self.session
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None and self.auto_commit:
            try:
                self.session.commit()
            except Exception as e:
                self.session.rollback()
                raise
        elif exc_type is not None:
            self.session.rollback()
        
        self.session.close()
        return False  # Don't suppress exceptions
    
    async def __aenter__(self):
        self.session = Session()
        return self.session
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None and self.auto_commit:
            try:
                self.session.commit()
            except Exception as e:
                self.session.rollback()
                raise
        elif exc_type is not None:
            self.session.rollback()
        
        self.session.close()
        return False  # Don't suppress exceptions


# Convenience functions for common patterns
def safe_database_operation(operation: Callable, *args, **kwargs) -> Any:
    """
    Execute a database operation with automatic session management and error handling.
    
    Args:
        operation: Function that takes a session as first argument
        *args, **kwargs: Additional arguments to pass to the operation
    
    Returns:
        Result of the operation
    """
    with DatabaseSessionManager() as session:
        return operation(session, *args, **kwargs)


async def safe_database_operation_async(operation: Callable, *args, **kwargs) -> Any:
    """
    Async version of safe_database_operation.
    """
    async with DatabaseSessionManager() as session:
        return await operation(session, *args, **kwargs)
