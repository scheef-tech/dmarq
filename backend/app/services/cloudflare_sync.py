"""
Cloudflare sync service for importing domains and DNS records
"""
import re
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.domain import Domain

logger = logging.getLogger(__name__)


class CloudflareSyncService:
    """Service for syncing domains from Cloudflare"""

    def __init__(self):
        pass

    def _get_db(self) -> Session:
        """Get a database session"""
        return SessionLocal()

    def parse_dmarc_policy(self, dmarc_record: str) -> Optional[str]:
        """Extract policy from DMARC TXT record"""
        if not dmarc_record:
            return None
        # Match p=none, p=quarantine, or p=reject
        match = re.search(r'p=(none|quarantine|reject)', dmarc_record.lower())
        return match.group(1) if match else None

    def sync_zones(self, zones_data: List[Dict[str, Any]], account_name: str) -> Dict[str, Any]:
        """
        Sync zones from Cloudflare to the database

        Args:
            zones_data: List of zone dictionaries from Cloudflare API
            account_name: Name of the Cloudflare account (e.g., 'scheef', 'onebase-llc')

        Returns:
            Summary of sync operation
        """
        db = self._get_db()
        try:
            created = 0
            updated = 0
            errors = []

            for zone in zones_data:
                try:
                    zone_name = zone.get("name", "")
                    zone_id = zone.get("id", "")

                    if not zone_name:
                        continue

                    # Check if domain exists
                    domain = db.query(Domain).filter(Domain.name == zone_name).first()

                    if domain:
                        # Update existing domain
                        domain.cloudflare_account = account_name
                        domain.cloudflare_zone_id = zone_id
                        domain.last_dns_sync = datetime.utcnow()
                        updated += 1
                    else:
                        # Create new domain
                        domain = Domain(
                            name=zone_name,
                            cloudflare_account=account_name,
                            cloudflare_zone_id=zone_id,
                            active=True,
                            verified=True,
                            last_dns_sync=datetime.utcnow()
                        )
                        db.add(domain)
                        created += 1

                except Exception as e:
                    errors.append(f"{zone.get('name', 'unknown')}: {str(e)}")
                    logger.error(f"Error syncing zone {zone.get('name')}: {e}")

            db.commit()

            return {
                "success": True,
                "account": account_name,
                "created": created,
                "updated": updated,
                "total": created + updated,
                "errors": errors
            }

        except Exception as e:
            db.rollback()
            logger.error(f"Error in sync_zones: {e}")
            return {
                "success": False,
                "error": str(e),
                "created": 0,
                "updated": 0
            }
        finally:
            db.close()

    def update_dns_records(self, domain_name: str, dns_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Update DNS record information for a domain

        Args:
            domain_name: The domain to update
            dns_records: List of DNS TXT records from Cloudflare

        Returns:
            Summary of update
        """
        db = self._get_db()
        try:
            domain = db.query(Domain).filter(Domain.name == domain_name).first()
            if not domain:
                return {"success": False, "error": f"Domain {domain_name} not found"}

            # Find DMARC and SPF records
            dmarc_record = None
            spf_record = None

            for record in dns_records:
                record_name = record.get("name", "")
                record_content = record.get("content", "")
                record_type = record.get("type", "")

                if record_type != "TXT":
                    continue

                # Check for DMARC record (_dmarc.domain.com)
                if record_name.startswith("_dmarc.") or record_name == f"_dmarc.{domain_name}":
                    dmarc_record = record_content

                # Check for SPF record
                if record_content.startswith("v=spf1"):
                    spf_record = record_content

            # Update domain
            if dmarc_record:
                domain.dmarc_policy = self.parse_dmarc_policy(dmarc_record)

            if spf_record:
                domain.spf_record = spf_record

            domain.last_dns_sync = datetime.utcnow()
            db.commit()

            return {
                "success": True,
                "domain": domain_name,
                "dmarc_policy": domain.dmarc_policy,
                "spf_record": spf_record is not None,
                "dmarc_record": dmarc_record
            }

        except Exception as e:
            db.rollback()
            logger.error(f"Error updating DNS records for {domain_name}: {e}")
            return {"success": False, "error": str(e)}
        finally:
            db.close()

    def bulk_update_domain_status(self, domains_status: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Bulk update domain status (active/parked)

        Args:
            domains_status: List of {domain_name, active, dmarc_policy, spf_record}

        Returns:
            Summary of updates
        """
        db = self._get_db()
        try:
            updated = 0
            created = 0
            errors = []

            for item in domains_status:
                try:
                    domain_name = item.get("domain_name")
                    if not domain_name:
                        continue

                    domain = db.query(Domain).filter(Domain.name == domain_name).first()

                    if domain:
                        # Update existing
                        if "active" in item:
                            domain.active = item["active"]
                        if "dmarc_policy" in item:
                            domain.dmarc_policy = item["dmarc_policy"]
                        if "spf_record" in item:
                            domain.spf_record = item["spf_record"]
                        if "cloudflare_account" in item:
                            domain.cloudflare_account = item["cloudflare_account"]
                        domain.last_dns_sync = datetime.utcnow()
                        updated += 1
                    else:
                        # Create new domain
                        domain = Domain(
                            name=domain_name,
                            active=item.get("active", True),
                            dmarc_policy=item.get("dmarc_policy"),
                            spf_record=item.get("spf_record"),
                            cloudflare_account=item.get("cloudflare_account"),
                            verified=True,
                            last_dns_sync=datetime.utcnow()
                        )
                        db.add(domain)
                        created += 1

                except Exception as e:
                    errors.append(f"{item.get('domain_name', 'unknown')}: {str(e)}")

            db.commit()

            return {
                "success": True,
                "created": created,
                "updated": updated,
                "total": created + updated,
                "errors": errors
            }

        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}
        finally:
            db.close()

    def get_all_domains(self) -> List[Dict[str, Any]]:
        """Get all domains from database"""
        db = self._get_db()
        try:
            domains = db.query(Domain).all()
            return [
                {
                    "id": d.id,
                    "name": d.name,
                    "active": d.active,
                    "dmarc_policy": d.dmarc_policy,
                    "spf_record": d.spf_record,
                    "cloudflare_account": d.cloudflare_account,
                    "last_dns_sync": d.last_dns_sync.isoformat() if d.last_dns_sync else None,
                    "created_at": d.created_at.isoformat() if d.created_at else None
                }
                for d in domains
            ]
        finally:
            db.close()

    def get_sync_status(self) -> Dict[str, Any]:
        """Get current sync status"""
        db = self._get_db()
        try:
            total_domains = db.query(Domain).count()
            synced_domains = db.query(Domain).filter(Domain.last_dns_sync != None).count()
            with_dmarc = db.query(Domain).filter(Domain.dmarc_policy != None).count()
            with_spf = db.query(Domain).filter(Domain.spf_record != None).count()

            # Get counts by account
            accounts = db.query(
                Domain.cloudflare_account,
            ).filter(Domain.cloudflare_account != None).distinct().all()

            account_counts = {}
            for (account,) in accounts:
                if account:
                    count = db.query(Domain).filter(Domain.cloudflare_account == account).count()
                    account_counts[account] = count

            return {
                "total_domains": total_domains,
                "synced_domains": synced_domains,
                "with_dmarc_policy": with_dmarc,
                "with_spf_record": with_spf,
                "accounts": account_counts
            }
        finally:
            db.close()


# Singleton instance
_sync_service: Optional[CloudflareSyncService] = None


def get_sync_service() -> CloudflareSyncService:
    """Get singleton instance of CloudflareSyncService"""
    global _sync_service
    if _sync_service is None:
        _sync_service = CloudflareSyncService()
    return _sync_service
