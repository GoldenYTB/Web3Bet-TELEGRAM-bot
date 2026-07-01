"""
wallet.py — Multi-chain wallet management covering every coin the bot supports.

Coins and how they're generated
---------------------------------
EVM family (one address covers all):
  ETH   — Ethereum + ERC-20 tokens (USDT, USDC, DAI, SHIB, etc.)
  BNB   — BNB Chain + BEP-20 tokens (USDT BEP-20, etc.)
  MATIC — Polygon + Polygon tokens

UTXO family (via hdwallet):
  BTC   — Bitcoin
  LTC   — Litecoin
  DOGE  — Dogecoin
  BCH   — Bitcoin Cash
  TRX   — Tron (also covers USDT TRC-20)

Monero (via hdwallet, uses spend key for restore):
  XMR   — Monero

TON (via tonsdk):
  TON   — Gram / Toncoin

Solana (via solders):
  SOL   — Solana + SPL tokens (USDT SPL, etc.)

Key storage
-----------
  EVM   → encrypted hex private key
  UTXO  → encrypted WIF string
  XMR   → encrypted spend_private_key hex
  TON   → encrypted 24-word mnemonic (space separated)
  SOL   → encrypted base58 keypair bytes
"""
from __future__ import annotations

import asyncio
import enum
import logging
import secrets
import struct
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Dict, List, Optional

import base58
from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account
from eth_account.signers.local import LocalAccount
from solana.rpc.async_api import AsyncClient as SolanaClient
from solana.rpc.models import TxOpts
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature as SolSignature
from solders.system_program import TransferParams, transfer as sol_transfer
from solders.transaction import VersionedTransaction
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import TxParams, Wei

from .config import cfg

logger = logging.getLogger(__name__)
Account.enable_unaudited_hdwallet_features()


# ── Constants ──────────────────────────────────────────────────────────────────

