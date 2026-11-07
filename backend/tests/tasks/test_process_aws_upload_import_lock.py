"""Regression for OW-BACKEND-V: the Apple XML import takes the same per-user import lock as the SDK import."""

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.integrations.celery.tasks import process_aws_upload_task


def test_import_xml_data_locks_each_batch_before_writes() -> None:
    user_id = uuid4()
    db = MagicMock()
    calls: list[str] = []
    batches: list[tuple[list[Any], list[Any], Any]] = [([MagicMock()], [], None), ([MagicMock()], [], None)]

    xml_service = MagicMock()
    xml_service.parse_xml.return_value = iter(batches)
    db.commit.side_effect = lambda: calls.append("commit")

    with (
        patch.object(process_aws_upload_task, "XMLService", return_value=xml_service),
        patch.object(
            process_aws_upload_task.DataSourceRepository,
            "lock_user_import",
            autospec=True,
            side_effect=lambda *_a, **_k: calls.append("lock"),
        ) as lock,
        patch.object(
            process_aws_upload_task.timeseries_service,
            "bulk_create_samples",
            side_effect=lambda *_a, **_k: calls.append("write"),
        ),
    ):
        process_aws_upload_task._import_xml_data(db, "/tmp/export.xml", str(user_id))

    assert lock.call_count == len(batches)
    assert all(call.args[1:] == (db, user_id) for call in lock.call_args_list)
    assert calls == ["lock", "write", "commit", "lock", "write", "commit"]
