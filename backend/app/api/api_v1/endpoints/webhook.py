import email
import base64
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel

from app.services.dmarc_parser import DMARCParser
from app.services.persistent_store import ReportStore
from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

settings = get_settings()


class EmailWebhookPayload(BaseModel):
    """Payload from Cloudflare Email Worker"""
    raw_email: str  # Base64 encoded raw email
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    subject: Optional[str] = None


def decode_email_header(header: str) -> str:
    """Decode an email header that might contain non-ASCII characters"""
    from email.header import decode_header
    decoded_parts = []
    for text, encoding in decode_header(header):
        if isinstance(text, bytes):
            decoded_parts.append(text.decode(encoding or 'utf-8', errors='replace'))
        else:
            decoded_parts.append(text)
    return " ".join(decoded_parts)


def process_email_attachments(msg: email.message.Message) -> int:
    """
    Process email attachments that might be DMARC reports

    Returns:
        Number of DMARC reports found and processed
    """
    reports_found = 0
    report_store = ReportStore.get_instance()

    for part in msg.walk():
        content_disposition = part.get_content_disposition()

        if content_disposition == 'attachment':
            filename = part.get_filename()
            if filename:
                filename = decode_email_header(filename)

                # Check if it's a likely DMARC report file
                if (filename.lower().endswith('.xml') or
                    filename.lower().endswith('.zip') or
                    filename.lower().endswith('.gz') or
                    filename.lower().endswith('.gzip')):

                    try:
                        content = part.get_payload(decode=True)
                        report = DMARCParser.parse_file(content, filename)
                        report_store.add_report(report)
                        reports_found += 1
                        logger.info(f"Webhook: Successfully processed DMARC report: {filename}")
                    except Exception as e:
                        logger.error(f"Webhook: Error processing attachment {filename}: {str(e)}")

    return reports_found


@router.post("/email")
async def receive_email(
    payload: EmailWebhookPayload,
    x_webhook_secret: Optional[str] = Header(None)
):
    """
    Receive email from Cloudflare Email Worker

    The raw email should be base64 encoded in the payload.
    """
    # Verify webhook secret if configured
    webhook_secret = getattr(settings, 'WEBHOOK_SECRET', None)
    if webhook_secret and x_webhook_secret != webhook_secret:
        logger.warning("Webhook: Invalid or missing webhook secret")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        # Decode the base64 email
        raw_email = base64.b64decode(payload.raw_email)

        # Parse the email
        msg = email.message_from_bytes(raw_email)

        # Extract subject for logging
        subject = payload.subject or ""
        if not subject and "Subject" in msg:
            subject = decode_email_header(msg["Subject"])

        logger.info(f"Webhook: Received email - Subject: {subject}")

        # Process attachments
        reports_found = process_email_attachments(msg)

        return {
            "success": True,
            "reports_found": reports_found,
            "subject": subject
        }

    except Exception as e:
        logger.error(f"Webhook: Error processing email: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Error processing email: {str(e)}")


@router.post("/email/raw")
async def receive_raw_email(
    request: Request,
    x_webhook_secret: Optional[str] = Header(None)
):
    """
    Receive raw email bytes directly (alternative endpoint)

    Content-Type should be application/octet-stream
    """
    webhook_secret = getattr(settings, 'WEBHOOK_SECRET', None)
    if webhook_secret and x_webhook_secret != webhook_secret:
        logger.warning("Webhook: Invalid or missing webhook secret")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        raw_email = await request.body()

        # Parse the email
        msg = email.message_from_bytes(raw_email)

        subject = ""
        if "Subject" in msg:
            subject = decode_email_header(msg["Subject"])

        logger.info(f"Webhook: Received raw email - Subject: {subject}")

        # Process attachments
        reports_found = process_email_attachments(msg)

        return {
            "success": True,
            "reports_found": reports_found,
            "subject": subject
        }

    except Exception as e:
        logger.error(f"Webhook: Error processing raw email: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Error processing email: {str(e)}")
