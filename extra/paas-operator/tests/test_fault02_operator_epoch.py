import uuid

from src.main import OPERATOR_INSTANCE_EPOCH, OPERATOR_INSTANCE_ID
from src.models.schemas import HealthResponse


def test_operator_epoch_is_process_stable_uuid():
    assert str(uuid.UUID(OPERATOR_INSTANCE_EPOCH)) == OPERATOR_INSTANCE_EPOCH
    assert OPERATOR_INSTANCE_EPOCH == OPERATOR_INSTANCE_EPOCH


def test_health_contract_requires_operator_epoch():
    value = HealthResponse(
        status="healthy", timestamp="2026-10-27T00:00:00Z",
        helm_version="v3", instance_epoch=OPERATOR_INSTANCE_EPOCH,
    )
    assert value.instance_epoch == OPERATOR_INSTANCE_EPOCH


def test_health_reports_a_replica_identity_next_to_the_epoch():
    """Replicas answer alternately behind one Service; the epoch alone cannot tell
    a restart from the other replica, so the replica identity travels with it."""
    assert OPERATOR_INSTANCE_ID is None or (
        isinstance(OPERATOR_INSTANCE_ID, str) and 0 < len(OPERATOR_INSTANCE_ID) <= 128)
    value = HealthResponse(
        status="healthy", timestamp="2026-10-27T00:00:00Z",
        helm_version="v3", instance_epoch=OPERATOR_INSTANCE_EPOCH,
        instance_id=OPERATOR_INSTANCE_ID,
    )
    assert value.model_dump()["instance_id"] == OPERATOR_INSTANCE_ID


def test_health_replica_identity_stays_optional_for_older_consumers():
    value = HealthResponse(
        status="healthy", timestamp="2026-10-27T00:00:00Z",
        helm_version="v3", instance_epoch=OPERATOR_INSTANCE_EPOCH,
    )
    assert value.instance_id is None
