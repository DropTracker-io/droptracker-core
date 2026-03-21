"""
Background Video Processing Worker

Polls the database for VideoUpload records with status="uploaded",
downloads the raw MJPEG from B2, converts to MP4 using FFmpeg,
uploads the MP4 back to B2, and deletes the raw MJPEG.

This worker can run as:
1. A standalone script: python -m services.video_worker
2. An asyncio task started alongside the API
3. A systemd service

The worker processes one video at a time to keep CPU/memory usage predictable.
At ~2 seconds per conversion, a single worker handles ~1,800 videos/hour.
"""

import asyncio
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from db.models.base import Session
from db.models.video_upload import VideoUpload
from utils.b2_storage import B2_CDN_BASE_URL
from utils.video_storage import (
    VIDEO_LOCAL_FINAL_DIR,
    VIDEO_LOCAL_RAW_DIR,
    VIDEO_LOCAL_RETENTION_MINUTES,
    backend_for_video_record,
    delete_object,
    derive_final_key,
    download_to_local,
    get_public_video_url,
    resolve_internal_path,
    store_final_from_local,
)

# How often to poll for new work (seconds)
POLL_INTERVAL = int(os.getenv("VIDEO_WORKER_POLL_INTERVAL", "5"))

# Maximum concurrent FFmpeg conversions
_cpu_count = os.cpu_count() or 2
_default_max_concurrent = max(1, min(2, _cpu_count // 2 if _cpu_count > 1 else 1))
MAX_CONCURRENT = max(1, min(int(os.getenv("VIDEO_WORKER_MAX_CONCURRENT", str(_default_max_concurrent))), 4))

# Temp directory for video processing
TEMP_DIR = os.getenv("VIDEO_WORKER_TEMP_DIR", "/tmp/droptracker-video")

# FFmpeg settings
FFMPEG_CRF = os.getenv("VIDEO_FFMPEG_CRF", "28")
FFMPEG_PRESET = os.getenv("VIDEO_FFMPEG_PRESET", "fast")
FFMPEG_THREADS = max(1, min(int(os.getenv("VIDEO_FFMPEG_THREADS", "1")), 4))


def _extract_fps_from_key(video_key: str) -> int:
    """
    Extract the FPS value from the video key filename.
    
    The presigned URL endpoint embeds FPS in the filename like:
    raw/12345/uuid_fps20.mjpeg -> fps=20
    """
    import re
    match = re.search(r"_fps(\d+)\.", video_key)
    if match:
        return int(match.group(1))
    return 20  # default


async def _convert_mjpeg_to_mp4(input_path: str, output_path: str, fps: int) -> bool:
    """
    Convert raw MJPEG (concatenated JPEG frames) to H.264 MP4 using FFmpeg.
    
    Args:
        input_path: Path to the raw .mjpeg file
        output_path: Path for the output .mp4 file
        fps: Frame rate to use for the output video

    Returns:
        True on success, False on failure
    """
    # H.264 (libx264) requires even width/height. Some MJPEG sources can be odd-sized
    # (e.g. 1213x797), so pad to the next even dimension to avoid encoder failure.
    vf = "pad=ceil(iw/2)*2:ceil(ih/2)*2"

    cmd = [
        "ffmpeg",
        "-f", "mjpeg",
        "-framerate", str(fps),
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", FFMPEG_CRF,
        "-preset", FFMPEG_PRESET,
        "-threads", str(FFMPEG_THREADS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-y",  # overwrite output
        output_path,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=120,  # 2 minute timeout per video
        )

        if process.returncode != 0:
            print(f"[VideoWorker] FFmpeg error (exit {process.returncode}): {stderr.decode()[-500:]}")
            return False

        # Verify output file exists and is non-zero
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            print(f"[VideoWorker] FFmpeg produced empty output: {output_path}")
            return False

        return True

    except asyncio.TimeoutError:
        print(f"[VideoWorker] FFmpeg timed out for {input_path}")
        try:
            process.kill()
        except Exception:
            pass
        return False
    except FileNotFoundError:
        print("[VideoWorker] FFmpeg not found! Install ffmpeg: apt install ffmpeg")
        return False
    except Exception as e:
        print(f"[VideoWorker] FFmpeg exception: {e}")
        return False


async def process_single_video(video_id: int) -> bool:
    """
    Process a single video upload through the full pipeline:
    1. Mark as "processing"
    2. Download raw MJPEG from B2
    3. Convert to MP4 with FFmpeg
    4. Upload MP4 to B2
    5. Delete raw MJPEG from B2
    6. Update database record with final URL
    
    Args:
        video_id: The VideoUpload.id to process
        
    Returns:
        True on success, False on failure
    """
    db_session = Session()
    work_dir = None

    try:
        # Fetch the record
        video = db_session.query(VideoUpload).filter(VideoUpload.id == video_id).first()
        if not video:
            print(f"[VideoWorker] Video ID {video_id} not found")
            return False

        if video.status != "uploaded":
            print(f"[VideoWorker] Video ID {video_id} is in '{video.status}' state, skipping")
            return False

        storage_backend = backend_for_video_record(video)
        video_key = video.video_key
        fps = video.fps or _extract_fps_from_key(video_key)
        final_key = derive_final_key(video_key)

        print(
            f"[VideoWorker] Processing video {video_id}: {video_key} -> {final_key} "
            f"@ {fps}fps (backend={storage_backend})"
        )

        # Mark as processing
        video.status = "processing"
        db_session.commit()

        # Create temp working directory (for FFmpeg output; local backend skips raw copy)
        work_dir = tempfile.mkdtemp(dir=TEMP_DIR, prefix=f"vid_{video_id}_")
        mp4_path = os.path.join(work_dir, "output.mp4")

        start_time = time.perf_counter()

        # Step 1: Resolve raw MJPEG path (local: use in-place, B2: download to temp)
        if storage_backend == "local":
            raw_path = resolve_internal_path(video_key, backend="local")
            if not raw_path or not os.path.exists(raw_path):
                video.status = "failed"
                video.error_message = "Raw local file not found on disk"
                db_session.commit()
                return False
            download_time = 0.0
        else:
            raw_path = os.path.join(work_dir, "input.mjpeg")
            if not await download_to_local(video_key, raw_path, backend=storage_backend):
                video.status = "failed"
                video.error_message = "Failed to download raw MJPEG from storage"
                db_session.commit()
                return False
            download_time = time.perf_counter() - start_time

        raw_size = os.path.getsize(raw_path)
        video.file_size_raw = raw_size
        if storage_backend == "local":
            print(f"[VideoWorker] Using local raw file {raw_size / 1024 / 1024:.1f}MB (no copy)")
        else:
            print(f"[VideoWorker] Downloaded {raw_size / 1024 / 1024:.1f}MB in {download_time:.1f}s")

        # Step 2: Convert MJPEG to MP4
        convert_start = time.perf_counter()
        if not await _convert_mjpeg_to_mp4(raw_path, mp4_path, fps):
            video.status = "failed"
            video.error_message = "FFmpeg conversion failed"
            db_session.commit()
            return False

        final_size = os.path.getsize(mp4_path)
        video.file_size_final = final_size
        convert_time = time.perf_counter() - convert_start
        compression_ratio = raw_size / final_size if final_size > 0 else 0
        print(
            f"[VideoWorker] Converted in {convert_time:.1f}s: "
            f"{raw_size / 1024 / 1024:.1f}MB -> {final_size / 1024 / 1024:.1f}MB "
            f"({compression_ratio:.1f}x compression)"
        )

        # Step 3: Store MP4 on configured backend
        if not await store_final_from_local(mp4_path, final_key, backend=storage_backend):
            video.status = "failed"
            video.error_message = "Failed to upload converted MP4 to storage"
            db_session.commit()
            return False

        # Step 4: Delete raw MJPEG (best effort; retention cleanup is safety net)
        await delete_object(video_key, backend=storage_backend)

        # Step 5: Update database record
        video.final_key = final_key
        # Persist a stable URL if we have a CDN base configured.
        # If not, /video/status will compute a presigned URL dynamically.
        final_url = get_public_video_url(final_key, backend=storage_backend)
        if storage_backend == "b2":
            video.video_url = final_url if B2_CDN_BASE_URL else None
        else:
            # Local backend uses internal same-machine paths.
            video.video_url = final_url
        video.status = "processed"
        video.processed_at = datetime.now()

        # Finish: if the webhook already linked this video to a Drop (video.drop_id),
        # populate drops.video_url now that the MP4 is ready.
        should_backfill_url = storage_backend != "b2" or bool(B2_CDN_BASE_URL)
        if video.drop_id and should_backfill_url:
            try:
                from db import Drop
                drop = db_session.query(Drop).filter(Drop.drop_id == video.drop_id).first()
                if drop and not drop.video_url:
                    drop.video_url = final_url
            except Exception as e:
                print(f"[VideoWorker] Could not update Drop.video_url for drop_id={video.drop_id}: {e}")

        # Finish (generic): backfill video_url on other submission types by unique_id.
        if video.submission_type and video.submission_unique_id and should_backfill_url:
            try:
                submission_type = str(video.submission_type).lower().strip()
                unique_id = str(video.submission_unique_id)
                if submission_type == "personal_best":
                    from db import PersonalBestEntry
                    pb = db_session.query(PersonalBestEntry).filter(PersonalBestEntry.unique_id == unique_id).first()
                    if pb and not getattr(pb, "video_url", None):
                        pb.video_url = final_url
                elif submission_type == "combat_achievement":
                    from db import CombatAchievementEntry
                    ca = db_session.query(CombatAchievementEntry).filter(CombatAchievementEntry.unique_id == unique_id).first()
                    if ca and not getattr(ca, "video_url", None):
                        ca.video_url = final_url
                elif submission_type == "collection_log":
                    from db import CollectionLogEntry
                    clog = db_session.query(CollectionLogEntry).filter(CollectionLogEntry.unique_id == unique_id).first()
                    if clog and not getattr(clog, "video_url", None):
                        clog.video_url = final_url
                # pet currently has no stable DB row with image/video url stored
            except Exception as e:
                print(
                    f"[VideoWorker] Could not backfill video_url for {video.submission_type} "
                    f"unique_id={video.submission_unique_id}: {e}"
                )

        db_session.commit()

        total_time = time.perf_counter() - start_time
        print(f"[VideoWorker] Video {video_id} processed successfully in {total_time:.1f}s")
        return True

    except Exception as e:
        print(f"[VideoWorker] Error processing video {video_id}: {e}")
        try:
            video = db_session.query(VideoUpload).filter(VideoUpload.id == video_id).first()
            if video:
                video.status = "failed"
                video.error_message = str(e)[:1000]
                db_session.commit()
        except Exception:
            db_session.rollback()
        return False

    finally:
        try:
            db_session.close()
        except Exception:
            pass
        # Clean up temp files
        if work_dir and os.path.exists(work_dir):
            try:
                shutil.rmtree(work_dir)
            except Exception as e:
                print(f"[VideoWorker] Error cleaning up {work_dir}: {e}")


async def _fetch_pending_video_ids() -> list:
    """Fetch IDs of videos ready for processing (status='uploaded')."""
    db_session = Session()
    try:
        records = (
            db_session.query(VideoUpload.id)
            .filter(VideoUpload.status == "uploaded")
            .order_by(VideoUpload.created_at.asc())
            .limit(MAX_CONCURRENT * 2)  # fetch a small batch
            .all()
        )
        return [r.id for r in records]
    except Exception as e:
        print(f"[VideoWorker] Error fetching pending videos: {e}")
        return []
    finally:
        try:
            db_session.close()
        except Exception:
            pass


async def _cleanup_stale_processing(max_age_minutes: int = 30):
    """
    Reset videos stuck in 'processing' state back to 'uploaded'
    so they can be retried. This handles crashed workers.
    """
    db_session = Session()
    try:
        from sqlalchemy import and_
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        stale = (
            db_session.query(VideoUpload)
            .filter(
                and_(
                    VideoUpload.status == "processing",
                    VideoUpload.updated_at < cutoff,
                )
            )
            .all()
        )
        for video in stale:
            print(f"[VideoWorker] Resetting stale video {video.id} from 'processing' to 'uploaded'")
            video.status = "uploaded"
            video.error_message = "Reset from stale processing state"
        if stale:
            db_session.commit()
    except Exception as e:
        print(f"[VideoWorker] Error cleaning up stale records: {e}")
        db_session.rollback()
    finally:
        try:
            db_session.close()
        except Exception:
            pass


async def _cleanup_expired_pending(max_age_minutes: int = 60):
    """
    Mark videos stuck in 'pending' state as 'failed' after the
    presigned URL has long expired (10 min URL + generous buffer).
    """
    db_session = Session()
    try:
        from sqlalchemy import and_
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        expired = (
            db_session.query(VideoUpload)
            .filter(
                and_(
                    VideoUpload.status == "pending",
                    VideoUpload.created_at < cutoff,
                )
            )
            .all()
        )
        for video in expired:
            video.status = "failed"
            video.error_message = "Upload never completed (presigned URL expired)"
        if expired:
            db_session.commit()
            print(f"[VideoWorker] Marked {len(expired)} expired pending uploads as failed")
    except Exception as e:
        print(f"[VideoWorker] Error cleaning up expired pending records: {e}")
        db_session.rollback()
    finally:
        try:
            db_session.close()
        except Exception:
            pass


async def _cleanup_local_storage_retention(max_age_minutes: int = VIDEO_LOCAL_RETENTION_MINUTES):
    """
    Delete local backend files older than retention threshold.

    This is a fallback safety net in case immediate post-notification cleanup
    is skipped or notifications fail.
    """
    cutoff_ts = (datetime.now() - timedelta(minutes=max_age_minutes)).timestamp()
    for base_dir in (VIDEO_LOCAL_RAW_DIR, VIDEO_LOCAL_FINAL_DIR):
        try:
            if not os.path.isdir(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for file_name in files:
                    path = os.path.join(root, file_name)
                    try:
                        if os.path.getmtime(path) < cutoff_ts:
                            os.remove(path)
                    except FileNotFoundError:
                        continue
                    except Exception as e:
                        print(f"[VideoWorker] Error deleting retained local file {path}: {e}")
                # Best-effort pruning of empty directories
                try:
                    if root != base_dir and not os.listdir(root):
                        os.rmdir(root)
                except Exception:
                    pass
        except Exception as e:
            print(f"[VideoWorker] Error running local storage retention cleanup: {e}")


async def worker_loop():
    """
    Main worker loop. Polls for uploaded videos and processes them.
    
    Runs indefinitely until cancelled. Processes up to MAX_CONCURRENT
    videos in parallel, then sleeps for POLL_INTERVAL seconds.
    """
    print(f"[VideoWorker] Starting video processing worker")
    print(f"[VideoWorker] Poll interval: {POLL_INTERVAL}s, Max concurrent: {MAX_CONCURRENT}")
    print(
        f"[VideoWorker] FFmpeg CRF: {FFMPEG_CRF}, Preset: {FFMPEG_PRESET}, "
        f"Threads per job: {FFMPEG_THREADS}"
    )
    print(f"[VideoWorker] Local file retention: {VIDEO_LOCAL_RETENTION_MINUTES} minutes")
    print(f"[VideoWorker] Temp directory: {TEMP_DIR}")

    # Ensure temp directory exists
    os.makedirs(TEMP_DIR, exist_ok=True)

    cleanup_counter = 0

    while True:
        try:
            # Periodic cleanup (every 12 poll cycles = ~60s at default interval)
            cleanup_counter += 1
            if cleanup_counter >= 12:
                cleanup_counter = 0
                await _cleanup_stale_processing()
                await _cleanup_expired_pending()
                await _cleanup_local_storage_retention()

            # Fetch pending work
            video_ids = await _fetch_pending_video_ids()

            if not video_ids:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Process up to MAX_CONCURRENT videos in parallel
            batch = video_ids[:MAX_CONCURRENT]
            print(f"[VideoWorker] Processing batch of {len(batch)} videos: {batch}")

            tasks = [process_single_video(vid_id) for vid_id in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for vid_id, result in zip(batch, results):
                if isinstance(result, Exception):
                    print(f"[VideoWorker] Video {vid_id} raised exception: {result}")
                elif not result:
                    print(f"[VideoWorker] Video {vid_id} processing failed")

            # Short sleep between batches to avoid hammering the DB
            await asyncio.sleep(1)

        except asyncio.CancelledError:
            print("[VideoWorker] Worker cancelled, shutting down")
            break
        except Exception as e:
            print(f"[VideoWorker] Unexpected error in worker loop: {e}")
            await asyncio.sleep(POLL_INTERVAL)


async def start_worker():
    """Entry point for running the worker as an asyncio task."""
    await worker_loop()


def run_standalone():
    """Entry point for running the worker as a standalone script."""
    asyncio.run(worker_loop())


if __name__ == "__main__":
    run_standalone()
