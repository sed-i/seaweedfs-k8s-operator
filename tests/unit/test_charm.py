# Copyright 2025 him
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from ops import testing

from charm import SeaweedfsK8S


def test_pebble_ready():
    # Arrange:
    ctx = testing.Context(SeaweedfsK8S)
    container = testing.Container("seaweedfs", can_connect=True)
    state_in = testing.State(containers={container})

    # Act:
    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)

    # Assert:
    assert state_out.unit_status == testing.ActiveStatus()
