from typing import Dict, List, Any, Optional
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.services.dmarc_parser import DMARCParser
from app.services.persistent_store import ReportStore

router = APIRouter()

class UploadResponse(BaseModel):
    """Response model for report upload"""
    success: bool
    domain: str
    message: str
    processed_records: int = 0  # Added this field to track processed records

class DomainSummary(BaseModel):
    """Domain summary response model"""
    domain: str
    total_count: int
    passed_count: int
    failed_count: int
    reports_processed: int
    compliance_rate: float

class ReportSummary(BaseModel):
    """DMARC report summary model"""
    report_id: str
    org_name: str
    begin_date: str
    end_date: str
    total_count: int
    passed_count: int
    failed_count: int

class PaginatedReportResponse(BaseModel):
    """Paginated reports response model"""
    total: int
    page: int
    page_size: int
    total_pages: int
    reports: List[ReportSummary]

class ReportRecordDetail(BaseModel):
    """Individual record within a report"""
    source_ip: str
    count: int
    disposition: str
    dkim: Optional[str] = None
    spf: Optional[str] = None
    header_from: Optional[str] = None
    envelope_from: Optional[str] = None

class ReportDetailResponse(BaseModel):
    """Full report detail response model"""
    report_id: str
    domain: str
    org_name: str
    begin_date: int
    end_date: int
    policy: Optional[str] = None
    subdomain_policy: Optional[str] = None
    adkim: Optional[str] = None
    aspf: Optional[str] = None
    percentage: Optional[int] = None
    processed_at: Optional[str] = None
    total_count: int
    passed_count: int
    failed_count: int
    pass_rate: float
    records: List[ReportRecordDetail]

@router.post("/upload", response_model=UploadResponse)
async def upload_report(file: UploadFile = File(...)):
    """
    Upload and process a DMARC aggregate report file (XML, ZIP, or GZIP)
    """
    try:
        # Read the file content
        file_content = await file.read()
        filename = file.filename
        
        # Parse the report
        parser = DMARCParser()
        report = parser.parse_file(file_content, filename)
        
        # Store the report
        store = ReportStore.get_instance()
        store.add_report(report)
        
        domain = report.get("domain", "unknown")
        processed_records = report.get("summary", {}).get("total_count", 0)
        
        return UploadResponse(
            success=True,
            domain=domain,
            message=f"Report processed successfully for domain {domain}",
            processed_records=processed_records
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Error processing report: {str(e)}"
        )

@router.get("/domains", response_model=List[str])
async def get_domains():
    """
    Get list of all domains with reports
    """
    store = ReportStore.get_instance()
    return store.get_domains()

@router.get("/domain/{domain}/summary", response_model=DomainSummary)
async def get_domain_summary(domain: str):
    """
    Get summary statistics for a specific domain
    """
    store = ReportStore.get_instance()
    summary = store.get_domain_summary(domain)
    
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No reports found for domain {domain}"
        )
    
    return DomainSummary(
        domain=domain,
        **summary
    )

@router.get("/summary", response_model=List[DomainSummary])
async def get_all_summaries():
    """
    Get summary statistics for all domains
    """
    store = ReportStore.get_instance()
    all_summaries = store.get_all_domain_summaries()
    
    return [
        DomainSummary(domain=domain, **summary)
        for domain, summary in all_summaries.items()
    ]

@router.get("/domain/{domain}/reports", response_model=List[ReportSummary])
async def get_domain_reports(domain: str):
    """
    Get all reports for a specific domain
    """
    store = ReportStore.get_instance()
    reports = store.get_domain_reports(domain)
    
    if not reports:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No reports found for domain {domain}"
        )
    
    return [
        ReportSummary(
            report_id=report.get("report_id", ""),
            org_name=report.get("org_name", ""),
            begin_date=report.get("begin_date", ""),
            end_date=report.get("end_date", ""),
            total_count=report.get("summary", {}).get("total_count", 0),
            passed_count=report.get("summary", {}).get("passed_count", 0),
            failed_count=report.get("summary", {}).get("failed_count", 0)
        )
        for report in reports
    ]

@router.get("/domain/{domain}/reports/paginated", response_model=PaginatedReportResponse)
async def get_domain_reports_paginated(
    domain: str,
    page: int = 1,
    page_size: int = 10,
    sort_by: str = "end_date",
    sort_order: str = "desc"
):
    """
    Get paginated reports for a specific domain with sorting options
    
    Args:
        domain: Domain name
        page: Page number (1-based)
        page_size: Number of reports per page
        sort_by: Field to sort by (report_id, org_name, begin_date, end_date, total_count)
        sort_order: Sort order (asc or desc)
    """
    store = ReportStore.get_instance()
    all_reports = store.get_domain_reports(domain)
    
    if not all_reports:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No reports found for domain {domain}"
        )
    
    # Apply sorting
    valid_sort_fields = ["report_id", "org_name", "begin_date", "end_date", "total_count"]
    sort_field = sort_by if sort_by in valid_sort_fields else "end_date"
    
    if sort_field == "total_count":
        all_reports.sort(
            key=lambda r: r.get("summary", {}).get("total_count", 0),
            reverse=(sort_order == "desc")
        )
    else:
        all_reports.sort(
            key=lambda r: r.get(sort_field, ""),
            reverse=(sort_order == "desc")
        )
    
    # Apply pagination
    total = len(all_reports)
    total_pages = (total + page_size - 1) // page_size
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_reports = all_reports[start_idx:end_idx]
    
    # Format reports
    report_entries = [
        ReportSummary(
            report_id=report.get("report_id", ""),
            org_name=report.get("org_name", ""),
            begin_date=report.get("begin_date", ""),
            end_date=report.get("end_date", ""),
            total_count=report.get("summary", {}).get("total_count", 0),
            passed_count=report.get("summary", {}).get("passed_count", 0),
            failed_count=report.get("summary", {}).get("failed_count", 0)
        )
        for report in paginated_reports
    ]
    
    return PaginatedReportResponse(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        reports=report_entries
    )


@router.get("/{report_id}", response_model=ReportDetailResponse)
async def get_report_detail(report_id: str):
    """
    Get detailed information for a specific report by its report_id
    """
    store = ReportStore.get_instance()
    report = store.get_report_by_id(report_id)

    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report with ID {report_id} not found"
        )

    return ReportDetailResponse(
        report_id=report.get("report_id", ""),
        domain=report.get("domain", "unknown"),
        org_name=report.get("org_name", ""),
        begin_date=report.get("begin_date", 0),
        end_date=report.get("end_date", 0),
        policy=report.get("policy"),
        subdomain_policy=report.get("subdomain_policy"),
        adkim=report.get("adkim"),
        aspf=report.get("aspf"),
        percentage=report.get("percentage"),
        processed_at=report.get("processed_at"),
        total_count=report.get("total_count", 0),
        passed_count=report.get("passed_count", 0),
        failed_count=report.get("failed_count", 0),
        pass_rate=report.get("pass_rate", 0),
        records=[
            ReportRecordDetail(
                source_ip=r.get("source_ip", ""),
                count=r.get("count", 0),
                disposition=r.get("disposition", "none"),
                dkim=r.get("dkim"),
                spf=r.get("spf"),
                header_from=r.get("header_from"),
                envelope_from=r.get("envelope_from")
            )
            for r in report.get("records", [])
        ]
    )