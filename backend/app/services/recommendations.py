"""
Recommendation engine for DMARC policy progression
"""
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum


class RecommendationType(str, Enum):
    """Types of recommendations"""
    READY_FOR_QUARANTINE = "ready_for_quarantine"
    READY_FOR_REJECT = "ready_for_reject"
    ENABLE_DKIM = "enable_dkim"
    SPOOFING_DETECTED = "spoofing_detected"
    AWAITING_REPORTS = "awaiting_reports"
    SET_PARKED_REJECT = "set_parked_reject"
    MONITOR = "monitor"
    INVESTIGATE_FAILURES = "investigate_failures"
    GOOD = "good"


class RecommendationPriority(str, Enum):
    """Priority levels for recommendations"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class Recommendation:
    """A single recommendation"""
    type: RecommendationType
    priority: RecommendationPriority
    title: str
    description: str
    action: Optional[str] = None


class RecommendationEngine:
    """
    Engine for generating DMARC policy recommendations based on domain data
    """

    # Thresholds for recommendations
    HIGH_PASS_RATE_THRESHOLD = 98.0  # Consider ready for policy upgrade
    GOOD_PASS_RATE_THRESHOLD = 95.0  # Good but needs more monitoring
    CONCERNING_PASS_RATE_THRESHOLD = 80.0  # Should investigate
    MIN_EMAILS_FOR_RECOMMENDATION = 100  # Minimum emails before recommending policy change
    MIN_REPORTS_FOR_RECOMMENDATION = 5  # Minimum reports before recommending policy change

    def generate_recommendation(
        self,
        domain_name: str,
        dmarc_policy: Optional[str],
        pass_rate: Optional[float],
        total_emails: int,
        report_count: int,
        failed_count: int,
        is_active: bool,
        spf_configured: bool = True,
        dkim_configured: bool = True,
        dkim_pass_rate: Optional[float] = None,
        spf_pass_rate: Optional[float] = None,
    ) -> Recommendation:
        """
        Generate a recommendation for a domain based on its current state.

        Args:
            domain_name: The domain name
            dmarc_policy: Current DMARC policy (none, quarantine, reject)
            pass_rate: Overall DMARC pass rate (0-100)
            total_emails: Total emails analyzed
            report_count: Number of reports received
            failed_count: Number of failed emails
            is_active: Whether domain is actively used for email
            spf_configured: Whether SPF is configured
            dkim_configured: Whether DKIM is configured
            dkim_pass_rate: DKIM-specific pass rate
            spf_pass_rate: SPF-specific pass rate

        Returns:
            Recommendation object with advice
        """
        policy = (dmarc_policy or "none").lower()

        # Case 1: Parked domain with weak policy
        if not is_active and policy != "reject":
            return Recommendation(
                type=RecommendationType.SET_PARKED_REJECT,
                priority=RecommendationPriority.HIGH,
                title="Set reject policy",
                description=f"Parked domain should use p=reject to prevent spoofing",
                action=f"Update DMARC to: v=DMARC1; p=reject; rua=mailto:dmarc-reports@dmarc.scheef.tech"
            )

        # Case 2: No reports yet
        if report_count == 0 or total_emails == 0:
            return Recommendation(
                type=RecommendationType.AWAITING_REPORTS,
                priority=RecommendationPriority.INFO,
                title="Awaiting reports",
                description="No DMARC reports received yet. Reports typically arrive within 24-48 hours.",
                action=None
            )

        # Case 3: Already at reject with good pass rate
        if policy == "reject" and pass_rate is not None and pass_rate >= self.HIGH_PASS_RATE_THRESHOLD:
            return Recommendation(
                type=RecommendationType.GOOD,
                priority=RecommendationPriority.INFO,
                title="Well protected",
                description=f"Domain has reject policy with {pass_rate}% pass rate. Excellent protection.",
                action=None
            )

        # Case 4: Spoofing detection - high failures from random sources
        if failed_count > 50 and pass_rate is not None and pass_rate < self.CONCERNING_PASS_RATE_THRESHOLD:
            return Recommendation(
                type=RecommendationType.SPOOFING_DETECTED,
                priority=RecommendationPriority.CRITICAL,
                title="Spoofing detected",
                description=f"High failure rate ({100 - pass_rate:.1f}%) with {failed_count} failed emails suggests active spoofing.",
                action="Review source IPs, consider moving to p=reject"
            )

        # Case 5: DKIM not configured but SPF passing
        if not dkim_configured or (dkim_pass_rate is not None and dkim_pass_rate < 50 and spf_pass_rate and spf_pass_rate > 90):
            return Recommendation(
                type=RecommendationType.ENABLE_DKIM,
                priority=RecommendationPriority.MEDIUM,
                title="Enable DKIM",
                description="SPF is passing but DKIM appears unconfigured or failing. Enable DKIM for stronger authentication.",
                action="Configure DKIM signing for your email services"
            )

        # Case 6: Ready to upgrade policy
        if pass_rate is not None and pass_rate >= self.HIGH_PASS_RATE_THRESHOLD:
            if total_emails >= self.MIN_EMAILS_FOR_RECOMMENDATION and report_count >= self.MIN_REPORTS_FOR_RECOMMENDATION:
                if policy == "none":
                    return Recommendation(
                        type=RecommendationType.READY_FOR_QUARANTINE,
                        priority=RecommendationPriority.MEDIUM,
                        title="Ready for quarantine",
                        description=f"With {pass_rate}% pass rate over {total_emails} emails, consider upgrading to p=quarantine.",
                        action="Update DMARC policy to p=quarantine"
                    )
                elif policy == "quarantine":
                    return Recommendation(
                        type=RecommendationType.READY_FOR_REJECT,
                        priority=RecommendationPriority.MEDIUM,
                        title="Ready for reject",
                        description=f"With {pass_rate}% pass rate over {total_emails} emails, consider upgrading to p=reject.",
                        action="Update DMARC policy to p=reject"
                    )

        # Case 7: Needs investigation
        if pass_rate is not None and pass_rate < self.GOOD_PASS_RATE_THRESHOLD:
            return Recommendation(
                type=RecommendationType.INVESTIGATE_FAILURES,
                priority=RecommendationPriority.HIGH,
                title="Investigate failures",
                description=f"Pass rate of {pass_rate}% is below target. Review failing sources.",
                action="Check sending sources for legitimate senders that need SPF/DKIM configuration"
            )

        # Case 8: Continue monitoring
        return Recommendation(
            type=RecommendationType.MONITOR,
            priority=RecommendationPriority.LOW,
            title="Continue monitoring",
            description=f"Pass rate: {pass_rate}%. Collect more data before policy changes.",
            action=None
        )

    def generate_recommendations_batch(
        self,
        domains_data: List[Dict[str, Any]]
    ) -> Dict[str, Recommendation]:
        """
        Generate recommendations for multiple domains.

        Args:
            domains_data: List of domain dictionaries with keys:
                - domain_name
                - dmarc_policy
                - pass_rate
                - total_emails
                - report_count
                - failed_count
                - active

        Returns:
            Dictionary mapping domain names to recommendations
        """
        recommendations = {}

        for domain in domains_data:
            domain_name = domain.get("domain_name", domain.get("name", ""))
            if not domain_name:
                continue

            rec = self.generate_recommendation(
                domain_name=domain_name,
                dmarc_policy=domain.get("dmarc_policy"),
                pass_rate=domain.get("pass_rate"),
                total_emails=domain.get("total_emails", 0),
                report_count=domain.get("report_count", 0),
                failed_count=domain.get("failed_count", 0),
                is_active=domain.get("active", True),
            )

            recommendations[domain_name] = rec

        return recommendations

    def get_critical_issues(
        self,
        domains_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Get list of critical issues that need immediate attention.

        Returns domains with critical or high priority recommendations.
        """
        issues = []
        recommendations = self.generate_recommendations_batch(domains_data)

        for domain_name, rec in recommendations.items():
            if rec.priority in [RecommendationPriority.CRITICAL, RecommendationPriority.HIGH]:
                domain_data = next((d for d in domains_data if d.get("domain_name", d.get("name")) == domain_name), {})
                issues.append({
                    "domain": domain_name,
                    "type": rec.type.value,
                    "priority": rec.priority.value,
                    "title": rec.title,
                    "description": rec.description,
                    "action": rec.action,
                    "failed_count": domain_data.get("failed_count", 0),
                    "pass_rate": domain_data.get("pass_rate")
                })

        # Sort by priority (critical first, then high)
        priority_order = {RecommendationPriority.CRITICAL.value: 0, RecommendationPriority.HIGH.value: 1}
        issues.sort(key=lambda x: priority_order.get(x["priority"], 2))

        return issues


# Singleton instance
_recommendation_engine: Optional[RecommendationEngine] = None


def get_recommendation_engine() -> RecommendationEngine:
    """Get singleton instance of RecommendationEngine"""
    global _recommendation_engine
    if _recommendation_engine is None:
        _recommendation_engine = RecommendationEngine()
    return _recommendation_engine
