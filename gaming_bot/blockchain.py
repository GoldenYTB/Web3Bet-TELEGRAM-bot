"""
blockchain.py — Deposit monitoring for all 15 coins across all networks.

Monitors:
  EVM (ETH/BSC/Polygon) — Alchemy RPC, polls latest blocks
  Solana                 — public RPC, polls signatures
  BTC/LTC/DOGE/BCH      — BlockCypher webhook + polling
  TRX/USDT TRC-20       — TronGrid API polling
  TON                    — TONCenter API polling
  XMR                    — manual credit only (private chain)

On confirmed deposit:
  1. Fetch USD price via CoinGecko
  2. Credit user.usd_balance
  3. Track coin holding in user.coin_holdings
  4. Send Telegram notification to user
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Set

import aiohttp

from .config import cfg

logger = logging.getLogger(__name__)

# ── Confirmation requirements ──────────────────────────────────────────────────
CONFIRMATIONS = {
    "ethereum":    12,
    "bsc":         15,
    "polygon":     128,
    "solana":      31,
    "bitcoin":     3,
    "litecoin":    6,
    "dogecoin":    6,
    "bitcoincash": 6,
    "tron":        20,
    "ton":         5,
}

POLL_INTERVAL  = 30   # seconds between polls
MAX_RETRIES    = 3


# ── Deposit event ──────────────────────────────────────────────────────────────

@dataclass
class DepositEvent:
    user_id:       int
    network:       str
    coin_symbol:   str
    amount:        Decimal
    tx_hash:       str
    confirmations: int      = 0
    confirmed:     bool     = False
    usd_value:     Optional[Decimal] = None
    address:       str      = ""


@dataclass
class WatchedAddress:
    user_id:     int
    address:     str
    network:     str
    coin_symbol: str
    added_at:    float = field(default_factory=time.time)


# ── BlockCypher client (BTC/LTC/DOGE/BCH) ────────────────────────────────────

BC_COIN_MAP = {
    "bitcoin":     "btc/main",
    "litecoin":    "ltc/main",
    "dogecoin":    "doge/main",
    "bitcoincash": "bcy/main",   # BlockCypher uses bcy for BCH
}
BC_SYMBOL_MAP = {
    "bitcoin": "BTC", "litecoin": "LTC",
    "dogecoin": "DOGE", "bitcoincash": "BCH",
}

class BlockCypherMonitor:
    BASE = "https://api.blockcypher.com/v1"

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._key     = api_key
        self._session = session
        # address → last seen tx hashes
        self._seen: Dict[str, Set[str]] = {}

    async def check_address(self, wa: WatchedAddress) -> List[DepositEvent]:
        coin_path = BC_COIN_MAP.get(wa.network)
        if not coin_path:
            return []
        sym = BC_SYMBOL_MAP.get(wa.network, wa.coin_symbol)
        url = f"{self.BASE}/{coin_path}/addrs/{wa.address}?token={self._key}&limit=10"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        except Exception as exc:
            logger.warning("BlockCypher check %s failed: %s", wa.address[:12], exc)
            return []

        events = []
        txrefs = data.get("txrefs", []) + data.get("unconfirmed_txrefs", [])
        seen   = self._seen.setdefault(wa.address, set())

        for tx in txrefs:
            tx_hash = tx.get("tx_hash", "")
            if tx_hash in seen:
                continue
            # Only incoming (received) txs
            if not tx.get("received"):
                continue
            value_sat   = tx.get("value", 0)
            confs       = tx.get("confirmations", 0)
            required    = CONFIRMATIONS.get(wa.network, 6)
            confirmed   = confs >= required
            # Convert satoshis to coin
            amount = Decimal(value_sat) / Decimal(10 ** 8)
            if amount <= 0:
                continue
            ev = DepositEvent(
                user_id=wa.user_id, network=wa.network,
                coin_symbol=sym, amount=amount,
                tx_hash=tx_hash, confirmations=confs,
                confirmed=confirmed, address=wa.address,
            )
            if confirmed:
                seen.add(tx_hash)
            events.append(ev)
        return events


# ── TronGrid client (TRX + USDT TRC-20) ──────────────────────────────────────

TRON_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

class TronMonitor:
    BASE = "https://api.trongrid.io"

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._key     = api_key
        self._session = session
        self._seen: Dict[str, Set[str]] = {}
        self._headers = {"TRON-PRO-API-KEY": api_key}

    async def check_address(self, wa: WatchedAddress) -> List[DepositEvent]:
        events = []
        # Check TRX native transfers
        events += await self._check_trx(wa)
        # Check USDT TRC-20 transfers
        if wa.coin_symbol in ("USDT", "TRX"):
            events += await self._check_trc20(wa)
        return events

    async def _check_trx(self, wa: WatchedAddress) -> List[DepositEvent]:
        url = f"{self.BASE}/v1/accounts/{wa.address}/transactions?limit=20&only_confirmed=true"
        try:
            async with self._session.get(url, headers=self._headers,
                                          timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return []
                data = await r.json()
        except Exception as exc:
            logger.warning("TronGrid TRX check failed: %s", exc)
            return []

        seen   = self._seen.setdefault(wa.address + ":trx", set())
        events = []
        for tx in data.get("data", []):
            tx_id = tx.get("txID", "")
            if tx_id in seen: continue
            raw = tx.get("raw_data", {})
            for contract in raw.get("contract", []):
                if contract.get("type") != "TransferContract": continue
                val = contract.get("parameter", {}).get("value", {})
                if val.get("to_address", "").upper() != wa.address.upper(): continue
                amount = Decimal(val.get("amount", 0)) / Decimal(10 ** 6)
                if amount <= 0: continue
                seen.add(tx_id)
                events.append(DepositEvent(
                    user_id=wa.user_id, network="tron",
                    coin_symbol="TRX", amount=amount,
                    tx_hash=tx_id, confirmations=20,
                    confirmed=True, address=wa.address,
                ))
        return events

    async def _check_trc20(self, wa: WatchedAddress) -> List[DepositEvent]:
        url = (f"{self.BASE}/v1/accounts/{wa.address}/transactions/trc20"
               f"?limit=20&contract_address={TRON_USDT_CONTRACT}")
        try:
            async with self._session.get(url, headers=self._headers,
                                          timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return []
                data = await r.json()
        except Exception as exc:
            logger.warning("TronGrid TRC20 check failed: %s", exc)
            return []

        seen   = self._seen.setdefault(wa.address + ":usdt_trc20", set())
        events = []
        for tx in data.get("data", []):
            tx_id = tx.get("transaction_id", "")
            if tx_id in seen: continue
            if tx.get("to", "").upper() != wa.address.upper(): continue
            if not tx.get("confirmed", False): continue
            amount = Decimal(tx.get("value", "0")) / Decimal(10 ** 6)
            if amount <= 0: continue
            seen.add(tx_id)
            events.append(DepositEvent(
                user_id=wa.user_id, network="tron",
                coin_symbol="USDT", amount=amount,
                tx_hash=tx_id, confirmations=20,
                confirmed=True, address=wa.address,
            ))
        return events


# ── TONCenter client (TON) ────────────────────────────────────────────────────

class TONMonitor:
    BASE = "https://toncenter.com/api/v2"

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._key     = api_key
        self._session = session
        self._seen: Dict[str, Set[str]] = {}
        self._headers = {"X-API-Key": api_key}

    async def check_address(self, wa: WatchedAddress) -> List[DepositEvent]:
        url = f"{self.BASE}/getTransactions?address={wa.address}&limit=20"
        try:
            async with self._session.get(url, headers=self._headers,
                                          timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return []
                data = await r.json()
        except Exception as exc:
            logger.warning("TONCenter check failed: %s", exc)
            return []

        if not data.get("ok"): return []
        seen   = self._seen.setdefault(wa.address, set())
        events = []
        for tx in data.get("result", []):
            tx_id = str(tx.get("transaction_id", {}).get("hash", ""))
            if tx_id in seen: continue
            msg   = tx.get("in_msg", {})
            if not msg: continue
            value = int(msg.get("value", 0))
            if value <= 0: continue
            # TON uses nanoTON
            amount = Decimal(value) / Decimal(10 ** 9)
            seen.add(tx_id)
            events.append(DepositEvent(
                user_id=wa.user_id, network="ton",
                coin_symbol="TON", amount=amount,
                tx_hash=tx_id, confirmations=5,
                confirmed=True, address=wa.address,
            ))
        return events


# ── EVM monitor (ETH/BSC/Polygon via Alchemy) ─────────────────────────────────

EVM_NATIVE = {"ethereum": "ETH", "bsc": "BNB", "polygon": "MATIC"}

# ERC-20 contract addresses we watch
ERC20_WATCH = {
    "ethereum": {
        "USDT":  "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "USDC":  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "DAI":   "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        "SHIB":  "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE",
    },
    "bsc": {
        "USDT":  "0x55d398326f99059fF775485246999027B3197955",
        "USDC":  "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    },
    "polygon": {
        "USDT":  "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "USDC":  "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "DAI":   "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
        "MATIC": None,  # native
    },
}

# ERC-20 Transfer event topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

class EVMMonitor:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session  = session
        self._seen:    Dict[str, Set[str]] = {}
        self._last_block: Dict[str, int]  = {}

    async def check_address(self, wa: WatchedAddress, rpc_url: str) -> List[DepositEvent]:
        net    = wa.network
        events = []
        events += await self._check_native(wa, rpc_url)
        for sym, contract in ERC20_WATCH.get(net, {}).items():
            if contract:
                events += await self._check_erc20(wa, rpc_url, sym, contract)
        return events

    async def _rpc(self, url: str, method: str, params: list) -> Optional[dict]:
        payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
        try:
            async with self._session.post(url, json=payload,
                                           timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return None
                return await r.json()
        except Exception as exc:
            logger.warning("EVM RPC %s failed: %s", method, exc)
            return None

    async def _check_native(self, wa: WatchedAddress, rpc_url: str) -> List[DepositEvent]:
        # Get latest block
        res = await self._rpc(rpc_url, "eth_blockNumber", [])
        if not res or "result" not in res: return []
        latest = int(res["result"], 16)
        last   = self._last_block.get(wa.address + wa.network, latest - 10)

        # Use eth_getBalance to detect balance changes (simpler than scanning blocks)
        # For proper monitoring use Alchemy's getAssetTransfers
        rpc_url_clean = rpc_url.split("/v2/")[0] + "/v2/" + rpc_url.split("/v2/")[-1]
        url = rpc_url_clean.replace("https://", "https://")

        # Use Alchemy's alchemy_getAssetTransfers if available
        params = [{
            "fromBlock":  hex(last + 1),
            "toBlock":    "latest",
            "toAddress":  wa.address,
            "category":   ["external"],
            "withMetadata": False,
            "excludeZeroValue": True,
            "maxCount":   "0x14",
        }]
        res2 = await self._rpc(rpc_url, "alchemy_getAssetTransfers", params)
        if not res2 or "result" not in res2:
            self._last_block[wa.address + wa.network] = latest
            return []

        seen   = self._seen.setdefault(wa.address + wa.network + ":native", set())
        events = []
        native_sym = EVM_NATIVE.get(wa.network, "ETH")
        required   = CONFIRMATIONS.get(wa.network, 12)

        for tx in res2["result"].get("transfers", []):
            tx_hash = tx.get("hash", "")
            if tx_hash in seen: continue
            value = tx.get("value", 0) or 0
            amount = Decimal(str(value))
            if amount <= 0: continue
            block_num = int(tx.get("blockNum", "0x0"), 16)
            confs     = latest - block_num
            confirmed = confs >= required
            ev = DepositEvent(
                user_id=wa.user_id, network=wa.network,
                coin_symbol=native_sym, amount=amount,
                tx_hash=tx_hash, confirmations=confs,
                confirmed=confirmed, address=wa.address,
            )
            if confirmed:
                seen.add(tx_hash)
            events.append(ev)

        self._last_block[wa.address + wa.network] = latest
        return events

    async def _check_erc20(self, wa: WatchedAddress, rpc_url: str,
                            sym: str, contract: str) -> List[DepositEvent]:
        res = await self._rpc(rpc_url, "eth_blockNumber", [])
        if not res or "result" not in res: return []
        latest = int(res["result"], 16)
        last   = self._last_block.get(wa.address + wa.network, latest - 10)
        addr_padded = "0x" + wa.address[2:].zfill(64).lower()
        params = [{
            "fromBlock": hex(last + 1),
            "toBlock":   "latest",
            "address":   contract,
            "topics":    [TRANSFER_TOPIC, None, addr_padded],
        }]
        res2 = await self._rpc(rpc_url, "eth_getLogs", params)
        if not res2 or "result" not in res2: return []

        seen     = self._seen.setdefault(wa.address + wa.network + ":" + sym, set())
        events   = []
        required = CONFIRMATIONS.get(wa.network, 12)

        # Token decimals
        decimals = {"USDT":6,"USDC":6,"DAI":18,"SHIB":18}.get(sym, 18)

        for log in res2["result"]:
            tx_hash = log.get("transactionHash", "")
            if tx_hash in seen: continue
            data     = log.get("data", "0x")
            value    = int(data, 16) if data and data != "0x" else 0
            amount   = Decimal(value) / Decimal(10 ** decimals)
            if amount <= 0: continue
            block_hex = log.get("blockNumber", "0x0")
            block_num = int(block_hex, 16) if block_hex else latest
            confs     = latest - block_num
            confirmed = confs >= required
            ev = DepositEvent(
                user_id=wa.user_id, network=wa.network,
                coin_symbol=sym, amount=amount,
                tx_hash=tx_hash, confirmations=confs,
                confirmed=confirmed, address=wa.address,
            )
            if confirmed:
                seen.add(tx_hash)
            events.append(ev)
        return events


# ── Solana monitor ────────────────────────────────────────────────────────────

class SolanaMonitor:
    def __init__(self, rpc_url: str, session: aiohttp.ClientSession) -> None:
        self._url     = rpc_url
        self._session = session
        self._seen:   Dict[str, Set[str]] = {}

    async def _rpc(self, method: str, params: list) -> Optional[dict]:
        try:
            async with self._session.post(self._url,
                json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200: return None
                return await r.json()
        except Exception as exc:
            logger.warning("Solana RPC %s failed: %s", method, exc)
            return None

    async def check_address(self, wa: WatchedAddress) -> List[DepositEvent]:
        res = await self._rpc("getSignaturesForAddress", [wa.address, {"limit": 20}])
        if not res or "result" not in res: return []
        seen   = self._seen.setdefault(wa.address, set())
        events = []
        for sig_info in res["result"]:
            sig = sig_info.get("signature", "")
            if sig in seen: continue
            if sig_info.get("err"): continue
            # Get transaction details
            tx_res = await self._rpc("getTransaction",
                [sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
            if not tx_res or not tx_res.get("result"): continue
            tx   = tx_res["result"]
            meta = tx.get("meta", {})
            if meta.get("err"): continue
            # Find SOL transfer to our address
            account_keys = tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
            pre_balances  = meta.get("preBalances", [])
            post_balances = meta.get("postBalances", [])
            for i, key_info in enumerate(account_keys):
                key = key_info if isinstance(key_info, str) else key_info.get("pubkey","")
                if key.lower() != wa.address.lower(): continue
                if i >= len(pre_balances) or i >= len(post_balances): continue
                diff = post_balances[i] - pre_balances[i]
                if diff <= 0: continue
                amount = Decimal(diff) / Decimal(10 ** 9)
                seen.add(sig)
                events.append(DepositEvent(
                    user_id=wa.user_id, network="solana",
                    coin_symbol="SOL", amount=amount,
                    tx_hash=sig, confirmations=31,
                    confirmed=True, address=wa.address,
                ))
                break
        return events


# ── Price feed ────────────────────────────────────────────────────────────────

COINGECKO_IDS = {
    "BTC":"bitcoin","ETH":"ethereum","USDT":"tether","USDC":"usd-coin",
    "LTC":"litecoin","SOL":"solana","BNB":"binancecoin","TRX":"tron",
    "XMR":"monero","DAI":"dai","DOGE":"dogecoin","SHIB":"shiba-inu",
    "BCH":"bitcoin-cash","MATIC":"matic-network","TON":"the-open-network",
}

class PriceFeed:
    _TTL = 60

    def __init__(self) -> None:
        self._cache:      Dict[str, Decimal] = {}
        self._cache_time: float = 0.0
        self._session:    Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def get_usd_prices(self) -> Dict[str, Decimal]:
        if time.monotonic() - self._cache_time < self._TTL and self._cache:
            return dict(self._cache)
        await self._refresh()
        return dict(self._cache)

    async def get_price(self, coin: str) -> Optional[Decimal]:
        p = await self.get_usd_prices()
        return p.get(coin.upper())

    async def coin_to_usd(self, coin: str, amount: Decimal) -> Optional[Decimal]:
        price = await self.get_price(coin)
        if not price: return None
        return (price * amount).quantize(Decimal("0.01"))

    async def usd_to_coin(self, usd: Decimal, coin: str) -> Optional[Decimal]:
        price = await self.get_price(coin)
        if not price or price == 0: return None
        return (usd / price).quantize(Decimal("0.00000001"))

    async def _refresh(self) -> None:
        ids = ",".join(COINGECKO_IDS.values())
        try:
            session = await self._get_session()
            async with session.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
            ) as r:
                if r.status != 200: return
                data = await r.json()
            for sym, cg_id in COINGECKO_IDS.items():
                if cg_id in data and "usd" in data[cg_id]:
                    self._cache[sym] = Decimal(str(data[cg_id]["usd"]))
            self._cache_time = time.monotonic()
            logger.debug("PriceFeed refreshed: %d coins", len(self._cache))
        except Exception as exc:
            logger.warning("PriceFeed refresh failed: %s", exc)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ── Master BlockchainMonitor ──────────────────────────────────────────────────

class BlockchainMonitor:
    """
    Polls all watched addresses every POLL_INTERVAL seconds.
    On confirmed deposit, calls on_deposit(DepositEvent).
    """

    def __init__(self, on_deposit: Callable, api_keys: dict) -> None:
        self._on_deposit  = on_deposit
        self._api_keys    = api_keys
        self._watched:    List[WatchedAddress] = []
        self._price_feed  = PriceFeed()
        self._session:    Optional[aiohttp.ClientSession] = None
        self._task:       Optional[asyncio.Task] = None
        self._running     = False

        # Sub-monitors (initialised in start())
        self._bc:   Optional[BlockCypherMonitor] = None
        self._tron: Optional[TronMonitor]        = None
        self._ton:  Optional[TONMonitor]         = None
        self._evm:  Optional[EVMMonitor]         = None
        self._sol:  Optional[SolanaMonitor]      = None

    def add_address(self, wa: WatchedAddress) -> None:
        # Avoid duplicates
        for existing in self._watched:
            if existing.address == wa.address and existing.network == wa.network:
                return
        self._watched.append(wa)
        logger.info("Watching %s on %s for user %d", wa.address[:12], wa.network, wa.user_id)

    def remove_address(self, address: str, network: str) -> None:
        self._watched = [w for w in self._watched
                         if not (w.address == address and w.network == network)]

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._bc   = BlockCypherMonitor(self._api_keys.get("blockcypher", ""), self._session)
        self._tron = TronMonitor(self._api_keys.get("trongrid", ""), self._session)
        self._ton  = TONMonitor(self._api_keys.get("toncenter", ""), self._session)
        self._evm  = EVMMonitor(self._session)
        self._sol  = SolanaMonitor(cfg.solana_rpc_url, self._session)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="blockchain-monitor")
        logger.info("BlockchainMonitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
        await self._price_feed.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("BlockchainMonitor stopped")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_all()
            except Exception as exc:
                logger.error("Poll loop error: %s", exc, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_all(self) -> None:
        if not self._watched:
            return
        tasks = [self._poll_one(wa) for wa in list(self._watched)]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_one(self, wa: WatchedAddress) -> None:
        net    = wa.network
        events: List[DepositEvent] = []

        try:
            if net in ("ethereum", "bsc", "polygon") and self._evm:
                rpc = cfg.rpc_urls.get(net, "")
                if rpc:
                    events = await self._evm.check_address(wa, rpc)

            elif net == "solana" and self._sol:
                events = await self._sol.check_address(wa)

            elif net in ("bitcoin","litecoin","dogecoin","bitcoincash") and self._bc:
                events = await self._bc.check_address(wa)

            elif net == "tron" and self._tron:
                events = await self._tron.check_address(wa)

            elif net == "ton" and self._ton:
                events = await self._ton.check_address(wa)

        except Exception as exc:
            logger.warning("Poll %s/%s failed: %s", net, wa.address[:12], exc)
            return

        for ev in events:
            if ev.confirmed:
                await self._handle_confirmed(ev)

    async def _handle_confirmed(self, ev: DepositEvent) -> None:
        # Get USD value
        usd = await self._price_feed.coin_to_usd(ev.coin_symbol, ev.amount)
        ev.usd_value = usd
        logger.info(
            "DEPOSIT CONFIRMED user=%d %s %s (~$%s) tx=%s",
            ev.user_id, ev.amount, ev.coin_symbol,
            f"{usd:.2f}" if usd else "?", ev.tx_hash[:16],
        )
        try:
            await self._on_deposit(ev)
        except Exception as exc:
            logger.error("on_deposit callback failed: %s", exc, exc_info=True)

    @property
    def price_feed(self) -> PriceFeed:
        return self._price_feed


# ── Convenience export ─────────────────────────────────────────────────────────
DepositEvent    = DepositEvent
WatchedAddress  = WatchedAddress
BlockchainMonitor = BlockchainMonitor
PriceFeed       = PriceFeed
