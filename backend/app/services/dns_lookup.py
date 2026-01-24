"""
DNS Lookup Service - Real DNS lookups for DMARC, SPF, and DKIM records.

Uses dnspython for DNS queries with caching support.
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from functools import lru_cache

import dns.resolver
import dns.exception

logger = logging.getLogger(__name__)


@dataclass
class DNSRecord:
    """Represents a DNS record lookup result"""
    exists: bool
    record: Optional[str] = None
    records: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class DMARCInfo:
    """Parsed DMARC record information"""
    exists: bool
    record: Optional[str] = None
    policy: Optional[str] = None  # none, quarantine, reject
    subdomain_policy: Optional[str] = None  # sp= tag
    percentage: int = 100  # pct= tag
    rua: List[str] = field(default_factory=list)  # Aggregate report URIs
    ruf: List[str] = field(default_factory=list)  # Forensic report URIs
    adkim: str = "r"  # DKIM alignment (r=relaxed, s=strict)
    aspf: str = "r"  # SPF alignment (r=relaxed, s=strict)
    error: Optional[str] = None


@dataclass
class SPFInfo:
    """Parsed SPF record information"""
    exists: bool
    record: Optional[str] = None
    mechanisms: List[str] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)
    all_mechanism: Optional[str] = None  # +all, -all, ~all, ?all
    error: Optional[str] = None


@dataclass
class DomainDNSInfo:
    """Complete DNS information for a domain"""
    domain: str
    dmarc: DMARCInfo
    spf: SPFInfo
    dkim_selectors: List[str] = field(default_factory=list)
    lookup_time: Optional[str] = None


class DNSLookupService:
    """
    Service for performing DNS lookups for email authentication records.

    Features:
    - DMARC record lookup and parsing
    - SPF record lookup and parsing
    - DKIM selector verification
    - Result caching with TTL
    """

    # Cache TTL in seconds (15 minutes)
    CACHE_TTL = 900

    # Common DKIM selectors to check
    COMMON_DKIM_SELECTORS = [
        "selector1",  # Microsoft 365
        "selector2",  # Microsoft 365
        "google",     # Google Workspace
        "default",    # Common default
        "dkim",       # Generic
        "mail",       # Generic
        "k1",         # Mailchimp
        "s1",         # Generic
        "s2",         # Generic
        "smtp",       # Generic
    ]

    def __init__(self, timeout: float = 5.0, cache_enabled: bool = True):
        """
        Initialize the DNS lookup service.

        Args:
            timeout: DNS query timeout in seconds
            cache_enabled: Whether to cache lookup results
        """
        self.timeout = timeout
        self.cache_enabled = cache_enabled
        self._cache: Dict[str, Tuple[Any, datetime]] = {}

        # Configure DNS resolver
        self.resolver = dns.resolver.Resolver()
        self.resolver.timeout = timeout
        self.resolver.lifetime = timeout

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get a cached result if not expired."""
        if not self.cache_enabled:
            return None

        if key in self._cache:
            value, timestamp = self._cache[key]
            if datetime.utcnow() - timestamp < timedelta(seconds=self.CACHE_TTL):
                return value
            else:
                del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any):
        """Cache a result."""
        if self.cache_enabled:
            self._cache[key] = (value, datetime.utcnow())

    def clear_cache(self):
        """Clear all cached results."""
        self._cache.clear()

    def _query_txt(self, domain: str) -> DNSRecord:
        """
        Query TXT records for a domain.

        Args:
            domain: Domain name to query

        Returns:
            DNSRecord with query results
        """
        try:
            answers = self.resolver.resolve(domain, 'TXT')
            records = []
            for rdata in answers:
                # TXT records can be split into multiple strings
                txt_value = ''.join([s.decode('utf-8') for s in rdata.strings])
                records.append(txt_value)

            return DNSRecord(
                exists=True,
                record=records[0] if records else None,
                records=records
            )

        except dns.resolver.NXDOMAIN:
            return DNSRecord(exists=False, error="Domain not found")
        except dns.resolver.NoAnswer:
            return DNSRecord(exists=False, error="No TXT records")
        except dns.resolver.NoNameservers:
            return DNSRecord(exists=False, error="No nameservers available")
        except dns.exception.Timeout:
            return DNSRecord(exists=False, error="DNS query timeout")
        except Exception as e:
            logger.error(f"DNS query error for {domain}: {e}")
            return DNSRecord(exists=False, error=str(e))

    def lookup_dmarc(self, domain: str) -> DMARCInfo:
        """
        Lookup and parse DMARC record for a domain.

        Args:
            domain: Domain name to query

        Returns:
            DMARCInfo with parsed DMARC configuration
        """
        cache_key = f"dmarc:{domain}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        dmarc_domain = f"_dmarc.{domain}"
        result = self._query_txt(dmarc_domain)

        if not result.exists:
            info = DMARCInfo(exists=False, error=result.error)
            self._set_cached(cache_key, info)
            return info

        # Find the DMARC record (starts with v=DMARC1)
        dmarc_record = None
        for record in result.records:
            if record.lower().startswith("v=dmarc1"):
                dmarc_record = record
                break

        if not dmarc_record:
            info = DMARCInfo(exists=False, error="No valid DMARC record found")
            self._set_cached(cache_key, info)
            return info

        # Parse DMARC tags
        info = self._parse_dmarc(dmarc_record)
        self._set_cached(cache_key, info)
        return info

    def _parse_dmarc(self, record: str) -> DMARCInfo:
        """Parse a DMARC record string into structured info."""
        info = DMARCInfo(exists=True, record=record)

        # Split into tags
        tags = [t.strip() for t in record.split(';')]

        for tag in tags:
            if '=' not in tag:
                continue

            key, value = tag.split('=', 1)
            key = key.strip().lower()
            value = value.strip()

            if key == 'p':
                info.policy = value.lower()
            elif key == 'sp':
                info.subdomain_policy = value.lower()
            elif key == 'pct':
                try:
                    info.percentage = int(value)
                except ValueError:
                    pass
            elif key == 'rua':
                # Parse mailto: URIs
                info.rua = [uri.strip() for uri in value.split(',')]
            elif key == 'ruf':
                info.ruf = [uri.strip() for uri in value.split(',')]
            elif key == 'adkim':
                info.adkim = value.lower()
            elif key == 'aspf':
                info.aspf = value.lower()

        return info

    def lookup_spf(self, domain: str) -> SPFInfo:
        """
        Lookup and parse SPF record for a domain.

        Args:
            domain: Domain name to query

        Returns:
            SPFInfo with parsed SPF configuration
        """
        cache_key = f"spf:{domain}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = self._query_txt(domain)

        if not result.exists:
            info = SPFInfo(exists=False, error=result.error)
            self._set_cached(cache_key, info)
            return info

        # Find the SPF record (starts with v=spf1)
        spf_record = None
        for record in result.records:
            if record.lower().startswith("v=spf1"):
                spf_record = record
                break

        if not spf_record:
            info = SPFInfo(exists=False, error="No SPF record found")
            self._set_cached(cache_key, info)
            return info

        # Parse SPF record
        info = self._parse_spf(spf_record)
        self._set_cached(cache_key, info)
        return info

    def _parse_spf(self, record: str) -> SPFInfo:
        """Parse an SPF record string into structured info."""
        info = SPFInfo(exists=True, record=record)

        # Split into mechanisms
        parts = record.split()
        mechanisms = []
        includes = []

        for part in parts[1:]:  # Skip v=spf1
            part_lower = part.lower()

            # Check for include
            if part_lower.startswith('include:'):
                includes.append(part[8:])  # Remove 'include:' prefix
                mechanisms.append(part)
            # Check for all mechanism
            elif 'all' in part_lower:
                info.all_mechanism = part
                mechanisms.append(part)
            else:
                mechanisms.append(part)

        info.mechanisms = mechanisms
        info.includes = includes
        return info

    def lookup_dkim(self, domain: str, selector: str) -> DNSRecord:
        """
        Lookup DKIM record for a specific selector.

        Args:
            domain: Domain name
            selector: DKIM selector to check

        Returns:
            DNSRecord with DKIM public key if found
        """
        cache_key = f"dkim:{selector}:{domain}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        dkim_domain = f"{selector}._domainkey.{domain}"
        result = self._query_txt(dkim_domain)

        self._set_cached(cache_key, result)
        return result

    def find_dkim_selectors(self, domain: str, selectors: List[str] = None) -> List[str]:
        """
        Find active DKIM selectors for a domain.

        Args:
            domain: Domain name to check
            selectors: List of selectors to try (defaults to common selectors)

        Returns:
            List of selectors that have valid DKIM records
        """
        if selectors is None:
            selectors = self.COMMON_DKIM_SELECTORS

        found_selectors = []
        for selector in selectors:
            result = self.lookup_dkim(domain, selector)
            if result.exists:
                found_selectors.append(selector)

        return found_selectors

    def lookup_domain(self, domain: str, check_dkim: bool = True) -> DomainDNSInfo:
        """
        Perform complete DNS lookup for a domain.

        Args:
            domain: Domain name to query
            check_dkim: Whether to search for DKIM selectors

        Returns:
            DomainDNSInfo with all DNS authentication records
        """
        dmarc = self.lookup_dmarc(domain)
        spf = self.lookup_spf(domain)

        dkim_selectors = []
        if check_dkim:
            dkim_selectors = self.find_dkim_selectors(domain)

        return DomainDNSInfo(
            domain=domain,
            dmarc=dmarc,
            spf=spf,
            dkim_selectors=dkim_selectors,
            lookup_time=datetime.utcnow().isoformat()
        )

    def to_api_response(self, domain: str, check_dkim: bool = True) -> Dict[str, Any]:
        """
        Get DNS info formatted for API response.

        Args:
            domain: Domain to lookup
            check_dkim: Whether to check for DKIM selectors

        Returns:
            Dictionary formatted for API response
        """
        info = self.lookup_domain(domain, check_dkim)

        return {
            "dmarc": info.dmarc.exists,
            "dmarcRecord": info.dmarc.record,
            "dmarcPolicy": info.dmarc.policy,
            "dmarcSubdomainPolicy": info.dmarc.subdomain_policy,
            "dmarcPercentage": info.dmarc.percentage,
            "dmarcRua": info.dmarc.rua,
            "dmarcRuf": info.dmarc.ruf,
            "dmarcError": info.dmarc.error,
            "spf": info.spf.exists,
            "spfRecord": info.spf.record,
            "spfMechanisms": info.spf.mechanisms,
            "spfIncludes": info.spf.includes,
            "spfAll": info.spf.all_mechanism,
            "spfError": info.spf.error,
            "dkim": len(info.dkim_selectors) > 0,
            "dkimSelectors": ", ".join(info.dkim_selectors) if info.dkim_selectors else None,
            "lookupTime": info.lookup_time
        }


# Singleton instance
_dns_service: Optional[DNSLookupService] = None


def get_dns_service() -> DNSLookupService:
    """Get the singleton DNSLookupService instance."""
    global _dns_service
    if _dns_service is None:
        _dns_service = DNSLookupService()
    return _dns_service
