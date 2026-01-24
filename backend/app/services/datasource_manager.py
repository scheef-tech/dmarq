"""
Data Source Manager - Factory and orchestration for all source types.

Provides:
- Factory pattern for creating source clients based on type
- CRUD operations for data sources
- Orchestration of sync/fetch operations across sources
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Type

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import encrypt_config, decrypt_config
from app.models.datasource import DataSource, DataSourceLog, DataSourceType, DataSourceStatus
from app.services.base_source import BaseSourceService, SourceStatus, FetchResult

logger = logging.getLogger(__name__)


class DataSourceManager:
    """
    Manager for data source operations.

    Handles:
    - Creating/updating/deleting data sources
    - Factory pattern for instantiating the correct service based on type
    - Logging operations to DataSourceLog
    - Orchestrating operations across multiple sources
    """

    # Registry of source type -> service class
    # Will be populated as services are implemented
    _service_registry: Dict[DataSourceType, Type[BaseSourceService]] = {}

    @classmethod
    def register_service(cls, source_type: DataSourceType, service_class: Type[BaseSourceService]):
        """
        Register a service class for a source type.

        Args:
            source_type: The DataSourceType enum value
            service_class: The service class that handles this type
        """
        cls._service_registry[source_type] = service_class
        logger.info(f"Registered service {service_class.__name__} for {source_type.value}")

    def __init__(self):
        self._db: Optional[Session] = None

    def _get_db(self) -> Session:
        """Get a database session"""
        if self._db is None:
            self._db = SessionLocal()
        return self._db

    def _close_db(self):
        """Close the database session"""
        if self._db is not None:
            self._db.close()
            self._db = None

    def _log(self, source_id: int, level: str, message: str, details: Dict = None):
        """Add a log entry for a data source"""
        db = self._get_db()
        try:
            log_entry = DataSourceLog(
                source_id=source_id,
                level=level,
                message=message,
                details=json.dumps(details) if details else None
            )
            db.add(log_entry)
            db.commit()
        except Exception as e:
            logger.error(f"Failed to write source log: {e}")
            db.rollback()

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def list_sources(self, source_type: DataSourceType = None) -> List[Dict[str, Any]]:
        """
        List all data sources, optionally filtered by type.

        Args:
            source_type: Optional filter by source type

        Returns:
            List of source dictionaries (without decrypted config)
        """
        db = self._get_db()
        try:
            query = db.query(DataSource)
            if source_type:
                query = query.filter(DataSource.type == source_type)

            sources = query.order_by(DataSource.created_at.desc()).all()

            return [
                {
                    "id": s.id,
                    "type": s.type.value,
                    "name": s.name,
                    "status": s.status.value,
                    "error_message": s.error_message,
                    "last_check": s.last_check.isoformat() if s.last_check else None,
                    "last_sync": s.last_sync.isoformat() if s.last_sync else None,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                }
                for s in sources
            ]
        finally:
            pass  # Keep session open for potential follow-up operations

    def get_source(self, source_id: int, include_config: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get a single data source by ID.

        Args:
            source_id: The source ID
            include_config: If True, include decrypted config (for internal use)

        Returns:
            Source dictionary or None if not found
        """
        db = self._get_db()
        source = db.query(DataSource).filter(DataSource.id == source_id).first()

        if not source:
            return None

        result = {
            "id": source.id,
            "type": source.type.value,
            "name": source.name,
            "status": source.status.value,
            "error_message": source.error_message,
            "last_check": source.last_check.isoformat() if source.last_check else None,
            "last_sync": source.last_sync.isoformat() if source.last_sync else None,
            "created_at": source.created_at.isoformat() if source.created_at else None,
            "updated_at": source.updated_at.isoformat() if source.updated_at else None,
        }

        if include_config:
            try:
                result["config"] = decrypt_config(source.config_encrypted)
            except Exception as e:
                logger.error(f"Failed to decrypt config for source {source_id}: {e}")
                result["config"] = {}

        return result

    def create_source(
        self,
        source_type: DataSourceType,
        name: str,
        config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create a new data source.

        Args:
            source_type: Type of source (imap, gmail_api, cloudflare)
            name: User-friendly name for the source
            config: Configuration dictionary (will be encrypted)

        Returns:
            Created source dictionary
        """
        db = self._get_db()
        try:
            encrypted_config = encrypt_config(config)

            source = DataSource(
                type=source_type,
                name=name,
                config_encrypted=encrypted_config,
                status=DataSourceStatus.PENDING
            )
            db.add(source)
            db.commit()
            db.refresh(source)

            self._log(source.id, "info", f"Data source created: {name}")

            return {
                "id": source.id,
                "type": source.type.value,
                "name": source.name,
                "status": source.status.value,
                "created_at": source.created_at.isoformat()
            }

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to create source: {e}")
            raise

    def update_source(
        self,
        source_id: int,
        name: str = None,
        config: Dict[str, Any] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Update an existing data source.

        Args:
            source_id: ID of source to update
            name: New name (optional)
            config: New configuration (optional, will be encrypted)

        Returns:
            Updated source dictionary or None if not found
        """
        db = self._get_db()
        try:
            source = db.query(DataSource).filter(DataSource.id == source_id).first()
            if not source:
                return None

            if name:
                source.name = name

            if config:
                source.config_encrypted = encrypt_config(config)
                # Reset status when config changes
                source.status = DataSourceStatus.PENDING
                source.error_message = None

            db.commit()
            db.refresh(source)

            self._log(source_id, "info", "Data source updated")

            return self.get_source(source_id)

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update source {source_id}: {e}")
            raise

    def delete_source(self, source_id: int) -> bool:
        """
        Delete a data source and all its logs.

        Args:
            source_id: ID of source to delete

        Returns:
            True if deleted, False if not found
        """
        db = self._get_db()
        try:
            source = db.query(DataSource).filter(DataSource.id == source_id).first()
            if not source:
                return False

            source_name = source.name
            db.delete(source)
            db.commit()

            logger.info(f"Deleted data source: {source_name}")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete source {source_id}: {e}")
            raise

    # =========================================================================
    # Service Factory
    # =========================================================================

    def get_service(self, source_id: int) -> Optional[BaseSourceService]:
        """
        Get the appropriate service instance for a data source.

        Args:
            source_id: ID of the data source

        Returns:
            Service instance or None if source not found or service not registered
        """
        source_data = self.get_source(source_id, include_config=True)
        if not source_data:
            logger.error(f"Source {source_id} not found")
            return None

        source_type = DataSourceType(source_data["type"])
        service_class = self._service_registry.get(source_type)

        if not service_class:
            logger.error(f"No service registered for type: {source_type.value}")
            return None

        return service_class(source_id, source_data.get("config", {}))

    # =========================================================================
    # Operations
    # =========================================================================

    def test_connection(self, source_id: int) -> Dict[str, Any]:
        """
        Test the connection for a data source.

        Args:
            source_id: ID of the source to test

        Returns:
            Test result dictionary
        """
        db = self._get_db()
        source = db.query(DataSource).filter(DataSource.id == source_id).first()
        if not source:
            return {"success": False, "message": "Source not found"}

        service = self.get_service(source_id)
        if not service:
            return {"success": False, "message": "Service not available for this source type"}

        try:
            success, message, stats = service.test_connection()

            # Update source status
            source.last_check = datetime.utcnow()
            if success:
                source.status = DataSourceStatus.CONNECTED
                source.error_message = None
                self._log(source_id, "success", f"Connection test passed: {message}", stats)
            else:
                source.status = DataSourceStatus.ERROR
                source.error_message = message
                self._log(source_id, "error", f"Connection test failed: {message}")

            db.commit()

            return {
                "success": success,
                "message": message,
                "stats": stats,
                "status": source.status.value
            }

        except Exception as e:
            error_msg = str(e)
            source.status = DataSourceStatus.ERROR
            source.error_message = error_msg
            source.last_check = datetime.utcnow()
            db.commit()

            self._log(source_id, "error", f"Connection test error: {error_msg}")
            return {"success": False, "message": error_msg}

    def fetch_reports(self, source_id: int, days: int = 7) -> Dict[str, Any]:
        """
        Fetch DMARC reports from a source.

        Args:
            source_id: ID of the source
            days: Number of days to look back

        Returns:
            Fetch result dictionary
        """
        db = self._get_db()
        source = db.query(DataSource).filter(DataSource.id == source_id).first()
        if not source:
            return {"success": False, "message": "Source not found"}

        service = self.get_service(source_id)
        if not service:
            return {"success": False, "message": "Service not available for this source type"}

        try:
            self._log(source_id, "info", f"Starting report fetch (last {days} days)")
            result = service.fetch_reports(days)

            source.last_sync = datetime.utcnow()
            if result.success:
                source.status = DataSourceStatus.CONNECTED
                source.error_message = None
                self._log(source_id, "success",
                         f"Fetch completed: {result.reports_found} reports from {result.processed} emails",
                         {"reports_found": result.reports_found, "processed": result.processed})
            else:
                source.status = DataSourceStatus.ERROR
                source.error_message = result.message
                self._log(source_id, "error", f"Fetch failed: {result.message}")

            db.commit()

            return {
                "success": result.success,
                "message": result.message,
                "processed": result.processed,
                "reports_found": result.reports_found,
                "new_domains": result.new_domains,
                "errors": result.errors
            }

        except Exception as e:
            error_msg = str(e)
            self._log(source_id, "error", f"Fetch error: {error_msg}")
            return {"success": False, "message": error_msg}

    def sync_domains(self, source_id: int) -> Dict[str, Any]:
        """
        Sync domains from a source (Cloudflare only).

        Args:
            source_id: ID of the source

        Returns:
            Sync result dictionary
        """
        db = self._get_db()
        source = db.query(DataSource).filter(DataSource.id == source_id).first()
        if not source:
            return {"success": False, "message": "Source not found"}

        if source.type != DataSourceType.CLOUDFLARE:
            return {"success": False, "message": "Domain sync only available for Cloudflare sources"}

        service = self.get_service(source_id)
        if not service:
            return {"success": False, "message": "Service not available"}

        try:
            self._log(source_id, "info", "Starting domain sync")
            result = service.sync_domains()

            source.last_sync = datetime.utcnow()
            if result.success:
                source.status = DataSourceStatus.CONNECTED
                self._log(source_id, "success",
                         f"Sync completed: {result.processed} domains synced",
                         result.details)
            else:
                source.status = DataSourceStatus.ERROR
                source.error_message = result.message

            db.commit()

            return {
                "success": result.success,
                "message": result.message,
                "processed": result.processed,
                "new_domains": result.new_domains,
                "details": result.details
            }

        except Exception as e:
            error_msg = str(e)
            self._log(source_id, "error", f"Sync error: {error_msg}")
            return {"success": False, "message": error_msg}

    def get_logs(self, source_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get log entries for a data source.

        Args:
            source_id: ID of the source
            limit: Maximum number of logs to return

        Returns:
            List of log entries
        """
        db = self._get_db()
        logs = db.query(DataSourceLog).filter(
            DataSourceLog.source_id == source_id
        ).order_by(DataSourceLog.timestamp.desc()).limit(limit).all()

        return [
            {
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "details": json.loads(log.details) if log.details else None,
                "timestamp": log.timestamp.isoformat()
            }
            for log in reversed(logs)  # Return in chronological order
        ]

    def clear_logs(self, source_id: int) -> int:
        """
        Clear all logs for a data source.

        Args:
            source_id: ID of the source

        Returns:
            Number of logs deleted
        """
        db = self._get_db()
        try:
            count = db.query(DataSourceLog).filter(
                DataSourceLog.source_id == source_id
            ).delete()
            db.commit()
            return count
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to clear logs for source {source_id}: {e}")
            return 0


# Singleton instance
_manager: Optional[DataSourceManager] = None


def get_datasource_manager() -> DataSourceManager:
    """Get the singleton DataSourceManager instance"""
    global _manager
    if _manager is None:
        _manager = DataSourceManager()
    return _manager
