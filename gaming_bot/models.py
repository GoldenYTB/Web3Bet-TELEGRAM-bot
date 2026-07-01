"""
models.py — SQLAlchemy ORM tables + in-memory runtime dataclasses.
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
    Enum as SAEnum, ForeignKey, Index, Numeric,
    String, Text, UniqueConstraint, func, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from typing import List as SAList


# ── ORM Base ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Enums ──────────────────────────────────────────────────────────────────────

class TxType(str, enum.Enum):
    DEPOSIT    = "deposit"
    WITHDRAWAL = "withdrawal"
    GAME_WIN   = "game_win"
    GAME_LOSS  = "game_loss"
    GAME_REFUND= "game_refund"
    TIP_SENT   = "tip_sent"
    TIP_RECV   = "tip_received"
    REFERRAL   = "referral"
    PROMO      = "promo"
    RAKE       = "rake"

class TxStatus(str, enum.Enum):
    PENDING   = "pending"
    COMPLETED = "completed"
    FAILED    = "failed"

class WithdrawalStatus(str, enum.Enum):
    QUEUED    = "queued"
    BROADCAST = "broadcast"
    CONFIRMED = "confirmed"
    FAILED    = "failed"


# ── ORM Models ────────────────────────────────────────────────────────────────

class UserORM(Base):
    __tablename__ = "users"
    id:          Mapped[int]  = mapped_column(primary_key=True)
    telegram_id: Mapped[int]  = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username:    Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    referred_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    balances:     Mapped[SAList["BalanceORM"]]  = relationship("BalanceORM",  back_populates="user")
    transactions: Mapped[SAList["Transaction"]] = relationship("Transaction", back_populates="user")


class BalanceORM(Base):
    __tablename__ = "balances"
    id:           Mapped[int]     = mapped_column(primary_key=True)
    user_id:      Mapped[int]     = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    network:      Mapped[str]     = mapped_column(String(20), nullable=False)
    token_symbol: Mapped[str]     = mapped_column(String(20), nullable=False)
    amount:       Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("0"))
    updated_at:   Mapped[datetime]= mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["UserORM"] = relationship("UserORM", back_populates="balances")
    __table_args__ = (
        UniqueConstraint("user_id", "network", "token_symbol", name="uq_balance"),
        CheckConstraint("amount >= 0", name="ck_balance_positive"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    id:             Mapped[str]      = mapped_column(String(36), primary_key=True)
    user_id:        Mapped[int]      = mapped_column(BigInteger, ForeignKey("users.id"))
    type:           Mapped[TxType]   = mapped_column(SAEnum(TxType,   name="tx_type"))
    status:         Mapped[TxStatus] = mapped_column(SAEnum(TxStatus, name="tx_status"), default=TxStatus.PENDING)
    network:        Mapped[str]      = mapped_column(String(20))
    token_symbol:   Mapped[str]      = mapped_column(String(20))
    amount:         Mapped[Decimal]  = mapped_column(Numeric(36, 18))
    direction:      Mapped[str]      = mapped_column(String(8))    # "credit" | "debit"
    balance_before: Mapped[Decimal]  = mapped_column(Numeric(36, 18))
    balance_after:  Mapped[Decimal]  = mapped_column(Numeric(36, 18))
    tx_hash:        Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    game_id:        Mapped[Optional[str]] = mapped_column(String(36),  nullable=True)
    note:           Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:     Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    user: Mapped["UserORM"] = relationship("UserORM", back_populates="transactions")
    __table_args__ = (Index("ix_tx_user_date", "user_id", "created_at"),)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id:             Mapped[str]              = mapped_column(String(36), primary_key=True)
    user_id:        Mapped[int]              = mapped_column(BigInteger, nullable=False)
    network:        Mapped[str]              = mapped_column(String(20))
    token_symbol:   Mapped[str]              = mapped_column(String(20))
    to_address:     Mapped[str]              = mapped_column(String(128))
    gross_amount:   Mapped[Decimal]          = mapped_column(Numeric(36, 18))
    fee_amount:     Mapped[Decimal]          = mapped_column(Numeric(36, 18))
    net_amount:     Mapped[Decimal]          = mapped_column(Numeric(36, 18))
    status:         Mapped[WithdrawalStatus] = mapped_column(SAEnum(WithdrawalStatus, name="w_status"), default=WithdrawalStatus.QUEUED)
    tx_hash:        Mapped[Optional[str]]    = mapped_column(String(128), nullable=True)
    created_at:     Mapped[datetime]         = mapped_column(DateTime(timezone=True), server_default=func.now())


class HouseRake(Base):
    __tablename__ = "house_rake"
    id:           Mapped[int]     = mapped_column(primary_key=True)
    network:      Mapped[str]     = mapped_column(String(20))
    token_symbol: Mapped[str]     = mapped_column(String(20))
    total_rake:   Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"))
    updated_at:   Mapped[datetime]= mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("network", "token_symbol", name="uq_rake"),)


# ── In-memory runtime dataclasses ─────────────────────────────────────────────

@dataclass
class Balance:
    token_symbol: str
    amount:       Decimal = Decimal("0")


@dataclass
class User:
    """In-memory user with full profile data."""
    telegram_id:    int
    username:       str
    first_name:     str
    registered_at:  float = field(default_factory=time.time)

    # ── UNIFIED USD BALANCE ───────────────────────────────────────────────────
    # All deposits converted to USD at time of deposit.
    # All bets in USD. Withdrawals swap USD to chosen coin via ChangeNow.
    usd_balance:    Decimal = Decimal("0")

    # Preferred payout coin (asked on first withdrawal, saved here)
    preferred_coin: str     = ""   # e.g. "SOL", "LTC", "USDT" — empty = not set yet

    # Stats
    games_played:   int     = 0
    games_won:      int     = 0
    total_wagered:  Decimal = Decimal("0")   # in USD
    total_won:      Decimal = Decimal("0")   # in USD
    biggest_win:    Decimal = Decimal("0")   # in USD
    favourite_game: str     = ""

    # Referral
    referred_by:    int = 0
    referral_count: int = 0

    # Deposit wallets: coin_symbol → address (generated on demand)
    deposit_addresses: Dict[str, str] = field(default_factory=dict)
    deposit_keys:      Dict[str, str] = field(default_factory=dict)   # coin → enc_key

    # Deposit coin holdings (for accurate withdrawal swap amounts)
    # coin_symbol → Decimal amount  (raw coin held before swap-out)
    coin_holdings:  Dict[str, Decimal] = field(default_factory=dict)

    # Game and promo history
    game_history: List[str] = field(default_factory=list)
    used_promos:  List[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return (self.games_won / self.games_played * 100) if self.games_played else 0.0

    @property
    def rank(self) -> str:
        w = self.games_won
        if w >= 500: return "💎 Diamond"
        if w >= 200: return "🥇 Gold III"
        if w >= 100: return "🥈 Silver"
        if w >= 50:  return "🥉 Bronze"
        if w >= 10:  return "⚔️ Iron II"
        return "🪨 Iron I"

    # ── Balance helpers ────────────────────────────────────────────────────────

    def credit_usd(self, amount: Decimal) -> None:
        """Add USD to balance (called after deposit price conversion)."""
        self.usd_balance = (self.usd_balance + amount).quantize(Decimal("0.01"))

    def debit_usd(self, amount: Decimal) -> bool:
        """Deduct USD. Returns False if insufficient."""
        if self.usd_balance < amount:
            return False
        self.usd_balance = (self.usd_balance - amount).quantize(Decimal("0.01"))
        return True

    def add_coin_holding(self, coin: str, amount: Decimal) -> None:
        """Track raw coin amounts deposited (for withdrawal accounting)."""
        self.coin_holdings[coin] = self.coin_holdings.get(coin, Decimal("0")) + amount

    def display_name(self) -> str:
        return f"@{self.username}" if self.username else self.first_name


@dataclass
class Store:
    """Central in-memory store — one instance lives on app.bot_data['store']."""

    users:        Dict[int, User]          = field(default_factory=dict)
    active_games: Dict[str, "GroupGame"]   = field(default_factory=dict)
    history:      List["GroupGame"]        = field(default_factory=list)

    # ── House fund (USD) ────────────────────────────────────────────────────────
    # All rake from games goes here. Used to pay out bot game winners.
    # Admin can deposit to house addresses to top this up.
    house_balance_usd: Decimal = Decimal("0")

    # House coin holdings: coin → amount (what's actually sitting in house wallets)
    house_coin_holdings: Dict[str, Decimal] = field(default_factory=dict)

    # Total stats
    total_rake_collected: Decimal = Decimal("0")   # USD
    total_volume_usd:     Decimal = Decimal("0")   # USD wagered all time

    def get_or_create_user(self, telegram_id: int, username: str, first_name: str) -> tuple[User, bool]:
        if telegram_id in self.users:
            return self.users[telegram_id], False
        user = User(telegram_id=telegram_id, username=username or "", first_name=first_name)
        self.users[telegram_id] = user
        return user, True

    def get_user(self, telegram_id: int) -> Optional[User]:
        return self.users.get(telegram_id)

    def leaderboard(self, limit: int = 10) -> List[User]:
        return sorted(self.users.values(),
                      key=lambda u: (u.games_won, u.total_won), reverse=True)[:limit]

    # ── House fund helpers ─────────────────────────────────────────────────────

    def add_rake(self, amount_usd: Decimal) -> None:
        """Add rake to house fund."""
        self.house_balance_usd     = (self.house_balance_usd + amount_usd).quantize(Decimal("0.01"))
        self.total_rake_collected  = (self.total_rake_collected + amount_usd).quantize(Decimal("0.01"))

    def credit_house_usd(self, amount_usd: Decimal) -> None:
        """Admin deposited funds — add to house balance."""
        self.house_balance_usd = (self.house_balance_usd + amount_usd).quantize(Decimal("0.01"))

    def debit_house_usd(self, amount_usd: Decimal) -> bool:
        """Pay out from house fund. Returns False if insufficient."""
        if self.house_balance_usd < amount_usd:
            return False
        self.house_balance_usd = (self.house_balance_usd - amount_usd).quantize(Decimal("0.01"))
        return True

    def add_house_coin(self, coin: str, amount: Decimal) -> None:
        self.house_coin_holdings[coin] = (
            self.house_coin_holdings.get(coin, Decimal("0")) + amount
        )

    # ── Game management ────────────────────────────────────────────────────────

    def add_game(self, game: "GroupGame") -> None:
        self.active_games[game.game_id] = game

    def get_game(self, game_id: str) -> Optional["GroupGame"]:
        return self.active_games.get(game_id)

    def remove_game(self, game_id: str) -> None:
        game = self.active_games.pop(game_id, None)
        if game:
            self.history.append(game)

    def get_game_for_chat(self, chat_id: int) -> Optional["GroupGame"]:
        return next(
            (g for g in self.active_games.values()
             if g.chat_id == chat_id and g.status.value in ("waiting", "active")),
            None,
        )

    def get_games_for_user(self, user_id: int, limit: int = 5) -> List["GroupGame"]:
        return [g for g in reversed(self.history)
                if g.creator_id == user_id or g.joiner_id == user_id][:limit]


# Avoid circular import — GroupGame is defined in games.py
from .games import GroupGame  # noqa: E402
