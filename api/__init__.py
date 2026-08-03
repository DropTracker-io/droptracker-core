import os
import asyncio
import time
from datetime import timedelta

from quart import Quart, jsonify, g, request
from quart_jwt_extended import JWTManager
from quart_rate_limiter import RateLimiter

from api.core import metrics
from api.core import log_pool_status
from api.routes.health import health_bp
from api.routes.players import players_bp
from api.routes.groups import groups_bp
from api.routes.group_create import group_create_bp
from api.routes.group_export import group_export_bp
from api.routes.personal_bests import personal_bests_bp
from api.routes.utils import utils_bp
from api.routes.webhook import webhook_bp
from api.routes.notifications import notifications_bp
from api.routes.video import video_bp
from api.worker import create_blueprint as create_worker_blueprint


_SLOW_REQUEST_THRESHOLD_MS = float(os.getenv("REQUEST_SLOW_THRESHOLD_MS", "750"))
_LOOP_LAG_THRESHOLD_SECONDS = float(os.getenv("EVENT_LOOP_LAG_THRESHOLD", "0.5"))


def create_app() -> Quart:
    from utils.sentry import init_sentry
    init_sentry("droptracker-api")

    app = Quart(__name__)

    # Configure logging to suppress HTTP access logs
    import logging
    logging.getLogger('quart.serving').setLevel(logging.ERROR)
    logging.getLogger('hypercorn.access').setLevel(logging.CRITICAL + 1)
    logging.getLogger('hypercorn.access').disabled = True

    # Core config
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_TOKEN_KEY")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=1)

    # Extensions
    JWTManager(app)
    RateLimiter(app)

    @app.before_request
    async def _record_request_start():
        g.request_start_time = time.perf_counter()

    @app.after_request
    async def _log_request_duration(response):
        start_time = getattr(g, "request_start_time", None)
        if start_time is not None:
            duration_ms = (time.perf_counter() - start_time) * 1000
            submission_type = getattr(g, "submission_type", None)
            subtype_suffix = f" [type={submission_type}]" if submission_type else ""
            if duration_ms >= _SLOW_REQUEST_THRESHOLD_MS:
                log_pool_status(
                    f"slow request {request.method} {request.path}{subtype_suffix} ({duration_ms:.2f} ms)"
                )
                print(
                    f"[RequestTiming] {request.method} {request.path}{subtype_suffix} "
                    f"took {duration_ms:.2f} ms"
                )
        return response

    async def _monitor_event_loop(interval: float = 1.0):
        loop = asyncio.get_running_loop()
        next_expected = loop.time() + interval
        while True:
            await asyncio.sleep(interval)
            now = loop.time()
            lag = now - next_expected
            if lag > _LOOP_LAG_THRESHOLD_SECONDS:
                print(f"[EventLoopLag] Detected {lag:.3f}s lag (threshold {_LOOP_LAG_THRESHOLD_SECONDS:.3f}s)")
                log_pool_status("loop lag")
            next_expected = now + interval

    # Error handlers
    @app.errorhandler(404)
    async def _not_found(e):
        return jsonify({"error": "Resource not found"}), 404

    @app.errorhandler(500)
    async def _server_error(e):
        return jsonify({"error": "Internal server error"}), 500

    # Blueprints
    app.register_blueprint(create_worker_blueprint(), url_prefix='/')
    app.register_blueprint(health_bp, url_prefix='/')
    app.register_blueprint(players_bp, url_prefix='/')
    app.register_blueprint(groups_bp, url_prefix='/')
    app.register_blueprint(group_create_bp, url_prefix='/')
    app.register_blueprint(group_export_bp, url_prefix='/')
    app.register_blueprint(personal_bests_bp, url_prefix='/')
    app.register_blueprint(utils_bp, url_prefix='/')
    app.register_blueprint(webhook_bp, url_prefix='/')
    app.register_blueprint(notifications_bp, url_prefix='/')
    app.register_blueprint(video_bp, url_prefix='/')

    @app.before_serving
    async def _start_monitor():
        app.loop_lag_task = asyncio.create_task(_monitor_event_loop())
        from api import notify_wake
        app.notify_wake_task = asyncio.create_task(notify_wake.run_listener())

    @app.after_serving
    async def _stop_monitor():
        for attr in ("loop_lag_task", "notify_wake_task"):
            task = getattr(app, attr, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return app


__all__ = ["create_app"]


