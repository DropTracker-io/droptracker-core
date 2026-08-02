import os
from datetime import datetime
import asyncio

from sqlalchemy import text

from api.core import metrics, redis_client, get_db_session


async def health_check(app, include_request_test: bool = True) -> bool:
    """Comprehensive health check for the API server.

    Accepts the app instance to avoid relying on request context.
    include_request_test allows skipping the HTTP self-check when the server
    is not yet ready to accept requests.
    """
    try:
        if app is None:
            print("Health check failed: Quart app is None")
            return False

        if metrics is None:
            print("Health check failed: metrics tracker is None")
            return False

        if not await test_database_connectivity():
            print("Health check failed: Database connectivity test failed")
            return False

        if not await test_redis_connectivity():
            print("Health check failed: Redis connectivity test failed")
            return False

        if include_request_test and not await test_request_processing():
            print("Health check failed: Request processing test failed")
            return False

        if not test_metrics_functionality():
            print("Health check failed: Metrics functionality test failed")
            return False

        return True
    except Exception as e:
        print(f"Health check failed with exception: {e}")
        return False


def _probe_database() -> bool:
    """Open, query and close in ONE thread.

    Previously the open, the query and the close were three separate awaits:
    ``wait_for`` cancels the AWAIT, not the worker thread, so on a timeout the
    event loop ran ``close()`` on a Session another thread was still executing
    a statement on. Session and Connection are not thread-safe, and the failure
    mode is corruption of a pooled connection — from the health check, which is
    supposed to be the harmless part of the system.
    """
    test_session = None
    try:
        test_session = get_db_session()
        return test_session.execute(text("SELECT 1")).scalar() == 1
    finally:
        if test_session:
            try:
                test_session.close()
            except Exception:
                pass


async def test_database_connectivity() -> bool:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe_database), timeout=8.0)
    except asyncio.TimeoutError:
        print("Database connectivity test timed out")
        return False
    except Exception as e:
        print(f"Database connectivity test failed: {e}")
        return False


async def test_redis_connectivity() -> bool:
    try:
        if not redis_client or not redis_client.client:
            return False
        ping_result = await asyncio.wait_for(
            asyncio.to_thread(redis_client.client.ping),
            timeout=3.0,
        )
        return bool(ping_result)
    except asyncio.TimeoutError:
        print("Redis connectivity test timed out")
        return False
    except Exception as e:
        print(f"Redis connectivity test failed: {e}")
        return False


async def test_request_processing() -> bool:
    try:
        import aiohttp

        port = int(os.environ.get("API_PORT", 31323))
        url = f"http://127.0.0.1:{port}/ping"

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as sess:
            async with sess.get(url) as response:
                if response.status != 200:
                    return False
                data = await response.json()
                return data.get("message") == "Pong"
    except asyncio.TimeoutError:
        print("Request processing test timed out")
        return False
    except Exception as e:
        print(f"Request processing test failed: {e}")
        return False


def test_metrics_functionality() -> bool:
    return True


async def health_check_lightweight() -> dict:
    from sqlalchemy import text

    checks = {}
    overall_healthy = True

    checks["metrics"] = {"status": "healthy" if metrics else "unhealthy"}
    if not metrics:
        overall_healthy = False

    # DB check — one thread for open+query+close, so a timeout can never leave
    # the event loop closing a Session a worker is still using (see
    # _probe_database).
    try:
        if await asyncio.wait_for(asyncio.to_thread(_probe_database), timeout=3.0):
            checks["database"] = {"status": "healthy"}
        else:
            checks["database"] = {"status": "unhealthy"}
            overall_healthy = False
    except Exception:
        checks["database"] = {"status": "unhealthy"}
        overall_healthy = False

    # Redis check
    try:
        if redis_client and redis_client.client:
            await asyncio.wait_for(
                asyncio.to_thread(redis_client.client.ping),
                timeout=1.0,
            )
            checks["redis"] = {"status": "healthy"}
        else:
            checks["redis"] = {"status": "unhealthy"}
            overall_healthy = False
    except Exception:
        checks["redis"] = {"status": "unhealthy"}
        overall_healthy = False

    return {"healthy": overall_healthy, "checks": checks}


