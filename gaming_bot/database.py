"""
database.py — Async database layer.

Provides
--------
  create_pool()           — Build SQLAlchemy async engine
  get_session()           — Async context manager → scoped AsyncSession
  get_session_no_autocommit() — Full transaction control (SAVEPOINTs)
  ping() / pool_status()  — Health checks
  close_pool()            — Graceful shutdown
  run_migrations()        — Dev helper: create_all()
  TransactionManager      — Atomic balance operations with SELECT FOR UPDATE
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import AsyncGenerator, Dict, List, Optional

from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from .models import (
    BalanceORM, Base, HouseRake, Transaction, TxStatus, TxType,
    UserORM, Withdrawal, WithdrawalStatus,
)

logger = logging.getLogger(__name__)

HOUSE_USER_ID = 0   # sentinel for house rake rows
_DECIMAL_PREC = Decimal("0.00000001")

# ── Module-level singletons ───────────────────────────────────────────────────

_engine:  Optional[AsyncEngine]      = None
_factory: Optional[async_sessionmaker] = None


# ── Pool management ───────────────────────────────────────────────────────────

def create_pool(
    database_url:  str,
    pool_size:     int  = 10,
    max_overflow:  int  = 20,
    pool_timeout:  int  = 30,
    echo:          bool = False,
) -> AsyncEngine:
    """Create and store the async engine. Call once at startup."""
    global _engine, _factory
    _engine = create_async_engine(
        database_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "command_timeout": 60,
            "server_settings": {"statement_timeout": "60000"},
            # Required for Neon's pooled (PgBouncer transaction-mode) endpoint —
            # PgBouncer does not support prepared statements across connections.
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
        },
    )
    _factory = async_sessionmaker(
        _engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=True, autobegin=True,
    )
    logger.info(
        "DB pool created: url=%s pool=%d+%d timeout=%ds",
        _masked_url(database_url), pool_size, max_overflow, pool_timeout,
    )
    return _engine


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession inside a transaction. Commits on success, rolls back on error."""
    if _factory is None:
        raise RuntimeError("DB pool not initialised. Call create_pool() first.")
    async with _factory() as session:
        async with session.begin():
            try:
                yield session
            except SQLAlchemyError as exc:
                logger.error("DB error — rolling back: %s", exc, exc_info=True)
                raise


@asynccontextmanager
async def get_session_no_autocommit() -> AsyncGenerator[AsyncSession, None]:
    """Full transaction control — useful for multi-step operations with SAVEPOINTs."""
    if _factory is None:
        raise RuntimeError("DB pool not initialised.")
    async with _factory() as session:
        try:
            yield session
        except SQLAlchemyError as exc:
            await session.rollback()
            logger.error("DB error — rolled back: %s", exc, exc_info=True)
            raise


async def ping() -> tuple[bool, str]:
    if _engine is None:
        return False, "engine not initialised"
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "ok"
    except OperationalError as exc:
        return False, f"connection error: {exc}"
    except Exception as exc:
        return False, f"unexpected error: {exc}"


async def pool_status() -> dict:
    if _engine is None:
        return {"status": "not_initialised"}
    pool = _engine.pool
    return {
        "status":      "ok",
        "size":        pool.size(),
        "checked_in":  pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow":    pool.overflow(),
    }


async def close_pool() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
        _engine  = None
        _factory = None
        logger.info("DB pool closed.")


async def run_migrations(engine: Optional[AsyncEngine] = None) -> None:
    """Create all ORM tables. Development helper — use Alembic in production."""
    target = engine or _engine
    if target is None:
        raise RuntimeError("No engine available for migration.")
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB tables created / verified.")


