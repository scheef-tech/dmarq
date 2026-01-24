"""
Gmail API Client for fetching DMARC reports via OAuth2.

Implements the BaseSourceService interface for consistent handling
with other data source types.
"""
import base64
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from app.services.base_source import BaseSourceService, SourceStatus, FetchResult
from app.services.dmarc_parser import DMARCParser
from app.services.persistent_store import ReportStore

logger = logging.getLogger(__name__)

# Gmail API scopes needed
GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify'  # For marking as read
]


class GmailAPIClient(BaseSourceService):
    """
    Gmail API client for fetching DMARC reports via OAuth2.

    Supports:
    - OAuth2 authentication flow
    - Token refresh
    - Fetching emails with DMARC attachments
    - Parsing and storing DMARC reports
    """

    def __init__(self, source_id: int, config: Dict[str, Any]):
        """
        Initialize Gmail API client.

        Args:
            source_id: Database ID of the DataSource
            config: Configuration containing:
                - client_id: OAuth client ID
                - client_secret: OAuth client secret
                - refresh_token: OAuth refresh token (after auth flow)
                - access_token: Current access token (optional)
                - email: Gmail email address
        """
        super().__init__(source_id, config)

        self.client_id = config.get("client_id")
        self.client_secret = config.get("client_secret")
        self.refresh_token = config.get("refresh_token")
        self.access_token = config.get("access_token")
        self.email = config.get("email")

        self._service = None
        self._credentials = None
        self.report_store = ReportStore.get_instance()

    def _get_credentials(self):
        """Get or refresh OAuth credentials."""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            if self._credentials and self._credentials.valid:
                return self._credentials

            if self.refresh_token:
                self._credentials = Credentials(
                    token=self.access_token,
                    refresh_token=self.refresh_token,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    scopes=GMAIL_SCOPES
                )

                # Refresh if expired
                if self._credentials.expired:
                    self._credentials.refresh(Request())
                    self.access_token = self._credentials.token

                return self._credentials

            return None

        except Exception as e:
            logger.error(f"Error getting Gmail credentials: {e}")
            return None

    def _get_service(self):
        """Get Gmail API service."""
        if self._service:
            return self._service

        try:
            from googleapiclient.discovery import build

            credentials = self._get_credentials()
            if not credentials:
                return None

            self._service = build('gmail', 'v1', credentials=credentials)
            return self._service

        except Exception as e:
            logger.error(f"Error building Gmail service: {e}")
            return None

    def get_authorization_url(self, redirect_uri: str) -> str:
        """
        Generate OAuth authorization URL.

        Args:
            redirect_uri: Callback URL for OAuth

        Returns:
            Authorization URL to redirect user to
        """
        try:
            from google_auth_oauthlib.flow import Flow

            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token"
                    }
                },
                scopes=GMAIL_SCOPES
            )
            flow.redirect_uri = redirect_uri

            auth_url, _ = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true',
                prompt='consent'  # Force consent to get refresh token
            )

            return auth_url

        except Exception as e:
            logger.error(f"Error generating auth URL: {e}")
            raise

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """
        Exchange authorization code for tokens.

        Args:
            code: Authorization code from OAuth callback
            redirect_uri: Same redirect URI used in authorization

        Returns:
            Dictionary with access_token, refresh_token, and email
        """
        try:
            from google_auth_oauthlib.flow import Flow
            from googleapiclient.discovery import build

            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token"
                    }
                },
                scopes=GMAIL_SCOPES
            )
            flow.redirect_uri = redirect_uri

            flow.fetch_token(code=code)
            credentials = flow.credentials

            # Get the user's email
            service = build('gmail', 'v1', credentials=credentials)
            profile = service.users().getProfile(userId='me').execute()
            email = profile.get('emailAddress')

            return {
                "access_token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "email": email
            }

        except Exception as e:
            logger.error(f"Error exchanging code: {e}")
            raise

    def test_connection(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Test the Gmail API connection.

        Returns:
            Tuple of (success, message, stats)
        """
        if not self.refresh_token:
            return False, "OAuth not configured - please complete authorization", {}

        try:
            service = self._get_service()
            if not service:
                return False, "Failed to initialize Gmail service", {}

            # Get profile info
            profile = service.users().getProfile(userId='me').execute()
            email = profile.get('emailAddress')
            total_messages = profile.get('messagesTotal', 0)

            # Search for DMARC emails
            dmarc_query = 'subject:DMARC OR subject:"Report domain"'
            dmarc_results = service.users().messages().list(
                userId='me',
                q=dmarc_query,
                maxResults=1
            ).execute()
            dmarc_count = dmarc_results.get('resultSizeEstimate', 0)

            stats = {
                "email": email,
                "total_messages": total_messages,
                "dmarc_count": dmarc_count,
                "timestamp": datetime.now().isoformat()
            }

            return True, f"Connected to {email}", stats

        except Exception as e:
            logger.error(f"Gmail connection test failed: {e}")
            return False, f"Connection failed: {str(e)}", {}

    def fetch_reports(self, days: int = 7) -> FetchResult:
        """
        Fetch DMARC reports from Gmail.

        Args:
            days: Number of days to look back

        Returns:
            FetchResult with operation details
        """
        if not self.refresh_token:
            return FetchResult(
                success=False,
                message="OAuth not configured"
            )

        try:
            service = self._get_service()
            if not service:
                return FetchResult(
                    success=False,
                    message="Failed to initialize Gmail service"
                )

            # Build search query
            date_after = (datetime.now() - timedelta(days=days)).strftime('%Y/%m/%d')
            query = f'(subject:DMARC OR subject:"Report domain") after:{date_after}'

            # Get matching messages
            results = service.users().messages().list(
                userId='me',
                q=query
            ).execute()

            messages = results.get('messages', [])
            processed = 0
            reports_found = 0
            errors = []
            domains_before = set(self.report_store.get_domains())

            for msg in messages:
                try:
                    # Get full message
                    full_msg = service.users().messages().get(
                        userId='me',
                        id=msg['id'],
                        format='full'
                    ).execute()

                    # Process attachments
                    payload = full_msg.get('payload', {})
                    parts = payload.get('parts', [])

                    for part in parts:
                        filename = part.get('filename', '')
                        if not filename:
                            continue

                        # Check for DMARC report files
                        if any(ext in filename.lower() for ext in ['.xml', '.zip', '.gz']):
                            # Get attachment data
                            attachment_id = part.get('body', {}).get('attachmentId')
                            if attachment_id:
                                attachment = service.users().messages().attachments().get(
                                    userId='me',
                                    messageId=msg['id'],
                                    id=attachment_id
                                ).execute()

                                data = base64.urlsafe_b64decode(attachment['data'])

                                try:
                                    report = DMARCParser.parse_file(data, filename)
                                    self.report_store.add_report(report)
                                    reports_found += 1
                                except Exception as e:
                                    errors.append(f"Error parsing {filename}: {str(e)}")

                    # Mark as read
                    service.users().messages().modify(
                        userId='me',
                        id=msg['id'],
                        body={'removeLabelIds': ['UNREAD']}
                    ).execute()

                    processed += 1

                except Exception as e:
                    errors.append(f"Error processing message {msg['id']}: {str(e)}")

            # Identify new domains
            domains_after = set(self.report_store.get_domains())
            new_domains = list(domains_after - domains_before)

            return FetchResult(
                success=True,
                message=f"Processed {processed} emails, found {reports_found} reports",
                processed=processed,
                reports_found=reports_found,
                new_domains=new_domains,
                errors=errors
            )

        except Exception as e:
            logger.error(f"Gmail fetch error: {e}")
            return FetchResult(
                success=False,
                message=str(e),
                errors=[str(e)]
            )

    def get_status(self) -> SourceStatus:
        """Get current Gmail connection status."""
        success, message, stats = self.test_connection()

        return SourceStatus(
            connected=success,
            message=message,
            stats=stats,
            error=None if success else message
        )

    def _get_required_config_fields(self) -> List[str]:
        """Get required configuration fields for Gmail API."""
        return ["client_id", "client_secret"]


# Register the service with DataSourceManager
def _register_gmail_service():
    """Register GmailAPIClient with the DataSourceManager."""
    try:
        from app.services.datasource_manager import DataSourceManager
        from app.models.datasource import DataSourceType
        DataSourceManager.register_service(DataSourceType.GMAIL_API, GmailAPIClient)
    except ImportError:
        pass

_register_gmail_service()
