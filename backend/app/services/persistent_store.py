"""
Persistent database-backed store for DMARC reports
Replaces in-memory ReportStore with PostgreSQL storage
"""
from typing import Dict, List, Any, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func, case

from app.core.database import SessionLocal, engine, Base
from app.models.user import User  # Import User first for foreign key dependency
from app.models.domain import Domain
from app.models.report import DMARCReport, ReportRecord
from app.models.datasource import DataSource, DataSourceLog  # Multi-source support


class PersistentReportStore:
    """
    Database-backed store for DMARC reports
    """

    _instance = None

    @classmethod
    def get_instance(cls) -> 'PersistentReportStore':
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = PersistentReportStore()
        return cls._instance

    def __init__(self):
        """Initialize database tables"""
        Base.metadata.create_all(bind=engine)

    def _get_db(self) -> Session:
        """Get a database session"""
        return SessionLocal()

    def add_report(self, report: Dict[str, Any]) -> None:
        """
        Add a new report to the database

        Args:
            report: Parsed DMARC report from DMARCParser
        """
        db = self._get_db()
        try:
            domain_name = report.get("domain", "unknown")

            # Get or create domain
            domain = db.query(Domain).filter(Domain.name == domain_name).first()
            if not domain:
                # Extract policy string - parser returns dict like {'p': 'quarantine', 'sp': '...', 'pct': '...'}
                policy_data = report.get("policy")
                policy_str = policy_data.get("p", "none") if isinstance(policy_data, dict) else policy_data

                domain = Domain(
                    name=domain_name,
                    dmarc_policy=policy_str,
                    active=True,
                    verified=True
                )
                db.add(domain)
                db.flush()

            # Check if report already exists (by report_id)
            report_id = report.get("report_id", f"unknown_{datetime.utcnow().timestamp()}")
            existing = db.query(DMARCReport).filter(
                DMARCReport.report_id == report_id,
                DMARCReport.domain_id == domain.id
            ).first()

            if existing:
                # Skip duplicate reports
                return

            # Create report
            # Extract policy string from dict if needed
            policy_data = report.get("policy")
            report_policy_str = policy_data.get("p", "none") if isinstance(policy_data, dict) else policy_data

            db_report = DMARCReport(
                domain_id=domain.id,
                report_id=report_id,
                org_name=report.get("org_name", "Unknown"),
                begin_date=report.get("begin_timestamp", 0),  # Use timestamp, not ISO string
                end_date=report.get("end_timestamp", 0),      # Use timestamp, not ISO string
                policy=report_policy_str,
                processed_at=datetime.utcnow()
            )
            db.add(db_report)
            db.flush()

            # Add records
            for record in report.get("records", []):
                db_record = ReportRecord(
                    report_id=db_report.id,
                    source_ip=record.get("source_ip", "unknown"),
                    count=record.get("count", 0),
                    disposition=record.get("disposition", "none"),
                    dkim=record.get("dkim_result", "unknown"),
                    spf=record.get("spf_result", "unknown"),
                    header_from=record.get("header_from")
                )
                db.add(db_record)

            # Update domain policy if changed
            if report.get("policy"):
                policy_data = report.get("policy")
                policy_str = policy_data.get("p", "none") if isinstance(policy_data, dict) else policy_data
                domain.dmarc_policy = policy_str

            db.commit()
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    def get_domains(self) -> List[str]:
        """Get list of all domains with reports"""
        db = self._get_db()
        try:
            domains = db.query(Domain.name).filter(Domain.active == True).all()
            return [d[0] for d in domains]
        finally:
            db.close()

    def get_domain_summary(self, domain_name: str) -> Dict[str, Any]:
        """Get summary statistics for a domain"""
        db = self._get_db()
        try:
            domain = db.query(Domain).filter(Domain.name == domain_name).first()
            if not domain:
                return {}

            # Calculate stats from records
            stats = db.query(
                func.sum(ReportRecord.count).label('total'),
                func.sum(
                    case(
                        ((ReportRecord.dkim == 'pass') | (ReportRecord.spf == 'pass'), ReportRecord.count),
                        else_=0
                    )
                ).label('passed')
            ).join(DMARCReport).filter(DMARCReport.domain_id == domain.id).first()

            total_count = stats.total or 0
            passed_count = stats.passed or 0
            failed_count = total_count - passed_count

            report_count = db.query(func.count(DMARCReport.id)).filter(
                DMARCReport.domain_id == domain.id
            ).scalar() or 0

            compliance_rate = 0
            if total_count > 0:
                compliance_rate = round((passed_count / total_count) * 100, 1)

            return {
                "total_count": total_count,
                "passed_count": passed_count,
                "failed_count": failed_count,
                "reports_processed": report_count,
                "compliance_rate": compliance_rate,
                "policy": domain.dmarc_policy or "unknown"
            }
        finally:
            db.close()

    def get_all_domain_summaries(self) -> Dict[str, Dict[str, Any]]:
        """Get summary statistics for all domains"""
        domains = self.get_domains()
        return {domain: self.get_domain_summary(domain) for domain in domains}

    def get_domain_reports(self, domain_name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all reports for a domain"""
        db = self._get_db()
        try:
            domain = db.query(Domain).filter(Domain.name == domain_name).first()
            if not domain:
                return []

            query = db.query(DMARCReport).filter(
                DMARCReport.domain_id == domain.id
            ).order_by(DMARCReport.end_date.desc())

            if limit:
                query = query.limit(limit)

            reports = []
            for report in query.all():
                # Calculate pass rate for this report
                stats = db.query(
                    func.sum(ReportRecord.count).label('total'),
                    func.sum(
                        case(
                            ((ReportRecord.dkim == 'pass') | (ReportRecord.spf == 'pass'), ReportRecord.count),
                            else_=0
                        )
                    ).label('passed')
                ).filter(ReportRecord.report_id == report.id).first()

                total = stats.total or 0
                passed = stats.passed or 0
                pass_rate = round((passed / total) * 100, 1) if total > 0 else 0

                reports.append({
                    "report_id": report.report_id,
                    "org_name": report.org_name,
                    "begin_date": report.begin_date,
                    "end_date": report.end_date,
                    "policy": report.policy,
                    "total_count": total,
                    "pass_rate": pass_rate
                })

            return reports
        finally:
            db.close()

    def get_domain_sources(self, domain_name: str, days: int = 30) -> List[Dict[str, Any]]:
        """Get sending sources for a domain"""
        db = self._get_db()
        try:
            domain = db.query(Domain).filter(Domain.name == domain_name).first()
            if not domain:
                return []

            # Aggregate by source IP
            sources = db.query(
                ReportRecord.source_ip,
                func.sum(ReportRecord.count).label('total_count'),
                ReportRecord.spf,
                ReportRecord.dkim,
                ReportRecord.disposition
            ).join(DMARCReport).filter(
                DMARCReport.domain_id == domain.id
            ).group_by(
                ReportRecord.source_ip,
                ReportRecord.spf,
                ReportRecord.dkim,
                ReportRecord.disposition
            ).order_by(func.sum(ReportRecord.count).desc()).all()

            return [
                {
                    "source_ip": s.source_ip,
                    "count": s.total_count,
                    "spf_result": s.spf or "unknown",
                    "dkim_result": s.dkim or "unknown",
                    "disposition": s.disposition or "none"
                }
                for s in sources
            ]
        finally:
            db.close()

    def get_report_by_id(self, report_id: str) -> Dict[str, Any]:
        """Get a single report with all its records by report_id"""
        db = self._get_db()
        try:
            report = db.query(DMARCReport).filter(DMARCReport.report_id == report_id).first()
            if not report:
                return {}

            # Get domain name
            domain = db.query(Domain).filter(Domain.id == report.domain_id).first()
            domain_name = domain.name if domain else "unknown"

            # Get all records for this report
            records = db.query(ReportRecord).filter(ReportRecord.report_id == report.id).all()

            # Calculate stats
            total_count = sum(r.count for r in records)
            passed_count = sum(r.count for r in records if r.dkim == 'pass' or r.spf == 'pass')
            failed_count = total_count - passed_count
            pass_rate = round((passed_count / total_count) * 100, 1) if total_count > 0 else 0

            return {
                "report_id": report.report_id,
                "domain": domain_name,
                "org_name": report.org_name,
                "begin_date": report.begin_date,
                "end_date": report.end_date,
                "policy": report.policy,
                "subdomain_policy": report.subdomain_policy,
                "adkim": report.adkim,
                "aspf": report.aspf,
                "percentage": report.percentage,
                "processed_at": report.processed_at.isoformat() if report.processed_at else None,
                "total_count": total_count,
                "passed_count": passed_count,
                "failed_count": failed_count,
                "pass_rate": pass_rate,
                "records": [
                    {
                        "source_ip": r.source_ip,
                        "count": r.count,
                        "disposition": r.disposition,
                        "dkim": r.dkim,
                        "spf": r.spf,
                        "header_from": r.header_from,
                        "envelope_from": r.envelope_from
                    }
                    for r in records
                ]
            }
        finally:
            db.close()

    def delete_domain_with_cleanup(self, domain_name: str) -> bool:
        """Delete a domain and all its associated data"""
        db = self._get_db()
        try:
            domain = db.query(Domain).filter(Domain.name == domain_name).first()
            if not domain:
                return False

            db.delete(domain)
            db.commit()
            return True
        except Exception:
            db.rollback()
            return False
        finally:
            db.close()

    def clear(self) -> None:
        """Clear all data (for testing)"""
        db = self._get_db()
        try:
            db.query(ReportRecord).delete()
            db.query(DMARCReport).delete()
            db.query(Domain).delete()
            db.commit()
        finally:
            db.close()


# Create alias for backward compatibility
ReportStore = PersistentReportStore