def _masked_url(url: str) -> str:
    if "@" in url:
        proto_creds, host = url.split("@", 1)
        if ":" in proto_creds:
            parts = proto_creds.rsplit(":", 1)
            return f"{parts[0]}:***@{host}"
    return url


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TxReceipt:
    tx_id:          str
    type:           TxType
    user_id:        int
    network:        str
    token_symbol:   str
    amount:         Decimal
    direction:      str
    balance_before: Decimal
    balance_after:  Decimal
    game_id:        Optional[str] = None
    note:           Optional[str] = None


@dataclass(frozen=True)
class GameSettlementReceipt:
    winner_tx:      TxReceipt
    loser_tx:       Optional[TxReceipt]
    rake_tx:        TxReceipt
    prize_pool:     Decimal
    rake_amount:    Decimal
    winner_payout:  Decimal
    tie:            bool


@dataclass(frozen=True)
class WithdrawalReceipt:
    withdrawal_id: str
    tx_id:         str
    gross_amount:  Decimal
    fee_amount:    Decimal
    net_amount:    Decimal
    to_address:    str


# ── Domain exceptions ─────────────────────────────────────────────────────────

class TransactionError(Exception):
    def __init__(self, msg: str, tx_id: str = "-"):
        super().__init__(msg); self.tx_id = tx_id

class InsufficientFundsError(TransactionError):      pass
class DuplicateTransactionError(TransactionError):   pass
class WithdrawalBelowMinimumError(TransactionError): pass


# ── Transaction Manager ───────────────────────────────────────────────────────

