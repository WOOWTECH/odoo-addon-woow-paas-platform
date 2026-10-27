import uuid

from src.main import OPERATOR_INSTANCE_EPOCH
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
