"""The merge helpers: validity windows, zero withdrawals and reference inputs.

Each helper merges into whatever a builder already holds, so several legs can
contribute to one transaction without overwriting each other.
"""

from __future__ import annotations

import pytest
from pycardano import TransactionBuilder

from charli3_dendrite.lending.transactions.errors import LendingBuildError
from charli3_dendrite.lending.transactions.errors import ValidityWindowError
from charli3_dendrite.lending.transactions.infra import EvalContext
from charli3_dendrite.lending.transactions.validity import merge_validity


def _builder() -> TransactionBuilder:
    return TransactionBuilder(EvalContext(last_block_slot=0))


def test_validity_intersects_with_an_existing_window():
    tx_builder = _builder()
    tx_builder.ttl = 100

    merge_validity(tx_builder, upper_slot=90, lower_slot=10)
    assert (tx_builder.validity_start, tx_builder.ttl) == (10, 90)

    # A later, looser bound never widens the window.
    merge_validity(tx_builder, upper_slot=95, lower_slot=5)
    assert (tx_builder.validity_start, tx_builder.ttl) == (10, 90)

    # A tighter one narrows it.
    merge_validity(tx_builder, upper_slot=80, lower_slot=20)
    assert (tx_builder.validity_start, tx_builder.ttl) == (20, 80)


def test_validity_on_an_empty_builder_sets_the_bounds_given():
    tx_builder = _builder()
    merge_validity(tx_builder, upper_slot=90)
    assert (tx_builder.validity_start, tx_builder.ttl) == (None, 90)

    merge_validity(tx_builder, upper_slot=90, lower_slot=None)
    assert tx_builder.validity_start is None


def test_validity_lower_bound_at_or_past_the_upper_raises():
    tx_builder = _builder()
    tx_builder.ttl = 90
    with pytest.raises(ValidityWindowError):
        merge_validity(tx_builder, upper_slot=100, lower_slot=95)
    # The ttl is exclusive (invalid-hereafter): a start equal to it is empty too.
    with pytest.raises(ValidityWindowError):
        merge_validity(tx_builder, upper_slot=100, lower_slot=90)
    # A refused merge leaves the builder as it was.
    assert (tx_builder.validity_start, tx_builder.ttl) == (None, 90)


def test_validity_upper_bound_below_an_existing_start_raises():
    tx_builder = _builder()
    tx_builder.validity_start = 50
    with pytest.raises(ValidityWindowError) as info:
        merge_validity(tx_builder, upper_slot=40)
    assert isinstance(info.value, LendingBuildError)
    assert isinstance(info.value, ValueError)
    assert (tx_builder.validity_start, tx_builder.ttl) == (50, None)
