"""
Backfill service with progress tracking and resume capability
"""
import imaplib
import email
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict
from email.header import decode_header

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.database import SessionLocal, engine, Base
from app.core.config import get_settings
from app.models.backfill import ProcessedEmail, BackfillLog
from app.services.dmarc_parser import DMARCParser
from app.services.persistent_store import ReportStore

logger = logging.getLogger(__name__)


@dataclass
class BackfillState:
    """Current state of the backfill operation"""
    status: str = "idle"  # idle, running, paused, completed, error
    total_emails: int = 0
    processed_emails: int = 0
    skipped_emails: int = 0  # Already processed (resume)
    reports_found: int = 0
    domains_found: List[str] = field(default_factory=list)
    errors: int = 0
    started_at: Optional[str] = None
    last_update: Optional[str] = None
    current_email: Optional[str] = None
    rate: float = 0.0  # emails per second
    eta_seconds: Optional[int] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def progress_percent(self) -> float:
        if self.total_emails == 0:
            return 0
        return round((self.processed_emails + self.skipped_emails) / self.total_emails * 100, 1)


class BackfillService:
    """Service for managing DMARC report backfill with resume capability"""

    _instance = None
    _state: BackfillState = None
    _stop_requested: bool = False
    _task: Optional[asyncio.Task] = None

    @classmethod
    def get_instance(cls) -> 'BackfillService':
        if cls._instance is None:
            cls._instance = BackfillService()
        return cls._instance

    def __init__(self):
        self._state = BackfillState()
        self._stop_requested = False
        # Ensure tables exist
        Base.metadata.create_all(bind=engine)

    def _get_db(self) -> Session:
        return SessionLocal()

    def _log(self, level: str, message: str, details: Dict = None):
        """Add log entry to database and logger"""
        db = self._get_db()
        try:
            log_entry = BackfillLog(
                level=level,
                message=message,
                details=json.dumps(details) if details else None
            )
            db.add(log_entry)
            db.commit()

            # Also log to standard logger
            log_func = getattr(logger, level, logger.info)
            log_func(f"[Backfill] {message}")
        except Exception as e:
            logger.error(f"Failed to write log: {e}")
        finally:
            db.close()

    def _is_email_processed(self, message_id: str) -> bool:
        """Check if an email has already been processed"""
        db = self._get_db()
        try:
            exists = db.query(ProcessedEmail).filter(
                ProcessedEmail.message_id == message_id
            ).first() is not None
            return exists
        finally:
            db.close()

    def _mark_email_processed(self, message_id: str, had_report: bool = False,
                               domain_found: str = None, error: str = None):
        """Mark an email as processed"""
        db = self._get_db()
        try:
            processed = ProcessedEmail(
                message_id=message_id,
                had_report=had_report,
                domain_found=domain_found,
                error=error
            )
            db.add(processed)
            db.commit()
        except Exception as e:
            db.rollback()
            # Might be duplicate, ignore
            pass
        finally:
            db.close()

    def _get_processed_count(self) -> int:
        """Get count of processed emails from database"""
        db = self._get_db()
        try:
            return db.query(func.count(ProcessedEmail.id)).scalar() or 0
        finally:
            db.close()

    def get_state(self) -> Dict[str, Any]:
        """Get current backfill state"""
        state_dict = self._state.to_dict()
        state_dict['progress_percent'] = self._state.progress_percent
        state_dict['total_processed_in_db'] = self._get_processed_count()
        return state_dict

    def get_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent log entries"""
        db = self._get_db()
        try:
            logs = db.query(BackfillLog).order_by(
                BackfillLog.timestamp.desc()
            ).limit(limit).all()

            return [
                {
                    "id": log.id,
                    "timestamp": log.timestamp.isoformat(),
                    "level": log.level,
                    "message": log.message,
                    "details": json.loads(log.details) if log.details else None
                }
                for log in reversed(logs)  # Return in chronological order
            ]
        finally:
            db.close()

    def clear_logs(self):
        """Clear all log entries"""
        db = self._get_db()
        try:
            db.query(BackfillLog).delete()
            db.commit()
        finally:
            db.close()

    def reset_processed(self):
        """Reset processed emails tracking (for full re-backfill)"""
        db = self._get_db()
        try:
            db.query(ProcessedEmail).delete()
            db.commit()
            self._log("info", "Reset processed emails tracking")
        finally:
            db.close()

    async def start(self, days: int = 9999) -> Dict[str, Any]:
        """Start the backfill process"""
        if self._state.status == "running":
            return {"success": False, "error": "Backfill already running"}

        self._stop_requested = False
        self._task = asyncio.create_task(self._run_backfill(days))

        return {"success": True, "message": "Backfill started"}

    async def stop(self) -> Dict[str, Any]:
        """Stop the backfill process gracefully"""
        if self._state.status != "running":
            return {"success": False, "error": "Backfill not running"}

        self._stop_requested = True
        self._log("info", "Stop requested, finishing current email...")

        return {"success": True, "message": "Stop requested"}

    async def _run_backfill(self, days: int):
        """Main backfill loop"""
        settings = get_settings()

        if not all([settings.IMAP_SERVER, settings.IMAP_USERNAME, settings.IMAP_PASSWORD]):
            self._state.status = "error"
            self._state.error_message = "IMAP credentials not configured"
            self._log("error", "IMAP credentials not configured")
            return

        self._state = BackfillState(
            status="running",
            started_at=datetime.utcnow().isoformat()
        )
        self._log("info", f"Starting backfill for last {days} days")

        mail = None
        start_time = datetime.utcnow()
        processed_in_session = 0

        try:
            # Connect to IMAP
            self._log("info", f"Connecting to {settings.IMAP_SERVER}...")
            mail = imaplib.IMAP4_SSL(settings.IMAP_SERVER, settings.IMAP_PORT)
            mail.login(settings.IMAP_USERNAME, settings.IMAP_PASSWORD)
            mail.select('INBOX')

            # Search for DMARC emails in date range (server-side filtering)
            date_since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

            # Filter for emails with "dmarc" in subject - server-side filtering
            # This dramatically reduces the number of emails to download
            self._log("info", f"Searching for DMARC emails since {date_since}...")
            status, data = mail.search(None, f'SINCE {date_since}', 'SUBJECT "dmarc"')

            if status != 'OK':
                raise Exception("Failed to search mailbox")

            email_ids = data[0].split()
            self._state.total_emails = len(email_ids)

            self._log("info", f"Found {len(email_ids)} emails to process")

            report_store = ReportStore.get_instance()

            # Process each email
            for idx, email_id in enumerate(email_ids):
                if self._stop_requested:
                    self._log("info", "Backfill stopped by user")
                    self._state.status = "paused"
                    break

                try:
                    # Fetch just headers first to get Message-ID
                    status, header_data = mail.fetch(email_id, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM)])')
                    if status != 'OK':
                        continue

                    header_text = header_data[0][1].decode('utf-8', errors='replace')
                    message_id = self._extract_message_id(header_text)

                    if not message_id:
                        message_id = f"imap-uid-{email_id.decode()}"

                    # Check if already processed
                    if self._is_email_processed(message_id):
                        self._state.skipped_emails += 1
                        continue

                    # Fetch full email
                    status, msg_data = mail.fetch(email_id, '(RFC822)')
                    if status != 'OK':
                        continue

                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    # Update state
                    subject = self._decode_header(msg.get('Subject', ''))[:50]
                    self._state.current_email = f"{subject}..."
                    self._state.last_update = datetime.utcnow().isoformat()

                    # Check if it's a DMARC report
                    had_report = False
                    domain_found = None

                    if self._is_dmarc_email(msg):
                        report_result = self._process_attachments(msg, report_store)
                        if report_result:
                            had_report = True
                            domain_found = report_result.get('domain')
                            self._state.reports_found += 1
                            if domain_found and domain_found not in self._state.domains_found:
                                self._state.domains_found.append(domain_found)
                                self._log("success", f"New domain found: {domain_found}")

                    # Mark as processed
                    self._mark_email_processed(message_id, had_report, domain_found)
                    self._state.processed_emails += 1
                    processed_in_session += 1

                    # Calculate rate and ETA
                    elapsed = (datetime.utcnow() - start_time).total_seconds()
                    if elapsed > 0 and processed_in_session > 0:
                        self._state.rate = round(processed_in_session / elapsed, 2)
                        remaining = self._state.total_emails - self._state.processed_emails - self._state.skipped_emails
                        if self._state.rate > 0:
                            self._state.eta_seconds = int(remaining / self._state.rate)

                    # Log progress periodically
                    if processed_in_session % 100 == 0:
                        self._log("info", f"Progress: {self._state.progress_percent}% ({self._state.processed_emails + self._state.skipped_emails}/{self._state.total_emails})")

                except Exception as e:
                    self._state.errors += 1
                    self._mark_email_processed(message_id or f"error-{idx}", error=str(e))
                    logger.error(f"Error processing email {email_id}: {e}")

                # Small delay to avoid overwhelming the server
                await asyncio.sleep(0.01)

            # Completed
            if not self._stop_requested:
                self._state.status = "completed"
                self._log("success", f"Backfill completed! Processed {self._state.processed_emails} emails, found {self._state.reports_found} reports across {len(self._state.domains_found)} domains")

        except Exception as e:
            self._state.status = "error"
            self._state.error_message = str(e)
            self._log("error", f"Backfill failed: {e}")

        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except:
                    pass

    def _extract_message_id(self, header_text: str) -> Optional[str]:
        """Extract Message-ID from header text"""
        for line in header_text.split('\n'):
            if line.lower().startswith('message-id:'):
                return line.split(':', 1)[1].strip()
        return None

    def _decode_header(self, header: str) -> str:
        """Decode email header"""
        if not header:
            return ""
        decoded_parts = []
        for text, encoding in decode_header(header):
            if isinstance(text, bytes):
                decoded_parts.append(text.decode(encoding or 'utf-8', errors='replace'))
            else:
                decoded_parts.append(text)
        return " ".join(decoded_parts)

    def _is_dmarc_email(self, msg) -> bool:
        """Check if email might contain DMARC reports"""
        subject = self._decode_header(msg.get('Subject', '')).lower()
        from_addr = self._decode_header(msg.get('From', '')).lower()

        dmarc_keywords = ['dmarc', 'aggregate', 'report', 'rua']
        dmarc_senders = ['noreply@', 'dmarc-noreply@', 'postmaster@',
                        'microsoft.com', 'google.com', 'yahoo.com']

        if any(kw in subject for kw in dmarc_keywords):
            return True
        if any(sender in from_addr for sender in dmarc_senders):
            return True

        # Check for DMARC-like attachments
        for part in msg.walk():
            if part.get_content_disposition() == 'attachment':
                filename = part.get_filename() or ''
                if any(ext in filename.lower() for ext in ['.xml', '.zip', '.gz']):
                    return True

        return False

    def _process_attachments(self, msg, report_store) -> Optional[Dict]:
        """Process email attachments for DMARC reports"""
        for part in msg.walk():
            if part.get_content_disposition() != 'attachment':
                continue

            filename = part.get_filename()
            if not filename:
                continue

            filename = self._decode_header(filename)

            if not any(ext in filename.lower() for ext in ['.xml', '.zip', '.gz']):
                continue

            try:
                content = part.get_payload(decode=True)
                report = DMARCParser.parse_file(content, filename)
                report_store.add_report(report)

                return {
                    'domain': report.get('domain'),
                    'org_name': report.get('org_name'),
                    'records': len(report.get('records', []))
                }
            except Exception as e:
                logger.error(f"Error parsing attachment {filename}: {e}")

        return None


# Global instance
backfill_service = BackfillService.get_instance()
