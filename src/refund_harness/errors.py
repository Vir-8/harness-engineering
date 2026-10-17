"""Failures of the model call. Adapters raise these; the HTTP layer maps them to status codes.
Defined here (not in an adapter) so every provider adapter raises the same errors."""


class ModelError(Exception):
    """Base class: something went wrong talking to the model."""


class ReplyParseError(ModelError):
    """The model answered, but the answer wasn't usable."""


class ModelTimeoutError(ModelError):
    """The model didn't answer in time."""


class ModelCallError(ModelError):
    """The call itself failed (e.g. the CLI exited non-zero)."""
