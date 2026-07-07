"""
Video Upload Database Model

This module defines the VideoUpload model which tracks video uploads through
the processing pipeline: pending -> uploading -> uploaded -> processing -> processed -> failed.

The lifecycle is:
1. API generates presigned URL, creates record with status="pending"
2. Plugin uploads MJPEG to presigned URL, status becomes "uploaded" via webhook
3. Background worker converts MJPEG to MP4, status becomes "processing" then "processed"
4. If anything fails, status becomes "failed"

Author: joelhalen
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, BigInteger, Index
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class VideoUpload(Base):
    """
    Represents a video upload in the DropTracker video processing pipeline.

    Attributes:
        id (int): Primary key, auto-incrementing identifier
        player_id (int): Foreign key to the player who uploaded the video
        video_key (str): Unique object key in B2 storage (e.g. "dt_raw/12345/uuid_fps20.mjpeg")
        final_key (str): Object key of the processed MP4 (e.g. "dt_videos/12345/uuid.mp4")
        video_url (str): Public CDN URL of the final processed video
        fps (int): Frame rate of the video (extracted from filename)
        status (str): Current status in the pipeline:
            "pending"    - presigned URL generated, awaiting upload
            "uploaded"   - raw MJPEG uploaded to B2, awaiting processing
            "processing" - FFmpeg conversion in progress
            "processed"  - MP4 ready, raw MJPEG deleted
            "failed"     - processing failed (see error_message)
        error_message (str): Error details if status is "failed"
        file_size_raw (int): Size of raw MJPEG in bytes (nullable)
        file_size_final (int): Size of final MP4 in bytes (nullable)
        drop_id (int): Foreign key to the associated Drop (nullable, set when webhook arrives)
        submission_type (str): Type of submission this video is for (drop, pb, ca, etc.)
        created_at (datetime): When the upload record was created
        updated_at (datetime): When the record was last updated
        processed_at (datetime): When FFmpeg conversion completed
    """
    __tablename__ = "video_uploads"
    __table_args__ = (
        Index("idx_video_status", "status"),
        Index("idx_video_player_date", "player_id", "created_at"),
        Index("idx_video_key", "video_key", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False, index=True)
    video_key = Column(String(500), nullable=False, unique=True)
    final_key = Column(String(500), nullable=True)
    video_url = Column(String(500), nullable=True)
    fps = Column(Integer, nullable=False, default=20)
    status = Column(String(20), nullable=False, default="pending", index=True)
    error_message = Column(String(1000), nullable=True)
    file_size_raw = Column(BigInteger, nullable=True)
    file_size_final = Column(BigInteger, nullable=True)
    drop_id = Column(Integer, ForeignKey("drops.drop_id"), nullable=True, index=True)
    submission_type = Column(String(50), nullable=True)
    storage_backend = Column(String(20), nullable=False, default="b2", index=True)
    # The unique_id/guid of the associated submission row (drop/pb/ca/clog/pet)
    # so background workers can backfill video URLs even if the submission was
    # created before video processing completed.
    submission_unique_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, onupdate=func.now(), default=func.now())
    processed_at = Column(DateTime, nullable=True)

    # Relationships
    player = relationship("Player", backref="video_uploads")
    drop = relationship("Drop", backref="video_upload")
