"""One-time delivery of a freshly minted API key.

A key's plaintext is shown once at mint and only its hash is stored, which is
right but makes handing one to somebody awkward: pasting a credential into a
Discord DM leaves it in that channel's history forever, and in ours.

So a reveal is a short-lived, single-use, audience-bound envelope. The secret
is stored **encrypted** (Fernet, the same key the webhook store uses) and the
ciphertext is destroyed the moment it is read — after which the row survives
only as a record that it *was* read, and by whom.

Three independent gates, all server-side:

* **single use** — ``viewed_at`` is set in the same transaction that returns
  the secret, so a second request has nothing to return;
* **expiry** — an unclaimed link dies on its own;
* **audience** — a viewer must be signed in and be the key's owner, or an
  admin of the owning group. A leaked URL alone is not enough.
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)

from .base import Base


class ApiKeyReveal(Base):
    __tablename__ = "api_key_reveals"
    __table_args__ = (
        Index("idx_api_key_reveals_key", "api_key_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: The random component of the link. Unique and high-entropy: it is one of
    #: the three gates, not the only one.
    reveal_token = Column(String(64), nullable=False, unique=True)
    api_key_id = Column(BigInteger, ForeignKey("api_keys.id"), nullable=False)

    #: Fernet ciphertext of the plaintext key. NULLed on view, so a database
    #: read after the fact cannot recover a delivered secret.
    secret_ciphertext = Column(Text, nullable=True)

    #: Who may open it. Exactly one is set: a specific user, or any admin of
    #: this group.
    audience_user_id = Column(Integer, nullable=True)
    audience_group_id = Column(Integer, nullable=True)

    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    viewed_at = Column(DateTime, nullable=True)
    viewed_by_user_id = Column(Integer, nullable=True)
