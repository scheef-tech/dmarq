"""
Abstract base class for data source services.

All data source types (IMAP, Gmail API, Cloudflare) must implement this interface.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple


@dataclass
class SourceStatus:
    """Status information for a data source"""
    connected: bool
    message: str
    last_check: Optional[datetime] = None
    last_sync: Optional[datetime] = None
    stats: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class FetchResult:
    """Result of a fetch/sync operation"""
    success: bool
    message: str
    processed: int = 0
    reports_found: int = 0
    new_domains: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class BaseSourceService(ABC):
    """
    Abstract base class that all data source services must implement.

    This provides a consistent interface for:
    - Testing connections
    - Fetching DMARC reports (IMAP, Gmail) or syncing domains (Cloudflare)
    - Getting current status
    - Managing source-specific operations
    """

    def __init__(self, source_id: int, config: Dict[str, Any]):
        """
        Initialize the source service.

        Args:
            source_id: Database ID of the DataSource
            config: Decrypted configuration dictionary
        """
        self.source_id = source_id
        self.config = config

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Test the connection to the data source.

        Returns:
            Tuple of:
            - success: Whether connection was successful
            - message: Human-readable status message
            - stats: Dictionary with connection statistics (varies by source type)

        Example stats for IMAP:
            {"message_count": 100, "unread_count": 5, "dmarc_count": 20}

        Example stats for Cloudflare:
            {"zones_count": 10, "account_name": "my-account"}
        """
        pass

    @abstractmethod
    def fetch_reports(self, days: int = 7) -> FetchResult:
        """
        Fetch DMARC reports from the source.

        For IMAP/Gmail: Retrieves emails and parses DMARC report attachments.
        For Cloudflare: This is a no-op (Cloudflare doesn't have reports).

        Args:
            days: Number of days to look back for reports

        Returns:
            FetchResult with details about the operation
        """
        pass

    @abstractmethod
    def get_status(self) -> SourceStatus:
        """
        Get the current status of the data source.

        Returns:
            SourceStatus with connection info and statistics
        """
        pass

    def sync_domains(self) -> FetchResult:
        """
        Sync domains from the source.

        Only applicable to Cloudflare sources. IMAP/Gmail sources should
        return a no-op result.

        Returns:
            FetchResult with sync details
        """
        return FetchResult(
            success=True,
            message="Domain sync not applicable for this source type",
            processed=0
        )

    def get_source_type(self) -> str:
        """Get the type identifier for this source"""
        return self.__class__.__name__.replace("Client", "").replace("Service", "").lower()

    def validate_config(self) -> Tuple[bool, List[str]]:
        """
        Validate the source configuration.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []
        required_fields = self._get_required_config_fields()

        for field_name in required_fields:
            if not self.config.get(field_name):
                errors.append(f"Missing required field: {field_name}")

        return len(errors) == 0, errors

    @abstractmethod
    def _get_required_config_fields(self) -> List[str]:
        """
        Get list of required configuration fields for this source type.

        Returns:
            List of field names that must be present in config
        """
        pass
