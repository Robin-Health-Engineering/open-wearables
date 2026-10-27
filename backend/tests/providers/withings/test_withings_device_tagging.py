"""Tagging a device as sold (spec 2026-10-27 O3, contract C2), against the real ingest path.

robin-backend matches a member's device to its order by hash_deviceid and tags it. On the member's
own Withings account that tag is what makes the device "the scale we sold", so the first tag also
announces the weigh-ins it already took within ``robin_reading_event_max_age_hours`` — Health Mate
setup takes a first weigh-in before the member links the account. Once each, never again.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User, UserConnection
from app.models.withings_device import WithingsDevice
from app.schemas.auth import ConnectionStatus
from app.services.providers.withings.device_tagging import set_order_ref
from tests.providers.withings.conftest import ProvisionedConnectionMaker, add_device, self_linked_connection
from tests.providers.withings.weigh_ins import (
    BODY_GRPID,
    HASH,
    SEND,
    body_group,
    days_ago,
    enable_events,
    pulse_group,
    recent,
    records,
    save,
)


@pytest.fixture(autouse=True)
def _events(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_events(monkeypatch)


def _sent(send: MagicMock) -> list[dict[str, Any]]:
    return [c.kwargs["args"][0] for c in send.call_args_list]


def _body_comp_member(db: Session, *, external_user_id: str | None = "cp-bodycomp-1") -> tuple[User, UserConnection]:
    """A member's own Withings account holding the Body Comp we sold (untagged yet) and a family scale."""
    user, own = self_linked_connection(db, external_user_id=external_user_id)
    add_device(db, own, "bodycomp-1", hash_device_id=HASH)
    add_device(db, own, "family-scale", hash_device_id="family-hash")
    return user, own


def test_a_weigh_in_before_the_tag_is_announced_once_when_the_device_is_tagged(db: Session) -> None:
    user, own = _body_comp_member(db)
    at = recent()
    rows = [body_group(at, attrib=0), pulse_group(at, attrib=0)]
    with patch(SEND) as send:
        save(db, user.id, own.id, rows)
        assert send.call_count == 0  # not a device Robin sold yet

        device = set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")

        assert device is not None
        assert device.order_ref == "ORDER-1"
        (payload,) = _sent(send)
        assert (payload["grpid"], payload["external_user_id"], payload["pending"]) == (
            str(BODY_GRPID),
            "cp-bodycomp-1",
            False,
        )
        assert {"weight", "heart_rate"} <= set(payload["types"])
        assert payload["hash_deviceid"] == HASH

        # Robin re-issues the tag on every match; a replacement order re-tags; the window is re-read;
        # a late sibling of the weigh-in arrives. None of them announces it again.
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-2")
        save(db, user.id, own.id, rows)
        late = body_group(at, grpid=BODY_GRPID + 9, attrib=0, measures=[{"value": 5120, "type": 76, "unit": -2}])
        save(db, user.id, own.id, [late])
    assert send.call_count == 1


def test_tagging_never_announces_another_device_on_the_same_account(db: Session) -> None:
    user, own = _body_comp_member(db)
    at = recent()
    family = body_group(at - 60, grpid=8530400001, attrib=0, hash_deviceid="family-hash", deviceid=777)
    with patch(SEND) as send:
        save(db, user.id, own.id, [body_group(at, attrib=0), family])
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
    assert [p["grpid"] for p in _sent(send)] == [str(BODY_GRPID)]


def test_a_weigh_in_older_than_the_event_window_is_not_announced_on_tagging(db: Session) -> None:
    user, own = _body_comp_member(db)
    with patch(SEND) as send:
        save(db, user.id, own.id, [body_group(days_ago(2), attrib=0)])
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
    send.assert_not_called()


def test_an_ambiguous_weigh_in_before_the_tag_is_never_announced(db: Session) -> None:
    """Decision D-c: it was registered at ingest; the tag neither holds it retroactively nor pushes it."""
    user, own = _body_comp_member(db)
    with patch(SEND) as send:
        save(db, user.id, own.id, [body_group(recent(), attrib=1)])
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
    send.assert_not_called()
    assert {r.status for r in records(db, own.id).values()} == {"registered"}


