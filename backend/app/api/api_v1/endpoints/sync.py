"""
API endpoints for syncing domains from Cloudflare
"""
from typing import Dict, List, Any, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.services.cloudflare_sync import get_sync_service

router = APIRouter()


class ZoneData(BaseModel):
    """Zone data from Cloudflare"""
    id: str
    name: str
    status: Optional[str] = None
    paused: Optional[bool] = False


class SyncZonesRequest(BaseModel):
    """Request to sync zones from a Cloudflare account"""
    account_name: str
    zones: List[ZoneData]


class SyncZonesResponse(BaseModel):
    """Response from zone sync"""
    success: bool
    account: str
    created: int
    updated: int
    total: int
    errors: List[str] = []


class DNSRecord(BaseModel):
    """DNS record from Cloudflare"""
    name: str
    type: str
    content: str


class UpdateDNSRequest(BaseModel):
    """Request to update DNS records for a domain"""
    domain_name: str
    dns_records: List[DNSRecord]


class DomainStatusUpdate(BaseModel):
    """Domain status update"""
    domain_name: str
    active: Optional[bool] = None
    dmarc_policy: Optional[str] = None
    spf_record: Optional[str] = None
    cloudflare_account: Optional[str] = None


class BulkUpdateRequest(BaseModel):
    """Request for bulk domain status update"""
    domains: List[DomainStatusUpdate]


class SyncStatusResponse(BaseModel):
    """Current sync status"""
    total_domains: int
    synced_domains: int
    with_dmarc_policy: int
    with_spf_record: int
    accounts: Dict[str, int]


@router.post("/zones", response_model=SyncZonesResponse)
async def sync_zones(request: SyncZonesRequest):
    """
    Sync zones from a Cloudflare account.

    This endpoint receives zone data (typically fetched via MCP or Cloudflare API)
    and stores/updates the domains in the database.
    """
    sync_service = get_sync_service()

    zones_data = [{"id": z.id, "name": z.name, "status": z.status, "paused": z.paused} for z in request.zones]
    result = sync_service.sync_zones(zones_data, request.account_name)

    return SyncZonesResponse(**result)


@router.post("/dns")
async def update_dns_records(request: UpdateDNSRequest):
    """
    Update DNS records (DMARC, SPF) for a specific domain.

    This endpoint receives DNS TXT records and extracts DMARC policy
    and SPF records to store in the database.
    """
    sync_service = get_sync_service()

    dns_records = [{"name": r.name, "type": r.type, "content": r.content} for r in request.dns_records]
    result = sync_service.update_dns_records(request.domain_name, dns_records)

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Failed to update DNS records")
        )

    return result


@router.post("/bulk")
async def bulk_update_domains(request: BulkUpdateRequest):
    """
    Bulk update domain status and DNS information.

    Useful for updating multiple domains at once with their
    active/parked status, DMARC policy, and SPF records.
    """
    sync_service = get_sync_service()

    domains_data = [
        {
            "domain_name": d.domain_name,
            "active": d.active,
            "dmarc_policy": d.dmarc_policy,
            "spf_record": d.spf_record,
            "cloudflare_account": d.cloudflare_account
        }
        for d in request.domains
    ]

    result = sync_service.bulk_update_domain_status(domains_data)

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Failed to update domains")
        )

    return result


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status():
    """
    Get current sync status showing domain counts and sync state.
    """
    sync_service = get_sync_service()
    return sync_service.get_sync_status()


@router.get("/domains")
async def get_all_domains():
    """
    Get all domains from the database with their sync status.
    """
    sync_service = get_sync_service()
    domains = sync_service.get_all_domains()
    return {"domains": domains, "total": len(domains)}


class SimpleImportRequest(BaseModel):
    """Simple domain import - one domain per line with optional account"""
    domains_text: str  # Format: "domain.com,account_name" or just "domain.com" per line


@router.post("/import")
async def import_domains_simple(request: SimpleImportRequest):
    """
    Simple domain import from text.

    Format: one domain per line, optionally with account name after comma.
    Example:
        scheef.tech,scheef
        perfahl.eu,scheef
        onebasegroup.com,onebase-llc
        myparked.com,scheef,parked

    Third field can be 'parked' or 'active' (default: active)
    """
    sync_service = get_sync_service()

    domains_data = []
    lines = request.domains_text.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        parts = [p.strip() for p in line.split(',')]
        domain_name = parts[0] if parts else None

        if not domain_name:
            continue

        account_name = parts[1] if len(parts) > 1 else None
        status = parts[2].lower() if len(parts) > 2 else 'active'
        is_active = status != 'parked'

        domains_data.append({
            "domain_name": domain_name,
            "active": is_active,
            "cloudflare_account": account_name
        })

    if not domains_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid domains found in input"
        )

    result = sync_service.bulk_update_domain_status(domains_data)

    return {
        "success": result.get("success", False),
        "imported": result.get("created", 0) + result.get("updated", 0),
        "created": result.get("created", 0),
        "updated": result.get("updated", 0),
        "errors": result.get("errors", [])
    }
