from fastapi import APIRouter
from datetime import datetime

from app.api.api_v1.endpoints.setup import setup_status
from app.core.config import get_settings
from app.services.persistent_store import ReportStore

router = APIRouter()


@router.get("/health", status_code=200)
async def health_check():
    """
    Health check endpoint to verify API status.
    For Milestone 1, this simply returns status information without checking a database.
    """
    return {
        "status": "ok",
        "version": "0.1.0",
        "service": "dmarq",
        "is_setup_complete": setup_status["is_setup_complete"]
    }


@router.get("/debug", status_code=200)
async def debug_info():
    """
    Debug endpoint showing internal state for troubleshooting.
    """
    settings = get_settings()
    store = ReportStore.get_instance()

    # Get domain stats
    domains = store.get_domains()
    domain_stats = {}
    total_reports = 0
    for domain in domains:
        summary = store.get_domain_summary(domain)
        domain_stats[domain] = {
            "reports": summary.get("reports_processed", 0),
            "messages": summary.get("total_count", 0),
            "compliance": summary.get("compliance_rate", 0),
            "policy": summary.get("policy", "unknown")
        }
        total_reports += summary.get("reports_processed", 0)

    # Check database connectivity
    db_status = "ok"
    try:
        from sqlalchemy import text
        from app.core.database import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "database": {
            "status": db_status,
            "url_configured": bool(settings.DATABASE_URL),
            "url_prefix": settings.DATABASE_URL[:20] + "..." if settings.DATABASE_URL else None
        },
        "imap": {
            "configured": all([settings.IMAP_SERVER, settings.IMAP_USERNAME, settings.IMAP_PASSWORD]),
            "server": settings.IMAP_SERVER,
            "username": settings.IMAP_USERNAME
        },
        "store": {
            "domain_count": len(domains),
            "total_reports": total_reports,
            "domains": domain_stats
        }
    }