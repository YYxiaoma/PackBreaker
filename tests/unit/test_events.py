from uuid import uuid4

from backend.app.domain.events import new_domain_event


def test_event_copies_payload_and_has_identity() -> None:
    payload = {"status": "PENDING"}

    event = new_domain_event(
        aggregate_id=uuid4(),
        event_type="TASK_CREATED",
        payload=payload,
    )

    payload["status"] = "DONE"

    assert event.payload == {"status": "PENDING"}
    assert event.event_id != event.aggregate_id
