"""Cellular-only paths stay cellular-only (spec 2026-10-27 O4).

A Body Comp Robin sold, tagged on the member's OWN Withings account, is "the scale we sold" for
readings and pushes — and nothing more. It sits on no cellular plan: ``endpartnerprogram`` must never
be called for it, and the SDK session / recovery, which act on an account WE created, must not see it.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, UserConnection
from app.services.providers.withings.connections import is_device_connection
from app.services.providers.withings.end_program import EndProgramResult
from app.services.providers.withings.sdk_provisioning import recover_sdk_account
from app.services.providers.withings.sdk_users import WithingsSdkUserError
from app.services.providers.withings.strategy import WithingsStrategy
from tests.providers.withings.conftest import ProvisionedConnectionMaker, add_device, self_linked_connection

_END = "app.services.providers.withings.strategy.end_partner_program"
_MAC = "00:24:e4:aa:bb:cc"


@pytest.fixture
def withings_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "withings_client_id", "client-id")
    monkeypatch.setattr(settings, "withings_client_secret", SecretStr("client-secret"))


def _sold_on_own_account(db: Session) -> tuple[User, UserConnection]:
    user, own = self_linked_connection(db, external_user_id=f"cp-{uuid4().hex}")
    add_device(db, own, "bodycomp-1", hash_device_id="bc-hash", order_ref="ORDER-1")
    return user, own


def test_a_sold_device_does_not_make_the_members_own_account_a_device_connection(db: Session) -> None:
    _, own = _sold_on_own_account(db)

    assert is_device_connection(db, own.id) is False


def test_disconnecting_it_never_ends_a_cellular_programme(db: Session, withings_credentials: None) -> None:
    user, own = _sold_on_own_account(db)
    strategy = WithingsStrategy()

    with (
        patch.object(strategy.webhook_service, "remove_user"),
        patch.object(strategy, "_device_mac_addresses", return_value=[_MAC]) as macs,
        patch(_END) as end,
    ):
        strategy.on_disconnect(db, user.id, connection_id=own.id)

    macs.assert_not_called()
    end.assert_not_called()


def test_positive_control_a_provisioned_connection_does_reach_end_program(
    db: Session, withings_credentials: None, make_provisioned_connection: ProvisionedConnectionMaker
) -> None:
    # Without this, the test above would also pass if end_program were unreachable for everyone.
    user, provisioned = make_provisioned_connection()
    strategy = WithingsStrategy()

    with (
        patch.object(strategy.webhook_service, "remove_user"),
        patch.object(strategy, "_device_mac_addresses", return_value=[_MAC]),
        patch(_END, return_value=[EndProgramResult(mac_address=_MAC, ok=True)]) as end,
    ):
        strategy.on_disconnect(db, user.id, connection_id=provisioned.id)

    end.assert_called_once()


def test_recovery_refuses_a_member_whose_only_sold_device_is_on_their_own_account(db: Session) -> None:
    user, _ = _sold_on_own_account(db)

    with pytest.raises(WithingsSdkUserError) as exc:
        recover_sdk_account(
            db, user_id=user.id, client_id="client-id", client_secret="client-secret", redirect_uri="https://x"
        )

    assert exc.value.not_found is True
