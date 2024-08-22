"""Module defining custom exceptions for handling errors."""


class NotAPoolError(Exception):
    """Error raised when a utxo is supplied and it does not contain pool data."""

    pass


class InvalidPoolError(Exception):
    """Error raised when a utxo has pool data, but it is formatted incorrectly."""

    pass


class InvalidLPError(Exception):
    """Error raised when no LP is found in a pool utxo, and LP is expected."""

    pass


class NoAssetsError(Exception):
    """Error raised when no assets are in the pool, or it only contains lovelace."""

    pass