class TransactionManager:
    """
    Atomic balance operations.  All mutations go through here.
    Uses SELECT FOR UPDATE + asyncio.Lock for double-layer race protection.

    Usage
    -----
        tm = TransactionManager(engine)

        async with tm.transaction() as session:
            receipt = await tm.credit_deposit(session, ...)

        async with tm.transaction() as session:
            receipt = await tm.debit_for_game(session, ...)
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine   = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        self._user_locks: Dict[int, asyncio.Lock] = {}
        self._locks_meta  = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide a session inside an auto-committing transaction."""
        async with self._sessions() as session:
            async with session.begin():
                try:
                    yield session
                except Exception:
                    raise

    # ── Public API ─────────────────────────────────────────────────────────────

    async def credit_deposit(
        self, session: AsyncSession,
        user_id: int, network: str, token_symbol: str,
        amount: Decimal, tx_hash: str, note: str = "",
    ) -> TxReceipt:
        """Credit a confirmed on-chain deposit. Idempotent on tx_hash."""
        tx_id = _new_id()
        if amount <= 0:
            raise TransactionError("Deposit amount must be positive.", tx_id)
        await self._assert_unique_hash(session, tx_hash, tx_id)
        balance = await self._get_or_create_balance(session, user_id, network, token_symbol)
        before  = balance.amount
        balance.amount = (balance.amount + amount).quantize(_DECIMAL_PREC)
        return await self._write_tx(
            session, tx_id=tx_id, user_id=user_id,
            type=TxType.DEPOSIT, network=network, token_symbol=token_symbol,
            amount=amount, direction="credit",
            balance_before=before, balance_after=balance.amount,
            tx_hash=tx_hash, note=note or "On-chain deposit confirmed",
        )

    async def debit_for_game(
        self, session: AsyncSession,
        user_id: int, network: str, token_symbol: str,
        amount: Decimal, game_id: str,
    ) -> TxReceipt:
        """Lock and debit wager (SELECT FOR UPDATE)."""
        tx_id = _new_id()
        async with self._user_lock(user_id):
            balance = await self._lock_balance(session, user_id, network, token_symbol)
            if balance is None or balance.amount < amount:
                avail = balance.amount if balance else Decimal("0")
                raise InsufficientFundsError(
                    f"Insufficient {token_symbol}: need {amount}, have {avail}", tx_id
                )
            before         = balance.amount
            balance.amount = (balance.amount - amount).quantize(_DECIMAL_PREC)
            return await self._write_tx(
                session, tx_id=tx_id, user_id=user_id,
                type=TxType.GAME_DEBIT, network=network, token_symbol=token_symbol,
                amount=amount, direction="debit",
                balance_before=before, balance_after=balance.amount,
                game_id=game_id, note=f"Wager held for game {game_id[:8]}",
            )

    async def process_game_win(
        self, session: AsyncSession,
        winner_id: int, loser_id: Optional[int],
        network: str, token_symbol: str,
        wager: Decimal, game_id: str,
        rake_pct: Decimal = Decimal("0.05"),
    ) -> GameSettlementReceipt:
        """Credit winner, accumulate rake. loser_id=None on tie."""
        tx_id      = _new_id()
        prize_pool = (wager * 2).quantize(_DECIMAL_PREC)
        rake       = (prize_pool * rake_pct).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)
        net        = (prize_pool - rake).quantize(_DECIMAL_PREC)
        is_tie     = loser_id is None

        if is_tie:
            each = (net / 2).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)
            bal  = await self._get_or_create_balance(session, winner_id, network, token_symbol)
            b    = bal.amount
            bal.amount = (bal.amount + each).quantize(_DECIMAL_PREC)
            winner_tx = await self._write_tx(
                session, tx_id=tx_id, user_id=winner_id,
                type=TxType.GAME_REFUND, network=network, token_symbol=token_symbol,
                amount=each, direction="credit",
                balance_before=b, balance_after=bal.amount,
                game_id=game_id, note="Tie — 50% refund",
            )
            payout = each
        else:
            bal = await self._get_or_create_balance(session, winner_id, network, token_symbol)
            b   = bal.amount
            bal.amount = (bal.amount + net).quantize(_DECIMAL_PREC)
            winner_tx = await self._write_tx(
                session, tx_id=tx_id, user_id=winner_id,
                type=TxType.GAME_WIN, network=network, token_symbol=token_symbol,
                amount=net, direction="credit",
                balance_before=b, balance_after=bal.amount,
                game_id=game_id, note=f"Won game {game_id[:8]}",
            )
            payout = net

        rake_tx = await self._write_tx(
            session, tx_id=_new_id(), user_id=HOUSE_USER_ID,
            type=TxType.RAKE, network=network, token_symbol=token_symbol,
            amount=rake, direction="credit",
            balance_before=Decimal("0"), balance_after=rake,
            game_id=game_id, note="House rake",
        )
        await self._accumulate_rake(session, network, token_symbol, rake)

        return GameSettlementReceipt(
            winner_tx=winner_tx, loser_tx=None, rake_tx=rake_tx,
            prize_pool=prize_pool, rake_amount=rake, winner_payout=payout, tie=is_tie,
        )

    async def process_refund(
        self, session: AsyncSession,
        user_id: int, network: str, token_symbol: str,
        amount: Decimal, game_id: str,
        original_tx_id: Optional[str] = None,
        reason: str = "Game cancelled",
    ) -> TxReceipt:
        tx_id   = _new_id()
        balance = await self._get_or_create_balance(session, user_id, network, token_symbol)
        before  = balance.amount
        balance.amount = (balance.amount + amount).quantize(_DECIMAL_PREC)
        return await self._write_tx(
            session, tx_id=tx_id, user_id=user_id,
            type=TxType.GAME_REFUND, network=network, token_symbol=token_symbol,
            amount=amount, direction="credit",
            balance_before=before, balance_after=balance.amount,
            game_id=game_id, related_tx_id=original_tx_id, note=reason,
        )

    async def process_withdrawal(
        self, session: AsyncSession,
        user_id: int, network: str, token_symbol: str,
        gross_amount: Decimal, to_address: str,
        fixed_fee: Decimal = Decimal("0.1"),
        pct_fee:   Decimal = Decimal("0.03"),
    ) -> WithdrawalReceipt:
        tx_id     = _new_id()
        fee_pct   = (gross_amount * pct_fee).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)
        total_fee = (fixed_fee + fee_pct).quantize(_DECIMAL_PREC)
        net       = (gross_amount - total_fee).quantize(_DECIMAL_PREC)

        if net <= 0:
            raise WithdrawalBelowMinimumError(
                f"Net after fees ({net}) is non-positive. Min withdrawal: {total_fee + Decimal('0.00000001')}",
                tx_id,
            )

        async with self._user_lock(user_id):
            balance = await self._lock_balance(session, user_id, network, token_symbol)
            if balance is None or balance.amount < gross_amount:
                avail = balance.amount if balance else Decimal("0")
                raise InsufficientFundsError(
                    f"Insufficient {token_symbol}: need {gross_amount}, have {avail}", tx_id
                )
            before         = balance.amount
            balance.amount = (balance.amount - gross_amount).quantize(_DECIMAL_PREC)

            receipt = await self._write_tx(
                session, tx_id=tx_id, user_id=user_id,
                type=TxType.WITHDRAWAL, network=network, token_symbol=token_symbol,
                amount=gross_amount, direction="debit",
                balance_before=before, balance_after=balance.amount,
                note=f"Withdrawal to {to_address} | net={net} fee={total_fee}",
            )
            # Fee row
            await self._write_tx(
                session, tx_id=_new_id(), user_id=HOUSE_USER_ID,
                type=TxType.WITHDRAWAL_FEE, network=network, token_symbol=token_symbol,
                amount=total_fee, direction="credit",
                balance_before=Decimal("0"), balance_after=total_fee,
                related_tx_id=tx_id, note=f"Withdrawal fee from user {user_id}",
            )
            wid = _new_id()
            session.add(Withdrawal(
                id=wid, transaction_id=tx_id, user_id=user_id,
                network=network, token_symbol=token_symbol, to_address=to_address,
                gross_amount=gross_amount, fee_amount=total_fee, net_amount=net,
                status=WithdrawalStatus.QUEUED,
            ))
            return WithdrawalReceipt(
                withdrawal_id=wid, tx_id=tx_id,
                gross_amount=gross_amount, fee_amount=total_fee,
                net_amount=net, to_address=to_address,
            )

    async def rollback_transaction(
        self, session: AsyncSession,
        original_tx_id: str, reason: str = "Manual rollback",
    ) -> TxReceipt:
        tx_id = _new_id()
        orig  = await session.get(Transaction, original_tx_id)
        if orig is None:
            raise TransactionError(f"Transaction {original_tx_id} not found.", tx_id)
        if orig.status == TxStatus.ROLLED_BACK:
            raise TransactionError(f"Transaction {original_tx_id} already rolled back.", tx_id)

        reverse_dir = "credit" if orig.direction == "debit" else "debit"

        async with session.begin_nested():
            if reverse_dir == "credit":
                bal    = await self._get_or_create_balance(session, orig.user_id, orig.network, orig.token_symbol)
                before = bal.amount
                bal.amount = (bal.amount + orig.amount).quantize(_DECIMAL_PREC)
            else:
                bal = await self._lock_balance(session, orig.user_id, orig.network, orig.token_symbol)
                if bal is None or bal.amount < orig.amount:
                    raise InsufficientFundsError("Cannot rollback: insufficient balance", tx_id)
                before = bal.amount
                bal.amount = (bal.amount - orig.amount).quantize(_DECIMAL_PREC)

            orig.status = TxStatus.ROLLED_BACK
            receipt = await self._write_tx(
                session, tx_id=tx_id, user_id=orig.user_id,
                type=orig.type, network=orig.network, token_symbol=orig.token_symbol,
                amount=orig.amount, direction=reverse_dir,
                balance_before=before, balance_after=bal.amount,
                related_tx_id=original_tx_id, note=f"ROLLBACK of {original_tx_id[:8]}: {reason}",
            )
        return receipt

    async def get_balance(
        self, session: AsyncSession, user_id: int, network: str, token_symbol: str
    ) -> Decimal:
        stmt   = select(BalanceORM).where(
            BalanceORM.user_id == user_id,
            BalanceORM.network == network,
            BalanceORM.token_symbol == token_symbol,
        )
        result = await session.execute(stmt)
        bal    = result.scalar_one_or_none()
        return bal.amount if bal else Decimal("0")

    async def get_history(
        self, session: AsyncSession, user_id: int,
        limit: int = 20, offset: int = 0,
    ) -> List[Transaction]:
        stmt = (
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc())
            .limit(limit).offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _get_or_create_balance(
        self, session: AsyncSession, user_id: int, network: str, token_symbol: str
    ) -> BalanceORM:
        stmt   = select(BalanceORM).where(
            BalanceORM.user_id == user_id,
            BalanceORM.network == network,
            BalanceORM.token_symbol == token_symbol,
        )
        result = await session.execute(stmt)
        bal    = result.scalar_one_or_none()
        if bal is None:
            bal = BalanceORM(user_id=user_id, network=network, token_symbol=token_symbol, amount=Decimal("0"))
            session.add(bal)
            await session.flush()
        return bal

    async def _lock_balance(
        self, session: AsyncSession, user_id: int, network: str, token_symbol: str
    ) -> Optional[BalanceORM]:
        """SELECT … FOR UPDATE — acquires a Postgres row-level lock."""
        stmt = (
            select(BalanceORM).where(
                BalanceORM.user_id == user_id,
                BalanceORM.network == network,
                BalanceORM.token_symbol == token_symbol,
            ).with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _write_tx(
        self, session: AsyncSession, *, tx_id: str, user_id: int,
        type: TxType, network: str, token_symbol: str,
        amount: Decimal, direction: str,
        balance_before: Decimal, balance_after: Decimal,
        status: TxStatus = TxStatus.COMPLETED,
        game_id: Optional[str] = None, tx_hash: Optional[str] = None,
        related_tx_id: Optional[str] = None, note: Optional[str] = None,
    ) -> TxReceipt:
        tx = Transaction(
            id=tx_id, user_id=user_id, type=type, status=status,
            network=network, token_symbol=token_symbol,
            amount=amount, direction=direction,
            balance_before=balance_before, balance_after=balance_after,
            game_id=game_id, tx_hash=tx_hash, related_tx_id=related_tx_id, note=note,
        )
        session.add(tx)
        await session.flush()
        return TxReceipt(
            tx_id=tx_id, type=type, user_id=user_id,
            network=network, token_symbol=token_symbol,
            amount=amount, direction=direction,
            balance_before=balance_before, balance_after=balance_after,
            game_id=game_id, note=note,
        )

    async def _assert_unique_hash(self, session: AsyncSession, tx_hash: str, tx_id: str) -> None:
        stmt   = select(Transaction).where(Transaction.tx_hash == tx_hash).limit(1)
        result = await session.execute(stmt)
        if result.scalar_one_or_none() is not None:
            raise DuplicateTransactionError(f"Deposit tx_hash {tx_hash} already processed.", tx_id)

    async def _accumulate_rake(
        self, session: AsyncSession, network: str, token_symbol: str, amount: Decimal
    ) -> None:
        stmt = select(HouseRake).where(
            HouseRake.network == network, HouseRake.token_symbol == token_symbol
        ).with_for_update()
        result = await session.execute(stmt)
        row    = result.scalar_one_or_none()
        if row:
            row.total_rake = (row.total_rake + amount).quantize(_DECIMAL_PREC)
        else:
            session.add(HouseRake(network=network, token_symbol=token_symbol, total_rake=amount))

    @asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncGenerator[None, None]:
        async with self._locks_meta:
            if user_id not in self._user_locks:
                self._user_locks[user_id] = asyncio.Lock()
        async with self._user_locks[user_id]:
            yield


def _new_id() -> str:
    return str(uuid.uuid4())
