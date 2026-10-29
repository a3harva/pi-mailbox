class MailboxError(Exception):
    """A problem the caller can fix: bad input, unknown id, alias taken, limit hit."""
