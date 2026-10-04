class ManualReviewRequired(Exception):
    """Raised when the workflow cannot safely determine a single outcome."""
    pass