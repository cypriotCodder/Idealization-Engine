"""Hashes shared by every scraper: text_hash for repost dedup, author_hash for privacy."""

import hashlib
import hmac
import unicodedata


def normalize_text(text: str) -> str:
    """Reduce `text` to the form that two reposts of the same complaint should share."""
    # NFKC folds non-breaking spaces, ligatures and full-width chars to their plain forms.
    # I'm leaving punctuation and URLs alone on purpose: stripping them catches a few more
    # reposts but risks merging two different short complaints, and a dropped row is gone.
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def author_hash(platform: str, author_id: str, salt: str) -> str:
    """Salted hash of an author, so we can count unique authors without keeping usernames."""
    # platform goes into the message so the same handle on two sites isn't linked by accident
    message = f"{platform}:{author_id}".encode()
    return hmac.new(salt.encode("utf-8"), message, hashlib.sha256).hexdigest()