ERC20_ABI = [
    {"name": "transfer",  "type": "function",
     "inputs": [{"name": "recipient", "type": "address"},
                {"name": "amount",    "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
    {"name": "decimals",  "type": "function",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
    {"name": "balanceOf", "type": "function",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

CHAIN_IDS      = {"ethereum": 1, "bsc": 56, "polygon": 137}
NATIVE_SYMBOLS = {"ethereum": "ETH", "bsc": "BNB", "polygon": "MATIC", "solana": "SOL"}
POA_NETWORKS   = {"bsc", "polygon"}

REQUIRED_CONFIRMATIONS = {
    "ethereum": 12, "bsc": 15, "polygon": 128, "solana": 31,
    "bitcoin": 3, "litecoin": 6, "dogecoin": 6, "bitcoincash": 6,
    "tron": 20, "monero": 10, "ton": 5,
}

WITHDRAWAL_FIXED_FEE = cfg.withdrawal_fixed_fee
WITHDRAWAL_PCT_FEE   = cfg.withdrawal_pct_fee
MAX_BROADCAST_RETRIES = 3
BROADCAST_RETRY_DELAY = 2.0
MAX_POLL_ATTEMPTS     = 60
POLL_INTERVAL         = 5.0


# ── Coin → network/chain mapping ──────────────────────────────────────────────

# Which internal network string handles each coin symbol
COIN_NETWORK: Dict[str, str] = {
    # EVM
    "ETH":   "ethereum",
    "BNB":   "bsc",
    "MATIC": "polygon",
    "USDT":  "bsc",        # default USDT = BSC BEP-20
    "USDC":  "ethereum",
    "DAI":   "ethereum",
    "SHIB":  "ethereum",
    # UTXO
    "BTC":   "bitcoin",
    "LTC":   "litecoin",
    "DOGE":  "dogecoin",
    "BCH":   "bitcoincash",
    "TRX":   "tron",
    # Other
    "XMR":   "monero",
    "TON":   "ton",
    "SOL":   "solana",
}

EVM_NETWORKS  = {"ethereum", "bsc", "polygon"}
UTXO_NETWORKS = {"bitcoin", "litecoin", "dogecoin", "bitcoincash", "tron"}


# ── Exceptions ─────────────────────────────────────────────────────────────────

class WalletError(Exception):               pass
class EncryptionError(WalletError):         pass
class UnsupportedNetworkError(WalletError): pass
class InsufficientFundsError(WalletError):  pass
class SigningError(WalletError):            pass
class BroadcastError(WalletError):          pass
class ConfirmationTimeoutError(WalletError):pass
class GasEstimationError(WalletError):      pass
class FeeCalculationError(WalletError):     pass


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WalletInfo:
    network:               str
    address:               str
    encrypted_private_key: str   # safe to persist in DB

@dataclass(frozen=True)
class SendReceipt:
    network:      str
    tx_hash:      str
    from_:        str
    to:           str
    amount:       Decimal
    token_symbol: str
    fee_paid:     Optional[Decimal] = None

class WithdrawalStatus(str, enum.Enum):
    PENDING      = "pending"
    SIGNING      = "signing"
    BROADCASTING = "broadcasting"
    BROADCAST    = "broadcast"
    CONFIRMING   = "confirming"
    CONFIRMED    = "confirmed"
    FAILED       = "failed"
    REFUNDED     = "refunded"

@dataclass
class WithdrawalRecord:
    withdrawal_id: str
    user_id:       int
    network:       str
    token_symbol:  str
    gross_amount:  Decimal
    to_address:    str
    status:        WithdrawalStatus = WithdrawalStatus.PENDING
    tx_hash:       Optional[str]    = None
    confirmations: int              = 0
    block_number:  Optional[int]    = None
    fee_estimate:  Optional[Decimal]= None
    net_amount:    Optional[Decimal]= None
    error:         Optional[str]    = None
    updated_at:    float            = field(default_factory=time.time)

    def transition(self, status: WithdrawalStatus, **kw) -> None:
        self.status = status; self.updated_at = time.time()
        for k, v in kw.items(): setattr(self, k, v)
        logger.info("[withdrawal:%s] → %s", self.withdrawal_id[:8], status.value)


# ── Key manager ────────────────────────────────────────────────────────────────

class KeyManager:
    """Fernet encrypt / decrypt for all key types."""

    def __init__(self, master_key: str = "") -> None:
        raw = master_key or cfg.wallet_master_key
        if not raw:
            raise EncryptionError("WALLET_MASTER_KEY not set.")
        try:
            self._fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
        except Exception as exc:
            raise EncryptionError(f"Invalid master key: {exc}") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise EncryptionError("Decryption failed — wrong master key") from exc

    # ── EVM ────────────────────────────────────────────────────────────────────
    def evm_account(self, encrypted_key: str) -> LocalAccount:
        return Account.from_key(self.decrypt(encrypted_key))

    # ── UTXO (BTC/LTC/DOGE/BCH/TRX) ──────────────────────────────────────────
    def utxo_wif(self, encrypted_key: str) -> str:
        return self.decrypt(encrypted_key)

    # ── Monero ─────────────────────────────────────────────────────────────────
    def xmr_spend_key(self, encrypted_key: str) -> str:
        return self.decrypt(encrypted_key)

    # ── TON ────────────────────────────────────────────────────────────────────
    def ton_mnemonics(self, encrypted_key: str) -> list[str]:
        return self.decrypt(encrypted_key).split(" ")

    # ── Solana ─────────────────────────────────────────────────────────────────
    def solana_keypair(self, encrypted_key: str) -> Keypair:
        return Keypair.from_bytes(base58.b58decode(self.decrypt(encrypted_key)))


# ── UTXO wallet helper ─────────────────────────────────────────────────────────

def _gen_utxo(crypto_class, network: str, key_mgr: KeyManager) -> WalletInfo:
    from hdwallet import HDWallet
    from hdwallet.entropies import BIP39Entropy
    entropy = BIP39Entropy(entropy=secrets.token_hex(16))
    w = HDWallet(cryptocurrency=crypto_class, semantic="p2pkh")
    w.from_entropy(entropy=entropy)
    addr = w.address()
    wif  = w.wif()
    enc  = key_mgr.encrypt(wif)
    logger.info("[%s] Generated wallet: %s", network, addr)
    return WalletInfo(network=network, address=addr, encrypted_private_key=enc)

def _utxo_crypto(network: str):
    """Return the hdwallet cryptocurrency class for a UTXO network."""
    from hdwallet.cryptocurrencies import (
        Bitcoin, Litecoin, Dogecoin, BitcoinCash, Tron
    )
    return {
        "bitcoin":     Bitcoin,
        "litecoin":    Litecoin,
        "dogecoin":    Dogecoin,
        "bitcoincash": BitcoinCash,
        "tron":        Tron,
    }[network]


# ── Wallet manager ─────────────────────────────────────────────────────────────

class WalletManager:
    """
    Generates and manages wallets for ALL supported coins.
    Handles balances and native-coin sends for EVM and Solana.
    UTXO/XMR/TON sends are noted as pending for manual/external broadcast
    (no free UTXO broadcast API is bundled — use BlockCypher or similar).
    """

    def __init__(self, master_key: str = "") -> None:
        self._key_mgr = KeyManager(master_key)
        self._evm:    Dict[str, AsyncWeb3]   = {}
        self._sol:    Optional[SolanaClient] = None

    async def init(self) -> None:
        for net, url in cfg.rpc_urls.items():
            if net == "solana":
                self._sol = SolanaClient(url)
            else:
                w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(url, request_kwargs={"timeout": 15}))
                if net in POA_NETWORKS:
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                self._evm[net] = w3
        logger.info("WalletManager connected")

    async def close(self) -> None:
        for w3 in self._evm.values():
            try: await w3.provider.disconnect()
            except Exception: pass
        if self._sol:
            await self._sol.close()

    async def __aenter__(self) -> "WalletManager":
        await self.init(); return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Generate wallet ────────────────────────────────────────────────────────

    async def generate_wallet(self, network: str) -> WalletInfo:
        """Generate a fresh wallet for any supported network."""
        if network in EVM_NETWORKS:
            return self._gen_evm(network)
        if network == "solana":
            return self._gen_solana()
        if network in UTXO_NETWORKS:
            return _gen_utxo(_utxo_crypto(network), network, self._key_mgr)
        if network == "monero":
            return self._gen_xmr()
        if network == "ton":
            return self._gen_ton()
        raise UnsupportedNetworkError(f"Unsupported network: {network}")

    def _gen_evm(self, network: str) -> WalletInfo:
        acct = Account.create()
        enc  = self._key_mgr.encrypt(acct.key.hex())
        logger.info("[%s] Generated EVM wallet: %s", network, acct.address)
        return WalletInfo(network=network, address=acct.address, encrypted_private_key=enc)

    def _gen_solana(self) -> WalletInfo:
        kp   = Keypair()
        enc  = self._key_mgr.encrypt(base58.b58encode(bytes(kp)).decode())
        addr = str(kp.pubkey())
        logger.info("[solana] Generated wallet: %s", addr)
        return WalletInfo(network="solana", address=addr, encrypted_private_key=enc)

    def _gen_xmr(self) -> WalletInfo:
        from hdwallet import HDWallet
        from hdwallet.cryptocurrencies import Monero
        from hdwallet.entropies import BIP39Entropy
        entropy = BIP39Entropy(entropy=secrets.token_hex(16))
        w = HDWallet(cryptocurrency=Monero)
        w.from_entropy(entropy=entropy)
        addr = w.primary_address()
        enc  = self._key_mgr.encrypt(w.spend_private_key())
        logger.info("[monero] Generated wallet: %s", addr[:20])
        return WalletInfo(network="monero", address=addr, encrypted_private_key=enc)

    def _gen_ton(self) -> WalletInfo:
        from tonsdk.crypto import mnemonic_new
        from tonsdk.contract.wallet import WalletVersionEnum, Wallets
        mnemonics = mnemonic_new(24)
        _, _, _, wallet = Wallets.from_mnemonics(mnemonics, WalletVersionEnum.v4r2, workchain=0)
        addr = wallet.address.to_string(True, True, False)
        enc  = self._key_mgr.encrypt(" ".join(mnemonics))
        logger.info("[ton] Generated wallet: %s", addr[:20])
        return WalletInfo(network="ton", address=addr, encrypted_private_key=enc)

    # ── Balance queries ────────────────────────────────────────────────────────

    async def get_native_balance(self, network: str, address: str) -> Decimal:
        try:
            if network in self._evm:
                w3  = self._evm[network]
                raw = await asyncio.wait_for(w3.eth.get_balance(w3.to_checksum_address(address)), 10)
                return Decimal(raw) / Decimal(10 ** 18)
            if network == "solana":
                pub  = Pubkey.from_string(address)
                resp = await asyncio.wait_for(self._sol.get_balance(pub), 10)
                return Decimal(resp.value) / Decimal(10 ** 9)
        except Exception as exc:
            logger.warning("[%s] Balance check failed: %s", network, exc)
        return Decimal("0")

    # ── Send native (EVM + Solana only — UTXO/XMR/TON via external broadcast) ─

    async def send_native(
        self, network: str, encrypted_key: str, to: str, amount: Decimal,
        gas_price_gwei: Optional[Decimal] = None,
    ) -> SendReceipt:
        if network in self._evm:
            return await self._send_evm(network, encrypted_key, to, amount, gas_price_gwei)
        if network == "solana":
            return await self._send_sol(encrypted_key, to, amount)
        raise UnsupportedNetworkError(
            f"Direct broadcast not yet supported for {network}. "
            "Use an external API like BlockCypher for UTXO chains."
        )

    async def _send_evm(
        self, network: str, enc: str, to: str, amount: Decimal,
        gas_gwei: Optional[Decimal],
    ) -> SendReceipt:
        w3        = self._evm[network]
        acct      = self._key_mgr.evm_account(enc)
        cs_to     = w3.to_checksum_address(to)
        value     = Wei(int(amount * Decimal(10 ** 18)))
        gas_price = Wei(int(gas_gwei * Decimal(10**9)) if gas_gwei
                        else int(await w3.eth.gas_price * 1.1))
        nonce     = await w3.eth.get_transaction_count(acct.address, "pending")
        tx: TxParams = {
            "to": cs_to, "from": acct.address, "value": value,
            "nonce": nonce, "gas": 21_000,
            "gasPrice": gas_price, "chainId": CHAIN_IDS[network],
        }
        signed  = acct.sign_transaction(tx)
        tx_hash = (await w3.eth.send_raw_transaction(signed.raw_transaction)).hex()
        logger.info("[%s] Sent %s %s tx=%s", network, amount, NATIVE_SYMBOLS[network], tx_hash[:12])
        return SendReceipt(network=network, tx_hash=tx_hash, from_=acct.address,
                           to=cs_to, amount=amount, token_symbol=NATIVE_SYMBOLS[network],
                           fee_paid=Decimal(gas_price * 21_000) / Decimal(10**18))

    async def _send_sol(self, enc: str, to: str, amount: Decimal) -> SendReceipt:
        kp       = self._key_mgr.solana_keypair(enc)
        to_pub   = Pubkey.from_string(to)
        lamports = int(amount * Decimal(10**9))
        bh       = (await self._sol.get_latest_blockhash()).value.blockhash
        ix       = sol_transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to_pub, lamports=lamports))
        msg      = MessageV0.try_compile(kp.pubkey(), [ix], [], bh)
        vtx      = VersionedTransaction(msg, [kp])
        sig      = str((await self._sol.send_transaction(vtx)).value)
        logger.info("[solana] Sent %s SOL sig=%s", amount, sig[:12])
        return SendReceipt(network="solana", tx_hash=sig, from_=str(kp.pubkey()),
                           to=to, amount=amount, token_symbol="SOL")

    def _require_evm(self, network: str) -> AsyncWeb3:
        if network not in self._evm:
            raise UnsupportedNetworkError(f"No EVM connection for {network}")
        return self._evm[network]


# ── Withdrawal pipeline ────────────────────────────────────────────────────────

class WithdrawalPipeline:
    """Full withdrawal lifecycle with fee calc, broadcast, and confirmation polling."""

    def __init__(
        self,
        wallet_manager: WalletManager,
        on_status:     Optional[Callable] = None,
        on_confirmed:  Optional[Callable] = None,
        on_failed:     Optional[Callable] = None,
    ) -> None:
        self._wm           = wallet_manager
        self._on_status    = on_status
        self._on_confirmed = on_confirmed
        self._on_failed    = on_failed

    async def quote(self, amount: Decimal) -> dict:
        pct       = (amount * WITHDRAWAL_PCT_FEE).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        total_fee = (WITHDRAWAL_FIXED_FEE + pct).quantize(Decimal("0.00000001"))
        net       = (amount - total_fee).quantize(Decimal("0.00000001"))
        return {"gross": amount, "fee": total_fee, "net": net,
                "fixed": WITHDRAWAL_FIXED_FEE, "pct": pct}

    async def execute(
        self,
        user_id:               int,
        network:               str,
        token_symbol:          str,
        gross_amount:          Decimal,
        to_address:            str,
        from_address:          str,
        encrypted_private_key: str,
        token_address:         Optional[str] = None,
        token_decimals:        int           = 18,
    ) -> WithdrawalRecord:
        q = await self.quote(gross_amount)
        if q["net"] <= 0:
            raise FeeCalculationError(f"Net {q['net']} after fees — increase amount.")

        record = WithdrawalRecord(
            withdrawal_id=str(uuid.uuid4()),
            user_id=user_id, network=network, token_symbol=token_symbol,
            gross_amount=gross_amount, to_address=to_address,
            fee_estimate=q["fee"], net_amount=q["net"],
        )

        try:
            record.transition(WithdrawalStatus.SIGNING)
            await self._notify(record)

            for attempt in range(1, MAX_BROADCAST_RETRIES + 1):
                try:
                    record.transition(WithdrawalStatus.BROADCASTING)
                    receipt = await self._wm.send_native(
                        network, encrypted_private_key, to_address, q["net"]
                    )
                    record.transition(WithdrawalStatus.BROADCAST, tx_hash=receipt.tx_hash)
                    await self._notify(record)
                    break
                except BroadcastError:
                    if attempt == MAX_BROADCAST_RETRIES: raise
                    await asyncio.sleep(BROADCAST_RETRY_DELAY * attempt)

            await self._poll_confirmation(record)

        except (BroadcastError, UnsupportedNetworkError, SigningError, FeeCalculationError) as exc:
            record.transition(WithdrawalStatus.FAILED, error=str(exc))
            await self._notify(record)
            if self._on_failed: await self._on_failed(record)
        except ConfirmationTimeoutError as exc:
            record.transition(WithdrawalStatus.FAILED, error=str(exc))
            await self._notify(record)
        except Exception as exc:
            record.transition(WithdrawalStatus.FAILED, error=str(exc))
            await self._notify(record)
            if self._on_failed: await self._on_failed(record)

        return record

    async def _poll_confirmation(self, record: WithdrawalRecord) -> None:
        required = REQUIRED_CONFIRMATIONS.get(record.network, 12)
        record.transition(WithdrawalStatus.CONFIRMING)
        await self._notify(record)

        for _ in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)
            confirmed, failed = await self._check_status(record)
            if failed:
                record.transition(WithdrawalStatus.FAILED, error="tx reverted on-chain")
                await self._notify(record)
                if self._on_failed: await self._on_failed(record)
                return
            if confirmed:
                record.transition(WithdrawalStatus.CONFIRMED)
                await self._notify(record)
                if self._on_confirmed: await self._on_confirmed(record)
                return

        raise ConfirmationTimeoutError(f"tx not confirmed after {MAX_POLL_ATTEMPTS * POLL_INTERVAL:.0f}s")

    async def _check_status(self, record: WithdrawalRecord) -> tuple[bool, bool]:
        net = record.network; tx = record.tx_hash
        req = REQUIRED_CONFIRMATIONS.get(net, 12)
        try:
            if net == "solana":
                sig  = SolSignature.from_string(tx)
                resp = await asyncio.wait_for(
                    self._wm._sol.get_signature_statuses([sig], search_transaction_history=True), 10)
                st = resp.value[0] if resp.value else None
                if not st: return False, False
                if st.err: return False, True
                confs = int(st.confirmations or 0)
                record.confirmations = confs
                return ("finalized" in str(getattr(st,"confirmation_status","")) or confs >= req), False
            elif net in EVM_NETWORKS:
                w3      = self._wm._require_evm(net)
                receipt = await asyncio.wait_for(w3.eth.get_transaction_receipt(tx), 10)
                if not receipt: return False, False
                cur   = await w3.eth.block_number
                confs = max(0, cur - receipt["blockNumber"])
                record.confirmations = confs; record.block_number = receipt["blockNumber"]
                if receipt.get("status") == 0: return False, True
                return confs >= req, False
        except Exception:
            pass
        return False, False

    async def _notify(self, record: WithdrawalRecord) -> None:
        if self._on_status:
            try: await self._on_status(record)
            except Exception as exc: logger.warning("Status callback: %s", exc)
