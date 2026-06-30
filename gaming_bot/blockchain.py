"""
blockchain.py — Blockchain monitoring: deposit detection, confirmations, price feeds.

Merges blockchain_client.py + price feed into one module.

Classes
-------
  EVMConnection      — Single EVM RPC wrapper with reconnect
  SolanaConnection   — Solana RPC wrapper with reconnect
  PriceFeed          — CoinGecko price cache (no API key needed)
  BlockchainMonitor  — Orchestrates deposit monitoring across all chains

Usage
-----
    async def on_deposit(event: DepositEvent) -> None:
        if event.confirmed:
            await credit_user(event.address, event.amount, event.token_symbol)

    async with BlockchainMonitor(callback=on_deposit) as monitor:
        monitor.watch("ethereum", "0xUserWallet", label="user:42")
        await asyncio.Event().wait()   # run forever
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Set

import aiohttp
from solana.rpc.async_api import AsyncClient as SolanaClient
from solana.rpc.commitment import Confirmed as SolanaConfirmed
from solders.pubkey import Pubkey
from solders.signature import Signature as SolSignature
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound
from web3.middleware import ExtraDataToPOAMiddleware

from .config import cfg

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC20_ABI_MINIMAL = [
    {"name": "decimals",  "type": "function", "inputs": [], "outputs": [{"name":"","type":"uint8"}], "stateMutability":"view"},
    {"name": "balanceOf", "type": "function",
     "inputs": [{"name":"account","type":"address"}], "outputs":[{"name":"","type":"uint256"}], "stateMutability":"view"},
]

REQUIRED_CONFIRMATIONS: Dict[str, int] = {"ethereum": 12, "bsc": 15, "polygon": 128, "solana": 31}
COINGECKO_IDS: Dict[str, str] = {
    "ETH": "ethereum", "BNB": "binancecoin", "MATIC": "matic-network",
    "SOL": "solana", "USDT": "tether", "USDC": "usd-coin",
}
PRICE_CACHE_TTL = 60
RPC_TIMEOUT     = 10
RECONNECT_DELAY = 5
MAX_RETRIES     = 5
POLL_INTERVAL   = 3
POA_NETWORKS    = {"bsc", "polygon"}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class WatchedAddress:
    network:        str
    address:        str
    label:          str = ""           # e.g. "user:42" for routing callbacks
    token_address:  Optional[str] = None   # None = native token
    token_symbol:   Optional[str] = None
    token_decimals: int = 18


@dataclass
class DepositEvent:
    network:       str
    address:       str               # recipient
    from_address:  str
    amount:        Decimal
    token_symbol:  str
    tx_hash:       str
    block_number:  int
    confirmations: int
    confirmed:     bool
    usd_value:     Optional[Decimal] = None
    label:         str = ""


@dataclass
class TxStatus:
    tx_hash:       str
    network:       str
    confirmations: int
    confirmed:     bool
    block_number:  Optional[int]
    status:        str               # "pending" | "confirmed" | "failed"


# ── Exceptions ────────────────────────────────────────────────────────────────

class BlockchainError(Exception):    pass
class RPCConnectionError(BlockchainError): pass


# ── RPC wrappers ──────────────────────────────────────────────────────────────

class EVMConnection:
    """AsyncWeb3 with health-check and reconnect."""

    def __init__(self, network: str, rpc_url: str) -> None:
        self.network    = network
        self.rpc_url    = rpc_url
        self.w3:        Optional[AsyncWeb3] = None
        self._connected = False

    async def connect(self) -> bool:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(
                    self.rpc_url, request_kwargs={"timeout": RPC_TIMEOUT}
                ))
                if self.network in POA_NETWORKS:
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                await asyncio.wait_for(w3.eth.block_number, timeout=RPC_TIMEOUT)
                self.w3 = w3
                self._connected = True
                logger.info("[%s] RPC connected: %s", self.network, self.rpc_url)
                return True
            except Exception as exc:
                logger.warning("[%s] Connect attempt %d/%d: %s", self.network, attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RECONNECT_DELAY * attempt)
        self._connected = False
        return False

    async def ensure(self) -> AsyncWeb3:
        if self._connected and self.w3:
            try:
                await asyncio.wait_for(self.w3.eth.block_number, timeout=RPC_TIMEOUT)
                return self.w3
            except Exception:
                self._connected = False
        if not await self.connect():
            raise RPCConnectionError(f"[{self.network}] Cannot connect to RPC")
        return self.w3   # type: ignore

    async def block_number(self) -> int:
        return await asyncio.wait_for((await self.ensure()).eth.block_number, RPC_TIMEOUT)

    async def receipt(self, tx_hash: str):
        try:
            return await asyncio.wait_for(
                (await self.ensure()).eth.get_transaction_receipt(tx_hash), RPC_TIMEOUT
            )
        except (TransactionNotFound, Exception):
            return None

    async def get_logs(self, params: dict) -> list:
        return await asyncio.wait_for((await self.ensure()).eth.get_logs(params), RPC_TIMEOUT)  # type: ignore


class SolanaConnection:
    """SolanaClient with reconnect."""

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url    = rpc_url
        self.client:    Optional[SolanaClient] = None
        self._connected = False

    async def connect(self) -> bool:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if self.client:
                    await self.client.close()
                self.client = SolanaClient(self.rpc_url)
                await asyncio.wait_for(self.client.get_health(), RPC_TIMEOUT)
                self._connected = True
                logger.info("[solana] RPC connected: %s", self.rpc_url)
                return True
            except Exception as exc:
                logger.warning("[solana] Connect attempt %d/%d: %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RECONNECT_DELAY * attempt)
        return False

    async def ensure(self) -> SolanaClient:
        if self._connected and self.client:
            try:
                await asyncio.wait_for(self.client.get_health(), RPC_TIMEOUT)
                return self.client
            except Exception:
                self._connected = False
        if not await self.connect():
            raise RPCConnectionError("[solana] Cannot connect to RPC")
        return self.client   # type: ignore

    async def close(self) -> None:
        if self.client:
            await self.client.close()


# ── Price feed ────────────────────────────────────────────────────────────────

class PriceFeed:
    """CoinGecko free-tier price cache with 60-second TTL."""

    _URL = "https://api.coingecko.com/api/v3/simple/price"

    def __init__(self) -> None:
        self._cache:      Dict[str, Decimal] = {}
        self._cache_time: float = 0.0
        self._session:    Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def get_price(self, symbol: str) -> Optional[Decimal]:
        symbol = symbol.upper()
        if time.monotonic() - self._cache_time < PRICE_CACHE_TTL and symbol in self._cache:
            return self._cache[symbol]
        await self._refresh()
        return self._cache.get(symbol)

    async def usd_value(self, symbol: str, amount: Decimal) -> Optional[Decimal]:
        price = await self.get_price(symbol)
        return (price * amount).quantize(Decimal("0.01")) if price is not None else None

    async def _refresh(self) -> None:
        ids = ",".join(COINGECKO_IDS.values())
        try:
            sess = await self._session_get()
            async with sess.get(self._URL, params={"ids": ids, "vs_currencies": "usd"}) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
            for sym, cg_id in COINGECKO_IDS.items():
                if cg_id in data and "usd" in data[cg_id]:
                    self._cache[sym] = Decimal(str(data[cg_id]["usd"]))
            self._cache_time = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[price] Refresh failed (using cache): %s", exc)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ── BlockchainMonitor ─────────────────────────────────────────────────────────

DepositCallback = Callable[[DepositEvent], object]


class BlockchainMonitor:
    """
    Monitors deposit addresses for inbound transactions on all chains.
    Fires `callback` twice per deposit: once on detection (confirmed=False)
    and again when the required confirmation threshold is met (confirmed=True).
    """

    def __init__(self, callback: Optional[DepositCallback] = None) -> None:
        self._callback = callback
        self._evm:  Dict[str, EVMConnection]  = {}
        self._sol:  Optional[SolanaConnection] = None
        self._prices = PriceFeed()

        self._watched:    Dict[str, List[WatchedAddress]] = {
            n: [] for n in ("ethereum", "bsc", "polygon", "solana")
        }
        self._pending:    Dict[str, tuple[DepositEvent, int]] = {}
        self._seen_txs:   Set[str] = set()
        self._sol_cursors: Dict[str, Optional[str]] = {}
        self._tasks:  List[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        for net, url in cfg.rpc_urls.items():
            if net == "solana":
                self._sol = SolanaConnection(url)
                await self._sol.connect()
            else:
                conn = EVMConnection(net, url)
                await conn.connect()
                self._evm[net] = conn

        self._running = True
        for net in ("ethereum", "bsc", "polygon"):
            self._tasks.append(asyncio.create_task(
                self._evm_loop(net), name=f"monitor-{net}"
            ))
        self._tasks.append(asyncio.create_task(self._sol_loop(), name="monitor-solana"))
        self._tasks.append(asyncio.create_task(self._confirm_loop(), name="confirmations"))
        logger.info("BlockchainMonitor started (%d networks)", len(self._evm) + 1)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for conn in self._evm.values():
            try:
                if conn.w3:
                    await conn.w3.provider.disconnect()
            except Exception:
                pass
        if self._sol:
            await self._sol.close()
        await self._prices.close()

    async def __aenter__(self) -> "BlockchainMonitor":
        await self.start(); return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Address registration ──────────────────────────────────────────────────

    def watch(
        self, network: str, address: str, label: str = "",
        token_address: Optional[str] = None,
        token_symbol:  Optional[str] = None,
        token_decimals: int = 18,
    ) -> None:
        wa = WatchedAddress(
            network=network, address=address, label=label,
            token_address=token_address, token_symbol=token_symbol, token_decimals=token_decimals,
        )
        if not any(w.address == address and w.token_address == token_address
                   for w in self._watched.get(network, [])):
            self._watched.setdefault(network, []).append(wa)
            if network == "solana":
                self._sol_cursors.setdefault(address, None)
            logger.info("[%s] Watching %s (%s)", network, address, label or "unlabelled")

    def unwatch(self, network: str, address: str) -> None:
        self._watched[network] = [w for w in self._watched.get(network, []) if w.address != address]

    # ── Monitoring loops ──────────────────────────────────────────────────────

    async def _evm_loop(self, network: str) -> None:
        conn       = self._evm[network]
        last_block: Optional[int] = None
        while self._running:
            try:
                if not self._watched[network]:
                    await asyncio.sleep(POLL_INTERVAL); continue
                current = await conn.block_number()
                if last_block is None:
                    last_block = current - 1
                if current > last_block:
                    await self._scan_evm(network, conn, last_block + 1, current)
                    last_block = current
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[%s] Monitor error: %s", network, exc)
                await asyncio.sleep(RECONNECT_DELAY)
            await asyncio.sleep(POLL_INTERVAL)

    async def _scan_evm(
        self, network: str, conn: EVMConnection, from_block: int, to_block: int
    ) -> None:
        watched  = self._watched[network]
        erc20_w  = [w for w in watched if w.token_address]
        native_w = [w for w in watched if not w.token_address]

        # ERC-20 via eth_getLogs
        if erc20_w:
            to_topics = ["0x" + w.address[2:].lower().zfill(64) for w in erc20_w]
            contracts  = list({w.token_address for w in erc20_w if w.token_address})
            try:
                logs = await conn.get_logs({
                    "fromBlock": from_block, "toBlock": to_block,
                    "address":   contracts,
                    "topics":    [ERC20_TRANSFER_TOPIC, None, to_topics],
                })
                for log in logs:
                    await self._process_erc20_log(network, log, erc20_w)
            except Exception as exc:
                logger.warning("[%s] ERC-20 scan error: %s", network, exc)

        # Native via block transactions
        if native_w:
            w3  = await conn.ensure()
            addr_set = {w.address.lower() for w in native_w}
            for bn in range(from_block, to_block + 1):
                try:
                    block = await asyncio.wait_for(
                        w3.eth.get_block(bn, full_transactions=True), RPC_TIMEOUT
                    )
                    for tx in block.get("transactions", []):
                        if tx.get("to") and tx["to"].lower() in addr_set and tx["value"] > 0:
                            await self._process_evm_native(network, tx, bn, native_w)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("[%s] Block %d error: %s", network, bn, exc)

    async def _process_erc20_log(
        self, network: str, log: dict, watched: List[WatchedAddress]
    ) -> None:
        tx_hash  = log["transactionHash"].hex()
        if tx_hash in self._seen_txs:
            return
        topics = log.get("topics", [])
        if len(topics) < 3:
            return
        to_addr = "0x" + topics[2].hex()[-40:]
        matched = next(
            (w for w in watched
             if w.address.lower() == to_addr.lower()
             and w.token_address and w.token_address.lower() == log["address"].lower()),
            None,
        )
        if not matched:
            return
        raw    = int(log["data"].hex(), 16)
        amount = Decimal(raw) / Decimal(10 ** matched.token_decimals)
        usd    = await self._prices.usd_value(matched.token_symbol or "TOKEN", amount)
        event  = DepositEvent(
            network=network,
            address=AsyncWeb3.to_checksum_address(to_addr),
            from_address="0x" + topics[1].hex()[-40:],
            amount=amount, token_symbol=matched.token_symbol or "TOKEN",
            tx_hash=tx_hash, block_number=log["blockNumber"],
            confirmations=0, confirmed=False,
            usd_value=usd, label=matched.label,
        )
        self._seen_txs.add(tx_hash)
        self._pending[tx_hash] = (event, REQUIRED_CONFIRMATIONS.get(network, 12))
        await self._emit(event)

    async def _process_evm_native(
        self, network: str, tx: dict, block: int, watched: List[WatchedAddress]
    ) -> None:
        tx_hash = tx["hash"].hex() if hasattr(tx["hash"], "hex") else tx["hash"]
        if tx_hash in self._seen_txs:
            return
        matched = next(
            (w for w in watched if not w.token_address and w.address.lower() == tx["to"].lower()),
            None,
        )
        if not matched:
            return
        sym    = {"ethereum": "ETH", "bsc": "BNB", "polygon": "MATIC"}.get(network, "")
        amount = Decimal(tx["value"]) / Decimal(10**18)
        usd    = await self._prices.usd_value(sym, amount)
        event  = DepositEvent(
            network=network,
            address=AsyncWeb3.to_checksum_address(tx["to"]),
            from_address=tx.get("from", ""),
            amount=amount, token_symbol=sym,
            tx_hash=tx_hash, block_number=block,
            confirmations=0, confirmed=False,
            usd_value=usd, label=matched.label,
        )
        self._seen_txs.add(tx_hash)
        self._pending[tx_hash] = (event, REQUIRED_CONFIRMATIONS.get(network, 12))
        await self._emit(event)

    async def _sol_loop(self) -> None:
        while self._running:
            try:
                watched = self._watched.get("solana", [])
                if not watched:
                    await asyncio.sleep(POLL_INTERVAL); continue
                client = await self._sol.ensure()
                for w in watched:
                    try:
                        await self._scan_sol_address(client, w)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("[solana] Scan error %s: %s", w.address, exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[solana] Loop error: %s", exc)
                await asyncio.sleep(RECONNECT_DELAY)
            await asyncio.sleep(POLL_INTERVAL)

    async def _scan_sol_address(self, client, watched: WatchedAddress) -> None:
        pub  = Pubkey.from_string(watched.address)
        last = self._sol_cursors.get(watched.address)
        until = SolSignature.from_string(last) if last else None
        resp  = await asyncio.wait_for(
            client.get_signatures_for_address(pub, until=until, limit=20, commitment=SolanaConfirmed),
            RPC_TIMEOUT,
        )
        sigs = resp.value
        if not sigs:
            return
        self._sol_cursors[watched.address] = str(sigs[0].signature)
        for sig_info in reversed(sigs):
            sig_str = str(sig_info.signature)
            if sig_str in self._seen_txs or sig_info.err:
                continue
            try:
                tx_resp = await asyncio.wait_for(
                    client.get_transaction(
                        SolSignature.from_string(sig_str), max_supported_transaction_version=0
                    ), RPC_TIMEOUT,
                )
            except Exception:
                continue
            if not tx_resp.value:
                continue
            tx   = tx_resp.value
            meta = tx.transaction.meta
            if not meta:
                continue
            keys = [str(k) for k in tx.transaction.transaction.message.account_keys]
            try:
                idx = keys.index(watched.address)
            except ValueError:
                continue
            delta = meta.post_balances[idx] - meta.pre_balances[idx]
            if delta <= 0:
                continue
            amount = Decimal(delta) / Decimal(10**9)
            usd    = await self._prices.usd_value("SOL", amount)
            event  = DepositEvent(
                network="solana", address=watched.address,
                from_address=keys[0] if keys else "unknown",
                amount=amount, token_symbol="SOL",
                tx_hash=sig_str, block_number=tx.slot or 0,
                confirmations=0, confirmed=False,
                usd_value=usd, label=watched.label,
            )
            self._seen_txs.add(sig_str)
            self._pending[sig_str] = (event, REQUIRED_CONFIRMATIONS["solana"])
            await self._emit(event)

    # ── Confirmation loop ─────────────────────────────────────────────────────

    async def _confirm_loop(self) -> None:
        while self._running:
            await asyncio.sleep(POLL_INTERVAL * 2)
            to_remove: List[str] = []
            for tx_hash, (event, required) in list(self._pending.items()):
                try:
                    confs = await self._get_confirmations(event.network, tx_hash, event.block_number)
                    if confs is None:
                        continue
                    event.confirmations = confs
                    if confs >= required and not event.confirmed:
                        event.confirmed = True
                        await self._emit(event)
                        to_remove.append(tx_hash)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("[confirm] %s: %s", tx_hash[:12], exc)
            for tx_hash in to_remove:
                self._pending.pop(tx_hash, None)

    async def _get_confirmations(
        self, network: str, tx_hash: str, tx_block: int
    ) -> Optional[int]:
        try:
            if network == "solana":
                client = await self._sol.ensure()
                sig    = SolSignature.from_string(tx_hash)
                resp   = await asyncio.wait_for(
                    client.get_signature_statuses([sig], search_transaction_history=True), RPC_TIMEOUT
                )
                st = resp.value[0] if resp.value else None
                if not st:
                    return 0
                if "finalized" in str(getattr(st, "confirmation_status", "")):
                    return REQUIRED_CONFIRMATIONS["solana"]
                return int(st.confirmations or 0)
            else:
                conn    = self._evm[network]
                current = await conn.block_number()
                receipt = await conn.receipt(tx_hash)
                if not receipt:
                    return 0
                return max(0, current - receipt["blockNumber"])
        except Exception:
            return None

    # ── Public query ──────────────────────────────────────────────────────────

    async def get_tx_status(self, network: str, tx_hash: str) -> TxStatus:
        req = REQUIRED_CONFIRMATIONS.get(network, 12)
        if network == "solana":
            client = await self._sol.ensure()
            sig    = SolSignature.from_string(tx_hash)
            resp   = await asyncio.wait_for(
                client.get_signature_statuses([sig], search_transaction_history=True), RPC_TIMEOUT
            )
            st = resp.value[0] if resp.value else None
            if not st:
                return TxStatus(tx_hash, network, 0, False, None, "pending")
            confs = int(st.confirmations or 0)
            ok    = st.err is None
            cs    = str(getattr(st, "confirmation_status", ""))
            conf  = "finalized" in cs or confs >= req
            return TxStatus(tx_hash, network, confs, conf, None, "confirmed" if conf and ok else ("failed" if not ok else "pending"))
        else:
            conn    = self._evm[network]
            current = await conn.block_number()
            receipt = await conn.receipt(tx_hash)
            if not receipt:
                return TxStatus(tx_hash, network, 0, False, None, "pending")
            confs = max(0, current - receipt["blockNumber"])
            ok    = receipt.get("status") == 1
            return TxStatus(
                tx_hash, network, confs, confs >= req and ok,
                receipt["blockNumber"], "confirmed" if ok else "failed"
            )

    async def get_price(self, symbol: str) -> Optional[Decimal]:
        return await self._prices.get_price(symbol)

    async def _emit(self, event: DepositEvent) -> None:
        if self._callback:
            try:
                result = self._callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.exception("Deposit callback raised: %s", exc)
        else:
            tag = "✅ CONFIRMED" if event.confirmed else "🔔 DETECTED"
            logger.info(
                "%s [%s] %s %s → %s | tx=%s | usd=%s",
                tag, event.network, event.amount, event.token_symbol,
                event.address, event.tx_hash[:12],
                f"${event.usd_value:.2f}" if event.usd_value else "n/a",
            )
