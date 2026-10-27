class UserError(ValueError):
    """A safe, user-facing error without message contents or arbitrary file paths."""


class NoiseDraftConflict(UserError):
    pass
