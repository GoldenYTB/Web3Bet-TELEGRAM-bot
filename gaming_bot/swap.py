"""
swap.py — ChangeNow integration for swapping any coin to any coin.

All amounts in USD internally. On withdrawal:
  1. Get estimated output amount for user's USD balance → their chosen coin
  2. Show user exact amount they'll receive
  3. Create ChangeNow exchange
  4. Bot sends the from-coin to ChangeNow deposit address
  5. ChangeNow sends chosen coin to user's address

Hidden fee: ChangeNow's spread is built in. We add nothing on top.
House earns from the exchange spread naturally.

ChangeNow v1 API docs: https://changenow.io/api/docs
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CHANGENOW_BASE = "https://api.changenow.io/v1"

# Map our internal coin symbols to ChangeNow ticker strings
CN_TICKER: dict[str, str] = {
    "BTC":   "btc",
    "ETH":   "eth",
    "USDT":  "usdtbsc",    # USDT on BSC
    "USDC":  "usdc",
    "LTC":   "ltc",
    "SOL":   "sol",
    "BNB":   "bnbbsc",
    "TRX":   "trx",
    "XMR":   "xmr",
    "DAI":   "dai",
    "DOGE":  "doge",
    "SHIB":  "shib",
    "BCH":   "bch",
    "MATIC": "matic",
    "TON":   "ton",
}

# CoinGecko IDs for price fetching (coin → USD)
COINGECKO_IDS: dict[str, str] = {
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "USDT":  "tether",
    "USDC":  "usd-coin",
    "LTC":   "litecoin",
    "SOL":   "solana",
    "BNB":   "binancecoin",
    "TRX":   "tron",
    "XMR":   "monero",
    "DAI":   "dai",
    "DOGE":  "dogecoin",
    "SHIB":  "shiba-inu",
    "BCH":   "bitcoin-cash",
    "MATIC": "matic-network",
    "TON":   "the-open-network",
}


@dataclass
class SwapEstimate:
    from_coin:      str
    to_coin:        str
    from_amount:    Decimal   # amount of from_coin to send
    to_amount:      Decimal   # amount of to_coin user receives
    rate:           Decimal   # 1 from_coin = X to_coin
    min_amount:     Decimal   # minimum from_coin for this pair
    usd_value:      Decimal   # approximate USD value


@dataclass
class SwapTransaction:
    exchange_id:    str
    deposit_address: str      # send from_coin here
    deposit_amount:  Decimal
    from_coin:       str
    to_coin:         str
    to_amount:       Decimal
    recipient:       str      # user's receiving address
    status:          str = "waiting"


class ChangeNowClient:
    """Async ChangeNow API client."""

    def __init__(self, api_key: str) -> None:
        self._key     = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "x-changenow-api-key": self._key,
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_min_amount(self, from_coin: str, to_coin: str) -> Decimal:
        """Get minimum swap amount for a pair."""
        from_t = CN_TICKER.get(from_coin, from_coin.lower())
        to_t   = CN_TICKER.get(to_coin,   to_coin.lower())
        try:
            session = await self._get_session()
            async with session.get(
                f"{CHANGENOW_BASE}/min-amount/{from_t}_{to_t}"
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return Decimal(str(data.get("minAmount", "0")))
        except Exception as exc:
            logger.warning("ChangeNow min-amount error: %s", exc)
        return Decimal("0")

    async def estimate(
        self, from_coin: str, to_coin: str, from_amount: Decimal
    ) -> Optional[SwapEstimate]:
        """
        Get estimated output for swapping from_amount of from_coin to to_coin.
        Returns None if the pair is unavailable or amount is below minimum.
        """
        from_t = CN_TICKER.get(from_coin, from_coin.lower())
        to_t   = CN_TICKER.get(to_coin,   to_coin.lower())
        try:
            session = await self._get_session()
            async with session.get(
                f"{CHANGENOW_BASE}/exchange-amount/{from_amount}/{from_t}_{to_t}"
            ) as r:
                if r.status != 200:
                    logger.warning("ChangeNow estimate %s→%s status=%d", from_coin, to_coin, r.status)
                    return None
                data = await r.json()
                to_amount = Decimal(str(data.get("estimatedAmount", "0")))
                rate      = to_amount / from_amount if from_amount > 0 else Decimal("0")
                return SwapEstimate(
                    from_coin=from_coin,
                    to_coin=to_coin,
                    from_amount=from_amount,
                    to_amount=to_amount,
                    rate=rate,
                    min_amount=Decimal(str(data.get("minAmount", "0"))),
                    usd_value=Decimal("0"),  # filled by caller if needed
                )
        except Exception as exc:
            logger.error("ChangeNow estimate error: %s", exc)
            return None

    async def create_exchange(
        self,
        from_coin:   str,
        to_coin:     str,
        from_amount: Decimal,
        recipient:   str,
        refund_addr: Optional[str] = None,
    ) -> Optional[SwapTransaction]:
        """
        Create a swap transaction.
        Returns deposit address and expected output amount.
        """
        from_t = CN_TICKER.get(from_coin, from_coin.lower())
        to_t   = CN_TICKER.get(to_coin,   to_coin.lower())
        payload = {
            "from":          from_t,
            "to":            to_t,
            "amount":        float(from_amount),
            "address":       recipient,
            "flow":          "standard",
        }
        if refund_addr:
            payload["refundAddress"] = refund_addr

        try:
            session = await self._get_session()
            async with session.post(
                f"{CHANGENOW_BASE}/transactions/{self._key}",
                json=payload,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.error("ChangeNow create error %d: %s", r.status, body[:200])
                    return None
                data = await r.json()
                return SwapTransaction(
                    exchange_id      = data["id"],
                    deposit_address  = data["payinAddress"],
                    deposit_amount   = Decimal(str(data["amount"])),
                    from_coin        = from_coin,
                    to_coin          = to_coin,
                    to_amount        = Decimal(str(data.get("estimatedAmount", "0"))),
                    recipient        = recipient,
                    status           = data.get("status", "waiting"),
                )
        except Exception as exc:
            logger.error("ChangeNow create_exchange error: %s", exc)
            return None

    async def get_status(self, exchange_id: str) -> str:
        """Poll swap status: waiting | confirming | exchanging | sending | finished | failed"""
        try:
            session = await self._get_session()
            async with session.get(
                f"{CHANGENOW_BASE}/transactions/{exchange_id}/{self._key}"
            ) as r:
                if r.status == 200:
                    return (await r.json()).get("status", "unknown")
        except Exception as exc:
            logger.warning("ChangeNow status error: %s", exc)
        return "unknown"


class PriceFeed:
    """
    CoinGecko price feed — converts coin amounts to/from USD.
    No API key needed. Cached for 60 seconds.
    """

    _CACHE_TTL = 60

    def __init__(self) -> None:
        self._cache:      dict[str, Decimal] = {}
        self._cache_time: float = 0.0
        self._session:    Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def get_usd_prices(self) -> dict[str, Decimal]:
        """Return USD prices for all supported coins. Uses cache."""
        import time
        if time.monotonic() - self._cache_time < self._CACHE_TTL and self._cache:
            return dict(self._cache)
        await self._refresh()
        return dict(self._cache)

    async def get_price(self, coin: str) -> Optional[Decimal]:
        prices = await self.get_usd_prices()
        return prices.get(coin.upper())

    async def coin_to_usd(self, coin: str, amount: Decimal) -> Optional[Decimal]:
        price = await self.get_price(coin)
        if price is None: return None
        return (price * amount).quantize(Decimal("0.01"))

    async def usd_to_coin(self, usd: Decimal, coin: str) -> Optional[Decimal]:
        price = await self.get_price(coin)
        if not price or price == 0: return None
        return (usd / price).quantize(Decimal("0.00000001"))

    async def _refresh(self) -> None:
        import time
        ids = ",".join(COINGECKO_IDS.values())
        try:
            session = await self._session_get()
            async with session.get(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usd",
            ) as r:
                if r.status != 200:
                    return
                data = await r.json()
            for sym, cg_id in COINGECKO_IDS.items():
                if cg_id in data and "usd" in data[cg_id]:
                    self._cache[sym] = Decimal(str(data[cg_id]["usd"]))
            self._cache_time = time.monotonic()
        except Exception as exc:
            logger.warning("PriceFeed refresh error: %s", exc)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# Module-level singletons — initialised in main.py
changenow: Optional[ChangeNowClient] = None
price_feed: Optional[PriceFeed] = None


def init(api_key: str) -> None:
    global changenow, price_feed
    changenow  = ChangeNowClient(api_key)
    price_feed = PriceFeed()
    logger.info("Swap module initialised (ChangeNow + CoinGecko)")
