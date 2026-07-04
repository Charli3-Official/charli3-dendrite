"""Offline tests for the lending tx-builder registry."""

import pytest
from charli3_dendrite.lending.danogo.transactions.builder import DanogoTxBuilder
from charli3_dendrite.lending.fluidtokens.transactions.builder import (
    FluidTokensTxBuilder,
)
from charli3_dendrite.lending.registry import available_lending_protocols
from charli3_dendrite.lending.registry import get_lending_builder
from charli3_dendrite.lending.transactions.base import AbstractLendingTxBuilder


def test_danogo_resolves_to_builder_class():
    # The registry maps the canonical name to the CLASS (not an instance), since
    # builders take a backend/config at construction.
    assert get_lending_builder("danogo") is DanogoTxBuilder
    assert issubclass(get_lending_builder("danogo"), AbstractLendingTxBuilder)


def test_lookup_is_case_insensitive():
    assert get_lending_builder("Danogo") is DanogoTxBuilder
    assert get_lending_builder("DANOGO") is DanogoTxBuilder


def test_fluidtokens_resolves_to_builder_class():
    # FluidTokens registers the same way: canonical name -> builder CLASS.
    assert get_lending_builder("fluidtokens") is FluidTokensTxBuilder
    assert issubclass(get_lending_builder("FluidTokens"), AbstractLendingTxBuilder)


def test_builders_listed_among_available():
    assert "danogo" in available_lending_protocols()
    assert "fluidtokens" in available_lending_protocols()


def test_unknown_protocol_raises_with_known_list():
    # The miss error names the requested protocol and the known ones to aid
    # discovery.
    with pytest.raises(KeyError, match="no lending builder registered"):
        get_lending_builder("not_a_protocol")
    with pytest.raises(KeyError, match="danogo"):
        get_lending_builder("not_a_protocol")
