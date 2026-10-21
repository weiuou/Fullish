"""Safe provider error codes; never carry upstream bodies or credentials."""


class ModelError(RuntimeError):
    """A safe error code, never an upstream response body or credential."""
