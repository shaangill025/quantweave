"""SYNTHETIC qualified feed and entitlement for ingestion tests (T023).

Built through the real T021 registry with invented ids and evidence; no real
provider is named or approved. Importable from tests via the pytest `pythonpath`.
"""

from datetime import UTC, datetime, timedelta

from qw_domain.ingest import IngestRights
from qw_domain.rights import (
    EntitlementHistory,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Grant,
    Provider,
    Registry,
    RightsProfile,
    RightState,
    Use,
    UseScope,
)

T = datetime(2025, 1, 2, tzinfo=UTC)  # feed qualified; grants expire 2028
TENANT, REGION, KEEP = "tenant-synth-a", "CA-ON", timedelta(365)
PERMIT = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", T, "counsel-synth")


def _profile(missing: Use | None = None) -> RightsProfile:
    def grant(use: Use | None = None) -> Grant:
        keep = KEEP if use is Use.RETENTION else None
        return Grant(RightState.GRANTED, PERMIT, datetime(2028, 1, 1, tzinfo=UTC), keep)

    uses = {u: grant(u) for u in Use if u is not missing}
    return RightsProfile(uses, {UseScope.PERSONAL: grant()}, {REGION: grant()})


def qualified_rights(
    feed_id: str,
    received: datetime,
    missing: Use | None = None,
    registry: Registry | None = None,
) -> IngestRights:
    """Rights for `feed_id`, qualified for every use except `missing` in the tenant
    entitlement; pass `registry` to use another (e.g. an empty) registry."""
    if registry is None:
        auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", T)
        work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", T)
        feed = (
            FeedHistory.new(feed_id, "provider-synth")
            .revise("synthetic_feed", frozenset(Use), _profile(), T)
            .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), T)
            .transition(FeedStatus.QUALIFIED, "operator-synth", (work, PERMIT), T)
        )
        ent = EntitlementHistory.new(TENANT, feed_id).revise(
            EntitlementSource.INSTALLATION_LICENSE, True, _profile(missing), None, T
        )
        registry = (
            Registry()
            .with_provider(Provider("provider-synth", "SYNTHETIC provider"))
            .with_feed(feed)
            .with_entitlement(ent)
        )
    scope = UseScope.PERSONAL
    return IngestRights(registry, TENANT, feed_id, received, scope, REGION, KEEP)
