"""
API endpoints for managing data sources (IMAP, Gmail API, Cloudflare).
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, status, Path, Query, BackgroundTasks
from pydantic import BaseModel, Field

from app.models.datasource import DataSourceType, DataSourceStatus
from app.services.datasource_manager import get_datasource_manager

router = APIRouter()


# ============================================================================
# Pydantic Schemas
# ============================================================================

class IMAPConfig(BaseModel):
    """Configuration for IMAP data source"""
    server: str = Field(..., description="IMAP server hostname")
    port: int = Field(993, description="IMAP server port")
    username: str = Field(..., description="IMAP username/email")
    password: str = Field(..., description="IMAP password")
    ssl: bool = Field(True, description="Use SSL/TLS")


class GmailAPIConfig(BaseModel):
    """Configuration for Gmail API data source"""
    client_id: Optional[str] = Field(None, description="OAuth client ID")
    client_secret: Optional[str] = Field(None, description="OAuth client secret")
    refresh_token: Optional[str] = Field(None, description="OAuth refresh token")
    email: Optional[str] = Field(None, description="Gmail email address")


class CloudflareConfig(BaseModel):
    """Configuration for Cloudflare data source"""
    api_token: str = Field(..., description="Cloudflare API token")
    account_id: Optional[str] = Field(None, description="Cloudflare account ID")
    account_name: str = Field(..., description="Account name for identification")


class CreateSourceRequest(BaseModel):
    """Request to create a new data source"""
    type: str = Field(..., description="Source type: imap, gmail_api, or cloudflare")
    name: str = Field(..., description="User-friendly name for the source")
    config: Dict[str, Any] = Field(..., description="Source-specific configuration")


class UpdateSourceRequest(BaseModel):
    """Request to update a data source"""
    name: Optional[str] = Field(None, description="New name for the source")
    config: Optional[Dict[str, Any]] = Field(None, description="Updated configuration")


class SourceResponse(BaseModel):
    """Response containing source information"""
    id: int
    type: str
    name: str
    status: str
    error_message: Optional[str] = None
    last_check: Optional[str] = None
    last_sync: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TestConnectionResponse(BaseModel):
    """Response from connection test"""
    success: bool
    message: str
    stats: Dict[str, Any] = {}
    status: Optional[str] = None


class FetchResponse(BaseModel):
    """Response from fetch/sync operation"""
    success: bool
    message: str
    processed: int = 0
    reports_found: int = 0
    new_domains: List[str] = []
    errors: List[str] = []


class LogEntry(BaseModel):
    """Log entry for a data source"""
    id: int
    level: str
    message: str
    details: Optional[Dict[str, Any]] = None
    timestamp: str


class LogsResponse(BaseModel):
    """Response containing log entries"""
    logs: List[LogEntry]
    total: int


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/", response_model=List[SourceResponse])
async def list_sources(
    type: Optional[str] = Query(None, description="Filter by source type")
):
    """
    List all data sources.

    Optionally filter by type (imap, gmail_api, cloudflare).
    """
    manager = get_datasource_manager()

    source_type = None
    if type:
        try:
            source_type = DataSourceType(type)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid source type: {type}. Must be one of: imap, gmail_api, cloudflare"
            )

    sources = manager.list_sources(source_type)
    return [SourceResponse(**s) for s in sources]


@router.post("/", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(request: CreateSourceRequest):
    """
    Create a new data source.

    The configuration will be encrypted before storage.
    """
    manager = get_datasource_manager()

    # Validate source type
    try:
        source_type = DataSourceType(request.type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid source type: {request.type}. Must be one of: imap, gmail_api, cloudflare"
        )

    # Validate required config fields based on type
    required_fields = {
        DataSourceType.IMAP: ["server", "username", "password"],
        DataSourceType.GMAIL_API: [],  # OAuth flow handles this
        DataSourceType.CLOUDFLARE: ["api_token", "account_name"],
    }

    missing = [f for f in required_fields.get(source_type, []) if not request.config.get(f)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required config fields: {', '.join(missing)}"
        )

    try:
        result = manager.create_source(source_type, request.name, request.config)
        return SourceResponse(**result)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create source: {str(e)}"
        )


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(source_id: int = Path(..., description="Source ID")):
    """
    Get a specific data source by ID.
    """
    manager = get_datasource_manager()
    source = manager.get_source(source_id)

    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    return SourceResponse(**source)


@router.put("/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: int = Path(..., description="Source ID"),
    request: UpdateSourceRequest = None
):
    """
    Update a data source.

    You can update the name, configuration, or both.
    Updating the configuration will reset the status to 'pending'.
    """
    manager = get_datasource_manager()

    result = manager.update_source(
        source_id,
        name=request.name if request else None,
        config=request.config if request else None
    )

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    return SourceResponse(**result)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: int = Path(..., description="Source ID")):
    """
    Delete a data source and all its logs.
    """
    manager = get_datasource_manager()
    deleted = manager.delete_source(source_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    return None


@router.post("/{source_id}/test", response_model=TestConnectionResponse)
async def test_connection(source_id: int = Path(..., description="Source ID")):
    """
    Test the connection to a data source.

    Updates the source status based on the result.
    """
    manager = get_datasource_manager()

    # Check source exists
    source = manager.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    result = manager.test_connection(source_id)
    return TestConnectionResponse(**result)


@router.get("/{source_id}/logs", response_model=LogsResponse)
async def get_source_logs(
    source_id: int = Path(..., description="Source ID"),
    limit: int = Query(100, description="Maximum number of logs to return")
):
    """
    Get log entries for a data source.
    """
    manager = get_datasource_manager()

    # Check source exists
    source = manager.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    logs = manager.get_logs(source_id, limit)
    return LogsResponse(logs=[LogEntry(**log) for log in logs], total=len(logs))


@router.delete("/{source_id}/logs", status_code=status.HTTP_204_NO_CONTENT)
async def clear_source_logs(source_id: int = Path(..., description="Source ID")):
    """
    Clear all logs for a data source.
    """
    manager = get_datasource_manager()

    # Check source exists
    source = manager.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    manager.clear_logs(source_id)
    return None


@router.post("/{source_id}/sync", response_model=FetchResponse)
async def sync_source(
    source_id: int = Path(..., description="Source ID"),
    background_tasks: BackgroundTasks = None
):
    """
    Sync domains from a Cloudflare data source.

    Only applicable to Cloudflare sources.
    """
    manager = get_datasource_manager()

    source = manager.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    if source["type"] != DataSourceType.CLOUDFLARE.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Domain sync is only available for Cloudflare sources"
        )

    result = manager.sync_domains(source_id)
    return FetchResponse(**result)


@router.post("/{source_id}/backfill", response_model=FetchResponse)
async def backfill_source(
    source_id: int = Path(..., description="Source ID"),
    days: int = Query(9999, description="Number of days to look back")
):
    """
    Backfill DMARC reports from an IMAP or Gmail API source.

    Not applicable to Cloudflare sources.
    """
    manager = get_datasource_manager()

    source = manager.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    if source["type"] == DataSourceType.CLOUDFLARE.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Backfill is not available for Cloudflare sources"
        )

    result = manager.fetch_reports(source_id, days)
    return FetchResponse(**result)


# ============================================================================
# OAuth Endpoints (for Gmail API)
# ============================================================================

@router.get("/{source_id}/oauth/start")
async def start_oauth(
    source_id: int = Path(..., description="Source ID"),
    redirect_uri: str = Query(..., description="OAuth redirect URI")
):
    """
    Start the OAuth flow for a Gmail API source.

    Returns the authorization URL to redirect the user to.
    """
    manager = get_datasource_manager()

    source = manager.get_source(source_id, include_config=True)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    if source["type"] != DataSourceType.GMAIL_API.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth is only available for Gmail API sources"
        )

    # Get the Gmail client service
    service = manager.get_service(source_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gmail API service not available"
        )

    # Generate OAuth URL
    try:
        auth_url = service.get_authorization_url(redirect_uri)
        return {"authorization_url": auth_url, "source_id": source_id}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate OAuth URL: {str(e)}"
        )


@router.post("/{source_id}/oauth/callback")
async def oauth_callback(
    source_id: int = Path(..., description="Source ID"),
    code: str = Query(..., description="Authorization code from OAuth callback"),
    redirect_uri: str = Query(..., description="OAuth redirect URI used in start")
):
    """
    Complete the OAuth flow for a Gmail API source.

    Exchanges the authorization code for tokens and stores them.
    """
    manager = get_datasource_manager()

    source = manager.get_source(source_id, include_config=True)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found"
        )

    if source["type"] != DataSourceType.GMAIL_API.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth callback is only for Gmail API sources"
        )

    service = manager.get_service(source_id)
    if not service:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gmail API service not available"
        )

    try:
        # Exchange code for tokens
        tokens = service.exchange_code(code, redirect_uri)

        # Update the source config with the new tokens
        config = source.get("config", {})
        config["refresh_token"] = tokens.get("refresh_token")
        config["access_token"] = tokens.get("access_token")
        config["email"] = tokens.get("email")

        manager.update_source(source_id, config=config)

        # Test the new connection
        test_result = manager.test_connection(source_id)

        return {
            "success": True,
            "message": "OAuth completed successfully",
            "email": tokens.get("email"),
            "connection_status": test_result.get("status")
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth callback failed: {str(e)}"
        )
