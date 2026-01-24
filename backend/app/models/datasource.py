"""
Models for managing data sources (IMAP, Gmail API, Cloudflare accounts)
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index, Enum as SQLEnum
from sqlalchemy.orm import relationship

from app.core.database import Base


class DataSourceType(str, Enum):
    """Types of data sources supported"""
    IMAP = "imap"
    GMAIL_API = "gmail_api"
    CLOUDFLARE = "cloudflare"


class DataSourceStatus(str, Enum):
    """Connection status of a data source"""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    PENDING = "pending"  # Initial state before first connection test


class DataSource(Base):
    """
    Data source configuration for retrieving DMARC reports or syncing domains.

    Supports multiple source types:
    - IMAP: Traditional email server connection
    - Gmail API: OAuth-based Gmail access
    - Cloudflare: Domain/DNS sync from Cloudflare accounts
    """

    __tablename__ = "data_sources"

    id = Column(Integer, primary_key=True, index=True)

    # Source identification
    type = Column(SQLEnum(DataSourceType), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # User-friendly name

    # Encrypted configuration (JSON with credentials/settings)
    # Structure varies by type:
    # IMAP: {"server": "", "port": 993, "username": "", "password": "", "ssl": true}
    # Gmail API: {"client_id": "", "client_secret": "", "refresh_token": "", "email": ""}
    # Cloudflare: {"api_token": "", "account_id": "", "account_name": ""}
    config_encrypted = Column(Text, nullable=False)

    # Connection status
    status = Column(SQLEnum(DataSourceStatus), default=DataSourceStatus.PENDING, index=True)
    error_message = Column(Text, nullable=True)

    # Sync tracking
    last_check = Column(DateTime, nullable=True)  # Last connection check
    last_sync = Column(DateTime, nullable=True)   # Last successful data sync

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    logs = relationship("DataSourceLog", back_populates="source", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index('ix_data_sources_type_status', 'type', 'status'),
    )

    def __repr__(self):
        return f"<DataSource {self.id}: {self.name} ({self.type.value})>"


class DataSourceLog(Base):
    """
    Log entries for data source operations.
    Tracks connection tests, sync operations, errors, etc.
    """

    __tablename__ = "data_source_logs"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True)

    # Log entry details
    level = Column(String(20), default="info", index=True)  # info, warning, error, success
    message = Column(Text, nullable=False)
    details = Column(Text, nullable=True)  # JSON string for extra context

    # Timestamp
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    source = relationship("DataSource", back_populates="logs")

    # Indexes
    __table_args__ = (
        Index('ix_data_source_logs_source_timestamp', 'source_id', 'timestamp'),
    )

    def __repr__(self):
        return f"<DataSourceLog {self.id}: [{self.level}] {self.message[:50]}>"
