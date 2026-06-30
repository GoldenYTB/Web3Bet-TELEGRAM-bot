"""
models.py — All data models in one place.

Section 1 — SQLAlchemy ORM (Postgres tables)
  Base, UserORM, BalanceORM, Transaction, Withdrawal, HouseRake

Section 2 — In-memory dataclasses (bot runtime state)
  User, Balance (simple), PendingGame, ActiveGame, Store

The ORM models are prefixed with their purpose to avoid confusion with the
lightweight in-memory versions used by the Telegram bot during a session.
In production the bot's Store should be replaced with async DB queries,
but the in-memory version works fine for demos and single-instance deploys.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime,
    Enum as SAEnum, ForeignKey, Index, Numeric, String, Text,
    UniqueConstraint, func, select, update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ══════════════════════════════════════════════════════════════════════════════
#  Section 1 — SQLAlchemy ORM models (persistent, Postgres)
# ══════════════════════════════════════════════════════════════════════════════

class Base(DeclarativeBase):
    pass


# ── Enums (ORM layer) ─────────────────────────────────────────────────────────

class TxType(str, enum.Enum):
    DEPOSIT        = "deposit"
    WITHDRAWAL     = "withdrawal"
    WITHDRAWAL_FEE = "withdrawal_fee"
    GAME_DEBIT     = "game_debit"
    GAME_WIN       = "game_win"
    GAME_REFUND    = "game_refund"
    RAKE           = "rake"


class TxStatus(str, enum.Enum):
    PENDING      = "pending"
    COMPLETED    = "completed"
    FAILED       = "failed"
    ROLLED_BACK  = "rolled_back"


class WithdrawalStatus(str, enum.Enum):
    QUEUED    = "queued"
    BROADCAST = "broadcast"
    CONFIRMED = "confirmed"
    FAILED    = "failed"


# ── ORM tables ────────────────────────────────────────────────────────────────

class UserORM(Base):
    """Registered Telegram user (persistent)."""
    __tablename__ = "users"

    id:          Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username:    Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    balances:     Mapped[List["BalanceORM"]]  = relationship("BalanceORM",  back_populates="user")
    transactions: Mapped[List["Transaction"]] = relationship("Transaction", back_populates="user")


class BalanceORM(Base):
    """On-chain balance per user / network / token (persistent). amount >= 0 enforced."""
    __tablename__ = "balances"

    id:           Mapped[int]     = mapped_column(primary_key=True)
    user_id:      Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    network:      Mapped[str]     = mapped_column(String(20), nullable=False)
    token_symbol: Mapped[str]     = mapped_column(String(20), nullable=False)
    amount:       Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    updated_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["UserORM"] = relationship("UserORM", back_populates="balances")

    __table_args__ = (
        UniqueConstraint("user_id", "network", "token_symbol", name="uq_balance_user_net_token"),
        CheckConstraint("amount >= 0", name="ck_balance_non_negative"),
        Index("ix_balance_user_net_tok", "user_id", "network", "token_symbol"),
    )


class Transaction(Base):
    """Immutable append-only ledger. Rows are NEVER updated or deleted."""
    __tablename__ = "transactions"

    id:             Mapped[str]      = mapped_column(String(36), primary_key=True)
    user_id:        Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    type:           Mapped[TxType]   = mapped_column(SAEnum(TxType,   name="tx_type"),   nullable=False)
    status:         Mapped[TxStatus] = mapped_column(SAEnum(TxStatus, name="tx_status"), nullable=False,
                                                     default=TxStatus.PENDING)
    network:        Mapped[str]      = mapped_column(String(20), nullable=False)
    token_symbol:   Mapped[str]      = mapped_column(String(20), nullable=False)
    amount:         Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    direction:      Mapped[str]      = mapped_column(String(8), nullable=False)   # "credit" | "debit"
    balance_before: Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    balance_after:  Mapped[Decimal]  = mapped_column(Numeric(36, 18), nullable=False)
    game_id:        Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    tx_hash:        Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    related_tx_id:  Mapped[Optional[str]] = mapped_column(String(36),  nullable=True)
    note:           Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:     Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user: Mapped["UserORM"] = relationship("UserORM", back_populates="transactions")

    __table_args__ = (
        Index("ix_tx_user_created", "user_id", "created_at"),
        Index("ix_tx_hash", "tx_hash"),
    )


class Withdrawal(Base):
    """Pending on-chain broadcast record."""
    __tablename__ = "withdrawals"

    id:             Mapped[str]              = mapped_column(String(36), primary_key=True)
    transaction_id: Mapped[str]              = mapped_column(String(36), ForeignKey("transactions.id"), nullable=False)
    user_id:        Mapped[int]              = mapped_column(BigInteger, nullable=False)
    network:        Mapped[str]              = mapped_column(String(20), nullable=False)
    token_symbol:   Mapped[str]              = mapped_column(String(20), nullable=False)
    to_address:     Mapped[str]              = mapped_column(String(128), nullable=False)
    gross_amount:   Mapped[Decimal]          = mapped_column(Numeric(36, 18), nullable=False)
    fee_amount:     Mapped[Decimal]          = mapped_column(Numeric(36, 18), nullable=False)
    net_amount:     Mapped[Decimal]          = mapped_column(Numeric(36, 18), nullable=False)
    status:         Mapped[WithdrawalStatus] = mapped_column(
        SAEnum(WithdrawalStatus, name="withdrawal_status"), nullable=False,
        default=WithdrawalStatus.QUEUED
    )
    tx_hash:      Mapped[Optional[str]]      = mapped_column(String(128), nullable=True)
    created_at:   Mapped[datetime]           = mapped_column(DateTime(timezone=True), server_default=func.now())
    broadcast_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_withdrawal_user",   "user_id"),
        Index("ix_withdrawal_status", "status"),
    )


class HouseRake(Base):
    """Accumulated rake per network / token."""
    __tablename__ = "house_rake"

    id:           Mapped[int]     = mapped_column(primary_key=True)
    network:      Mapped[str]     = mapped_column(String(20), nullable=False)
    token_symbol: Mapped[str]     = mapped_column(String(20), nullable=False)
    total_rake:   Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    updated_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("network", "token_symbol", name="uq_rake_net_token"),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Section 2 — In-memory dataclasses (bot session state)
# ══════════════════════════════════════════════════════════════════════════════

class GameStatus(str, enum.Enum):
    WAITING   = "waiting"
    ACTIVE    = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GameType(str, enum.Enum):
    DICE    = "dice"
    BOWLING = "bowling"


@dataclass
class Balance:
    """Lightweight in-memory balance entry."""
    token_symbol: str
    amount: Decimal = Decimal("0")

    def __str__(self) -> str:
        return f"{self.amount:.4f} {self.token_symbol}"


@dataclass
class User:
    """In-memory Telegram user with balances and game stats."""
    telegram_id:   int
    username:      str
    first_name:    str
    registered_at: float = field(default_factory=time.time)

    # "NETWORK:TOKEN" → Balance
    balances:     Dict[str, Balance] = field(default_factory=dict)
    games_played: int     = 0
    games_won:    int     = 0
    total_wagered: Decimal = Decimal("0")
    total_won:     Decimal = Decimal("0")

    @property
    def win_rate(self) -> float:
        return (self.games_won / self.games_played * 100) if self.games_played else 0.0

    def get_balance(self, network: str, token: str) -> Decimal:
        return self.balances.get(f"{network}:{token}", Balance(token)).amount

    def add_balance(self, network: str, token: str, amount: Decimal) -> None:
        key = f"{network}:{token}"
        if key not in self.balances:
            self.balances[key] = Balance(token)
        self.balances[key].amount += amount

    def deduct_balance(self, network: str, token: str, amount: Decimal) -> bool:
        key = f"{network}:{token}"
        current = self.balances.get(key, Balance(token)).amount
        if current < amount:
            return False
        self.balances[key].amount -= amount
        return True

    def display_name(self) -> str:
        return f"@{self.username}" if self.username else self.first_name


@dataclass
class PendingGame:
    """A player sitting in the matchmaking queue."""
    game_id:      str     = field(default_factory=lambda: str(uuid.uuid4()))
    player_id:    int     = 0
    game_type:    GameType = GameType.DICE
    network:      str     = "BSC"
    token_symbol: str     = "USDT"
    wager_amount: Decimal = Decimal("0")
    created_at:   float   = field(default_factory=time.time)

    @property
    def queue_key(self) -> str:
        return f"{self.game_type.value}:{self.network}:{self.token_symbol}:{self.wager_amount}"


@dataclass
class ActiveGame:
    """A resolved or in-progress game between two players."""
    game_id:       str
    game_type:     GameType
    network:       str
    token_symbol:  str
    wager_amount:  Decimal
    player1_id:    int
    player2_id:    int
    status:        GameStatus = GameStatus.ACTIVE
    winner_id:     Optional[int] = None
    player1_score: int     = 0
    player2_score: int     = 0
    house_fee:     Decimal = Decimal("0")
    winner_payout: Decimal = Decimal("0")
    final_seed:    str     = ""
    created_at:    float   = field(default_factory=time.time)
    resolved_at:   Optional[float] = None


# ── In-memory store ───────────────────────────────────────────────────────────

class Store:
    """
    Central in-memory store. Lives on Application.bot_data["store"].
    Swap individual methods for async DB queries when migrating to full ORM.
    """

    def __init__(self) -> None:
        self.users:         Dict[int, User]        = {}
        self.pending_games: Dict[str, PendingGame] = {}
        self.active_games:  Dict[str, ActiveGame]  = {}
        self._queue:        Dict[str, List[PendingGame]] = {}

    def get_or_create_user(
        self, telegram_id: int, username: str, first_name: str
    ) -> tuple[User, bool]:
        if telegram_id in self.users:
            return self.users[telegram_id], False
        user = User(telegram_id=telegram_id, username=username or "", first_name=first_name)
        user.add_balance("BSC",    "USDT", Decimal("100"))
        user.add_balance("SOLANA", "SOL",  Decimal("1"))
        self.users[telegram_id] = user
        return user, True

    def get_user(self, telegram_id: int) -> Optional[User]:
        return self.users.get(telegram_id)

    def leaderboard(self, limit: int = 10) -> List[User]:
        return sorted(
            self.users.values(),
            key=lambda u: (u.games_won, u.total_won),
            reverse=True,
        )[:limit]

    def enqueue(self, pending: PendingGame) -> Optional[PendingGame]:
        key   = pending.queue_key
        queue = self._queue.setdefault(key, [])
        for i, existing in enumerate(queue):
            if existing.player_id != pending.player_id:
                queue.pop(i)
                self.pending_games.pop(existing.game_id, None)
                return existing
        queue.append(pending)
        self.pending_games[pending.game_id] = pending
        return None

    def dequeue(self, game_id: str) -> Optional[PendingGame]:
        pending = self.pending_games.pop(game_id, None)
        if pending:
            queue = self._queue.get(pending.queue_key, [])
            self._queue[pending.queue_key] = [g for g in queue if g.game_id != game_id]
        return pending

    def get_pending_for_player(self, player_id: int) -> Optional[PendingGame]:
        return next((g for g in self.pending_games.values() if g.player_id == player_id), None)