def test_a_weigh_in_with_an_ambiguous_group_before_the_tag_is_never_announced(db: Session) -> None:
    """D-c holds per weigh-in: an ambiguous body group silences its unambiguous pulse sibling too."""
    user, own = _body_comp_member(db)
    at = recent()
    with patch(SEND) as send:
        save(db, user.id, own.id, [body_group(at, attrib=1), pulse_group(at, attrib=0)])
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
    send.assert_not_called()


def test_a_weigh_in_after_the_tag_is_announced_by_its_own_ingest(db: Session) -> None:
    user, own = _body_comp_member(db)
    with patch(SEND) as send:
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
        assert send.call_count == 0
        save(db, user.id, own.id, [body_group(recent(), attrib=0)])
    assert [p["grpid"] for p in _sent(send)] == [str(BODY_GRPID)]


def test_a_member_without_a_robin_id_is_tagged_but_gets_no_event(db: Session) -> None:
    user, own = _body_comp_member(db, external_user_id=None)
    with patch(SEND) as send:
        save(db, user.id, own.id, [body_group(recent(), attrib=0)])
        device = set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1")
    assert device is not None
    assert device.order_ref == "ORDER-1"
    send.assert_not_called()


def test_tagging_the_device_on_a_provisioned_account_never_announces_again(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    """Its weigh-ins were announced at ingest: a provisioned account's devices are always in scope."""
    user, provisioned = make_provisioned_connection()
    add_device(db, provisioned, HASH, hash_device_id=HASH)
    with patch(SEND) as send:
        save(db, user.id, provisioned.id, [body_group(recent(), attrib=0)])
        set_order_ref(db, user_id=user.id, device_id=HASH, order_ref="ORDER-1")
    assert send.call_count == 1


def test_a_member_with_both_accounts_gets_the_self_linked_event_under_their_robin_id(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, provisioned = make_provisioned_connection()
    user.external_user_id = "cp-member-9"
    cellular = add_device(db, provisioned, "cell-1", hash_device_id="cell-hash")
    _, own = self_linked_connection(db, user=user)
    add_device(db, own, "bodycomp-1", hash_device_id=HASH)
    with patch(SEND) as send:
        save(db, user.id, own.id, [body_group(recent(), attrib=0)])
        set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-2")
    assert [p["external_user_id"] for p in _sent(send)] == ["cp-member-9"]
    db.refresh(cellular)
    assert cellular.order_ref is None


def test_the_same_device_id_on_both_accounts_tags_the_provisioned_row(
    db: Session, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    user, provisioned = make_provisioned_connection()
    on_provisioned = add_device(db, provisioned, "shared-1", hash_device_id="shared-hash")
    _, own = self_linked_connection(db, user=user)
    on_own = add_device(db, own, "shared-1", hash_device_id="shared-hash")

    tagged = set_order_ref(db, user_id=user.id, device_id="shared-1", order_ref="ORDER-1")

    assert tagged is not None
    assert tagged.id == on_provisioned.id
    db.refresh(on_own)
    assert on_own.order_ref is None


def test_a_device_on_a_revoked_self_linked_account_is_none_and_untouched(db: Session) -> None:
    user, own = _body_comp_member(db)
    own.status = ConnectionStatus.REVOKED
    db.flush()

    assert set_order_ref(db, user_id=user.id, device_id="bodycomp-1", order_ref="ORDER-1") is None
    assert db.query(WithingsDevice).filter_by(user_connection_id=own.id, device_id="bodycomp-1").one().order_ref is None


def test_another_members_self_linked_device_is_none_and_untouched(db: Session) -> None:
    _, own = _body_comp_member(db)
    stranger, _ = self_linked_connection(db)

    assert set_order_ref(db, user_id=stranger.id, device_id="bodycomp-1", order_ref="ORDER-1") is None
    assert db.query(WithingsDevice).filter_by(user_connection_id=own.id, device_id="bodycomp-1").one().order_ref is None
