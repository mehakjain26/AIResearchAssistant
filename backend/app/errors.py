class AppError(Exception):
    """An error with an HTTP status and a user-facing message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
