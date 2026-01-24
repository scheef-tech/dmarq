"""
API endpoints for backfill management
"""
from typing import List, Dict, Any
from fastapi import APIRouter, Query, BackgroundTasks

from app.services.backfill_service import BackfillService

router = APIRouter()


@router.get("/status")
async def get_backfill_status():
    """
    Get current backfill status with progress information
    """
    service = BackfillService.get_instance()
    return service.get_state()


@router.post("/start")
async def start_backfill(
    background_tasks: BackgroundTasks,
    days: int = Query(9999, description="Number of days to look back")
):
    """
    Start the backfill process. Will resume from where it left off.
    """
    service = BackfillService.get_instance()

    # Run in background
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(service.start(days=days))

    return {"success": True, "message": f"Backfill started for last {days} days"}


@router.post("/stop")
async def stop_backfill():
    """
    Stop the backfill process gracefully (finishes current email)
    """
    service = BackfillService.get_instance()
    return await service.stop()


@router.get("/logs")
async def get_backfill_logs(
    limit: int = Query(100, description="Maximum number of log entries to return")
):
    """
    Get recent backfill log entries
    """
    service = BackfillService.get_instance()
    return {"logs": service.get_logs(limit=limit)}


@router.post("/logs/clear")
async def clear_backfill_logs():
    """
    Clear all backfill log entries
    """
    service = BackfillService.get_instance()
    service.clear_logs()
    return {"success": True, "message": "Logs cleared"}


@router.post("/reset")
async def reset_backfill():
    """
    Reset the processed emails tracking (for a complete re-backfill).
    This does NOT delete the actual DMARC reports, only the tracking of which emails have been processed.
    """
    service = BackfillService.get_instance()

    if service.get_state()['status'] == 'running':
        return {"success": False, "error": "Cannot reset while backfill is running"}

    service.reset_processed()
    return {"success": True, "message": "Processed emails tracking has been reset"}
