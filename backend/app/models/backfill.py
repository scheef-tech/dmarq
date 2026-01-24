"""
Models for tracking backfill progress and processed emails
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Index

from app.core.database import Base


class ProcessedEmail(Base):
    """Track which emails have been processed (for resume capability)"""

    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String, unique=True, nullable=False, index=True)  # IMAP Message-ID header
    imap_uid = Column(Integer, nullable=True)  # IMAP UID (can change between sessions)
    processed_at = Column(DateTime, default=datetime.utcnow, index=True)
    had_report = Column(Boolean, default=False)  # Whether it contained a DMARC report
    domain_found = Column(String, nullable=True)  # Domain if report was found
    error = Column(Text, nullable=True)  # Error message if processing failed

    __table_args__ = (
        Index('ix_processed_emails_date', 'processed_at'),
    )


class BackfillLog(Base):
    """Log entries for backfill operations"""

    __tablename__ = "backfill_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    level = Column(String, default="info")  # info, warning, error, success
    message = Column(Text, nullable=False)
    details = Column(Text, nullable=True)  # JSON string for extra details
