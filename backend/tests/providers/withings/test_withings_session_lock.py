"""Exactly one reading event per weigh-in, across CONCURRENT ingests (review 4152718174 on PR #20).

A cellular Body Pro 2 weigh-in arrives as two groups. When two ingests each insert one of them,
both used to commit before either checked for an earlier sibling, and each took the other's group
for the one that had announced the weigh-in: no push at all. The ingest now takes a session lock
before recording, decides under it, and only enqueues after its commit.

These tests need COMMITTED rows seen from two real connections, so they cannot use the per-test
rollback ``db`` fixture: ``committed_member`` creates its own member on the test database and
deletes it (``user`` cascades to everything else) afterwards. Every wait is bounded.
"""

import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, text
from sqlalchemy.orm import Session

from app.models import User, UserConnection, WithingsMeasureGroupRecord
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings import attribution
from app.services.providers.withings._client import PaginatedResult
from app.services.providers.withings.data_247 import Withings247Data
from app.services.providers.withings.measure_groups import lock_sessions, session_lock_key
from tests.providers.withings.conftest import ProvisionedConnectionMaker
from tests.providers.withings.weigh_ins import (
    BODY_GRPID,
    HASH,
    PULSE_GRPID,
    SEND,
    body_group,
    days_ago,
    enable_events,
    pulse_group,
    recent,
    records,
    samples_at,
    save,
)

_PAGINATE = "app.services.providers.withings.data_247.paginate"
_EVENTS = "app.services.providers.withings.reading_events"
_WAIT = 30.0  # seconds; a healthy run takes well under one


@dataclass(frozen=True)
class Member:
    user_id: UUID
    connection_id: UUID


