"""Archive external-operation evidence in the same transaction as record deletion."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.infrastructure.persistence.models import (
    UnpackDeletedItemAudit,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)


def archive_deleted_item_operations(
    session: Session, *, definition_id: str, item: UnpackExecutionItem
) -> None:
    """Persist a retained snapshot before FK CASCADE removes the original journal.

    No media or downloader operations are performed.
    """
    operations = session.scalars(
        select(UnpackExternalOperationJournal)
        .where(UnpackExternalOperationJournal.item_id == item.id)
        .order_by(
            UnpackExternalOperationJournal.created_at,
            UnpackExternalOperationJournal.id,
        )
    ).all()
    if not operations:
        return
    payload = {
        "source_snapshot": item.source_snapshot,
        "media_identity": item.media_identity,
        "item_status": item.status,
        "selected_candidate_id": item.selected_candidate_id,
        "execution_plan": item.execution_plan,
        "execution_plan_digest": item.execution_plan_digest,
        "execution_state": item.execution_state,
        "operations": [
            {
                "id": row.id,
                "idempotency_key": row.idempotency_key,
                "operation_type": row.operation_type,
                "status": row.status,
                "target": row.target,
                "intent": row.intent,
                "before_snapshot": row.before_snapshot,
                "after_snapshot": row.after_snapshot,
                "last_error_code": row.last_error_code,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in operations
        ],
    }
    session.add(
        UnpackDeletedItemAudit(
            definition_id=definition_id,
            execution_id=item.execution_id,
            item_id=item.id,
            payload=payload,
        )
    )
    session.flush()