@pytest.fixture
def committed_member(engine: Engine) -> Generator[Member, None, None]:
    """A member with one provisioned Withings connection, committed; deleted again afterwards."""
    now = datetime.now(timezone.utc)
    user_id, connection_id = uuid4(), uuid4()
    with Session(bind=engine) as s:
        s.add(User(id=user_id, created_at=now, email=f"{user_id.hex}@example.com"))
        s.flush()
        s.add(
            UserConnection(
                id=connection_id,
                user_id=user_id,
                provider="withings",
                provider_user_id="4242",
                access_token="access",
                refresh_token="refresh",
                token_expires_at=now + timedelta(days=1),
                scope="user.metrics",
                status=ConnectionStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
        s.add(
            WithingsSdkAccount(
                id=uuid4(),
                user_connection_id=connection_id,
                external_id=f"cp-{uuid4().hex}",
                csrf_token="csrf",
                updated_at=now,
            )
        )
        s.commit()
    try:
        yield Member(user_id=user_id, connection_id=connection_id)
    finally:
        with Session(bind=engine) as s:
            s.execute(delete(User).where(User.id == user_id))  # cascades to the connection, groups and samples
            s.commit()


def _ingest(s: Session, member: Member) -> None:
    data = Withings247Data(provider_name="withings", api_base_url="https://wbsapi.withings.net", oauth=MagicMock())
    now = datetime.now(timezone.utc)
    data.save_measures(s, member.user_id, now - timedelta(days=1), now, member.connection_id)


@dataclass
class Worker:
    """One ingest on its own connection, pausable once around its first commit."""

    name: str
    rows: list[dict[str, Any]]
    job: Callable[[Session, Member], Any] = _ingest
    pause_before_commit: bool = False
    pause_after_commit: bool = False
    paused: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    pid: int | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None

    def start(self, engine: Engine, member: Member) -> None:
        self.thread = threading.Thread(target=self._run, args=(engine, member), name=self.name, daemon=True)
        self.thread.start()
        assert self.started.wait(_WAIT)

    def join(self) -> None:
        assert self.thread is not None
        self.thread.join(_WAIT)
        assert not self.thread.is_alive(), f"{self.name} did not finish"
        assert self.error is None, f"{self.name} failed: {self.error!r}"

    def _run(self, engine: Engine, member: Member) -> None:
        try:
            with Session(bind=engine, autoflush=False) as s:
                self.pid = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
                self.started.set()
                real_commit = s.commit
                pause_pending = [True]

                def commit() -> None:
                    pause = pause_pending[0]
                    pause_pending[0] = False
                    if pause and self.pause_before_commit:
                        self._pause()
                    real_commit()
                    if pause and self.pause_after_commit:
                        self._pause()

                with patch.object(s, "commit", side_effect=commit):
                    self.job(s, member)
        except BaseException as e:  # reported by join()
            self.error = e
            self.started.set()
            self.paused.set()

    def _pause(self) -> None:
        self.paused.set()
        if not self.resume.wait(_WAIT):
            raise TimeoutError(f"{self.name} was never resumed")


def _rows_by_thread(workers: list[Worker]) -> Callable[..., PaginatedResult]:
    """One ``paginate`` patch for every thread: each worker reads its own rows."""
    by_name = {w.name: w.rows for w in workers}

    def paginate(**_: Any) -> PaginatedResult:
        return PaginatedResult(rows=by_name[threading.current_thread().name], envelope={})

    return paginate


@pytest.fixture
def make_worker(committed_member: Member) -> Generator[Callable[..., Worker], None, None]:
    """``Worker(...)``, resumed and joined at teardown even when the test failed while one was paused.

    Requests ``committed_member`` so it tears down first: a paused worker holds its transaction open,
    and the member's cleanup would otherwise wait on it.
    """
    made: list[Worker] = []

    def _make(*args: Any, **kwargs: Any) -> Worker:
        made.append(Worker(*args, **kwargs))
        return made[-1]

    yield _make
    for worker in made:
        worker.resume.set()
    for worker in made:
        if worker.thread is not None:
            worker.thread.join(_WAIT)


def _waits_on_advisory_lock(engine: Engine, worker: Worker) -> bool:
    """Whether ``worker`` is blocked on an advisory lock (polled, bounded), rather than finished or running."""
    deadline = time.monotonic() + _WAIT
    with engine.connect() as c:
        while time.monotonic() < deadline:
            waiting = c.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND pid = :pid"),
                {"pid": worker.pid},
            ).scalar_one()
            c.rollback()  # a fresh snapshot of pg_locks each poll
            if waiting:
                return True
            assert worker.thread is not None
            if not worker.thread.is_alive():
                return False
            time.sleep(0.02)
    return False


class _Sends:
    """``send_task`` stand-in recording which worker enqueued which grpid."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, *_: Any, args: list[dict[str, Any]], **__: Any) -> None:
        with self._lock:
            self.calls.append((threading.current_thread().name, args[0]["grpid"]))


def _warm_up(engine: Engine, member: Member) -> None:
    """An earlier, unrelated weigh-in, so the member's Withings data source already exists.

    Otherwise the two ingests would also meet on the data source's unique index, which would hide
    whether they serialise on the session lock.
    """
    with Session(bind=engine, autoflush=False) as s, patch(SEND):
        save(s, member.user_id, member.connection_id, [body_group(days_ago(3), grpid=1, attrib=0)])


def _statuses(engine: Engine, member: Member) -> dict[str, str]:
    with Session(bind=engine) as s:
        record = WithingsMeasureGroupRecord
        rows = s.query(record.grpid, record.status).filter(record.user_connection_id == member.connection_id)
        return dict(rows.all())


def test_two_ingests_each_with_one_sibling_announce_the_weigh_in_once(
    engine: Engine, committed_member: Member, make_worker: Callable[..., Worker], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lucas' interleaving: W1 records [body], W2 records [pulse], and W1 has committed before W2 decides.

    Before the fix each worker checked for an earlier sibling AFTER its commit, so with both commits
    in, each saw the other's group and neither sent. Now the decision is taken under the session lock
    before the commit: W1 decided to announce while W2 could not yet record, and W2 then sees W1's
    committed body group and stays silent.
    """
    enable_events(monkeypatch)
    _warm_up(engine, committed_member)
    at = recent()
    w1 = make_worker("W1", [body_group(at, attrib=0)], pause_after_commit=True)
    w2 = make_worker("W2", [pulse_group(at, attrib=0)])
    sends = _Sends()
    with patch(_PAGINATE, side_effect=_rows_by_thread([w1, w2])), patch(SEND, side_effect=sends):
        w1.start(engine, committed_member)
        assert w1.paused.wait(_WAIT)  # W1 committed, has not enqueued yet
        w2.start(engine, committed_member)
        w2.join()  # nothing holds the lock any more: W2 runs to the end
        w1.resume.set()
        w1.join()
    assert sends.calls == [("W1", str(BODY_GRPID))]
    assert _statuses(engine, committed_member) == {
        "1": "registered",
        str(BODY_GRPID): "registered",
        str(PULSE_GRPID): "registered",
    }


def test_a_second_ingest_of_the_weigh_in_waits_for_the_first_to_commit(
    engine: Engine, committed_member: Member, make_worker: Callable[..., Worker], monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1 holds the session lock from before it records until it commits; W2 blocks on it, then stays silent."""
    enable_events(monkeypatch)
    _warm_up(engine, committed_member)
    at = recent()
    w1 = make_worker("W1", [body_group(at, attrib=0)], pause_before_commit=True)
    w2 = make_worker("W2", [pulse_group(at, attrib=0)])
    sends = _Sends()
    with patch(_PAGINATE, side_effect=_rows_by_thread([w1, w2])), patch(SEND, side_effect=sends):
        w1.start(engine, committed_member)
        assert w1.paused.wait(_WAIT)  # W1 recorded and decided, not committed
        w2.start(engine, committed_member)
        waited = _waits_on_advisory_lock(engine, w2)
        w1.resume.set()
        w1.join()
        w2.join()
    assert waited, "W2 did not wait for W1's session lock"
    assert sends.calls == [("W1", str(BODY_GRPID))]


def test_a_pending_weigh_in_split_across_two_ingests_is_one_pending_session_and_one_event(
    engine: Engine, committed_member: Member, make_worker: Callable[..., Worker], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the lock W2's stored-status read sees W1's pending body group, so the pulse joins it (R5)."""
    enable_events(monkeypatch)
    _warm_up(engine, committed_member)
    at = recent()
    w1 = make_worker("W1", [body_group(at, attrib=1)], pause_before_commit=True)
    w2 = make_worker("W2", [pulse_group(at, attrib=0)])
    sends = _Sends()
    with patch(_PAGINATE, side_effect=_rows_by_thread([w1, w2])), patch(SEND, side_effect=sends):
        w1.start(engine, committed_member)
        assert w1.paused.wait(_WAIT)
        w2.start(engine, committed_member)
        assert _waits_on_advisory_lock(engine, w2)
        w1.resume.set()
        w1.join()
        w2.join()
    assert sends.calls == [("W1", str(BODY_GRPID))]
    statuses = _statuses(engine, committed_member)
    assert (statuses[str(BODY_GRPID)], statuses[str(PULSE_GRPID)]) == ("pending", "pending")


# --- the decision composes with the attribution states (one connection, rollback fixture) ---------


def test_a_late_sibling_of_a_discarded_weigh_in_announces_nothing(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    at = recent()
    with patch(SEND) as send:
        save(db, user.id, connection.id, [body_group(at)])  # pending: announced once, pending: true
        assert attribution.discard_reading(db, user_id=user.id, grpid=str(BODY_GRPID)) is not None
        save(db, user.id, connection.id, [pulse_group(at, attrib=0)])
    assert [c.kwargs["args"][0]["grpid"] for c in send.call_args_list] == [str(BODY_GRPID)]
    assert records(db, connection.id)[str(PULSE_GRPID)].status == "discarded"
    assert samples_at(db, user.id, at) == []


def test_a_failed_decision_still_commits_the_ingest(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision runs inside the ingest's transaction, in a savepoint: its failure costs the event only."""
    enable_events(monkeypatch)
    user, connection = make_provisioned_connection()
    at = recent()

    def _broken_query(db: Session, *_: Any) -> None:
        db.execute(text("SELECT * FROM no_such_table"))  # aborts the transaction, as a real DB error does

    with (
        patch(SEND) as send,
        patch(f"{_EVENTS}.UserConnectionRepository.get", side_effect=_broken_query),
        patch(f"{_EVENTS}.log_and_capture_error") as capture,
    ):
        counts = save(db, user.id, connection.id, [body_group(at, attrib=0), pulse_group(at, attrib=0)])
    send.assert_not_called()
    capture.assert_called_once()
    assert counts.inserted == 5
    assert len(samples_at(db, user.id, at)) == 5
    assert set(records(db, connection.id)) == {str(BODY_GRPID), str(PULSE_GRPID)}


def test_a_sessions_groups_share_one_lock_key_and_other_sessions_do_not() -> None:
    connection_id = uuid4()
    at = datetime(2026, 10, 1, 8, 8, 47, tzinfo=timezone.utc)

    def key(grpid: str, hash_device_id: str | None = HASH, device_id: str | None = "15542329", **kw: Any) -> str:
        return session_lock_key(
            kw.get("connection", connection_id),
            grpid=grpid,
            hash_device_id=hash_device_id,
            device_id=device_id,
            measured_at=kw.get("at", at),
        )

    assert key("1") == key("2")  # body and pulse group of one weigh-in
    assert key("1") == key("2", device_id=None)  # the hash decides when there is one
    assert key("1") != key("1", at=at + timedelta(seconds=1))
    assert key("1") != key("1", hash_device_id="another")
    assert key("1") != key("1", connection=uuid4())
    # Nothing names a device: a session of its own, keyed by its grpid.
    assert key("1", None, None) != key("2", None, None)
    assert key("1", None, None) == key("1", None, None, at=at + timedelta(days=1))


# --- confirm, discard and the retirement take the same lock ---------------------------------------


def _stale_pending_weigh_in(engine: Engine, member: Member) -> datetime:
    """A pending weigh-in measured 8 days ago (too old to announce, old enough to retire)."""
    at = days_ago(8)
    with Session(bind=engine, autoflush=False) as s, patch(SEND):
        save(s, member.user_id, member.connection_id, [body_group(at), pulse_group(at)])
    return datetime.fromtimestamp(at, tz=timezone.utc)


_ACTIONS: dict[str, Callable[[Session, Member], Any]] = {
    "confirm": lambda s, m: attribution.confirm_reading(s, user_id=m.user_id, grpid=str(PULSE_GRPID)),
    "discard": lambda s, m: attribution.discard_reading(s, user_id=m.user_id, grpid=str(PULSE_GRPID)),
    "retire": lambda s, _: attribution.retire_stale_pending(s),
}
_AFTER = {"confirm": "registered", "discard": "discarded", "retire": "discarded"}


@pytest.mark.parametrize("action", sorted(_ACTIONS))
def test_confirm_discard_and_retire_wait_for_the_session_lock_an_ingest_holds(
    engine: Engine, committed_member: Member, make_worker: Callable[..., Worker], action: str
) -> None:
    """An ingest of a late sibling holds this lock; the member's answer cannot interleave with it."""
    measured_at = _stale_pending_weigh_in(engine, committed_member)
    key = session_lock_key(
        committed_member.connection_id,
        grpid=str(BODY_GRPID),
        hash_device_id=HASH,
        device_id="15542329",
        measured_at=measured_at,
    )
    worker = make_worker(action, [], job=_ACTIONS[action])
    with Session(bind=engine) as holder:
        lock_sessions(holder, [key])  # what an ingest of this session holds until its commit
        worker.start(engine, committed_member)
        waited = _waits_on_advisory_lock(engine, worker)
        holder.rollback()  # the "ingest" ends
        worker.join()
    assert waited, f"{action} did not wait for the session lock"
    statuses = _statuses(engine, committed_member)
    assert (statuses[str(BODY_GRPID)], statuses[str(PULSE_GRPID)]) == (_AFTER[action], _AFTER[action])
