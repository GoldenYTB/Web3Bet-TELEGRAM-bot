"""
games.py — Game engine using real Telegram dice/darts/bowling emoji values.

Telegram controls all randomness — the bot just receives the values
Telegram reports for each emoji send. This makes results 100% provably fair
because neither the bot nor the players control the outcome.

Scoring:
  🎲 Dice    — value 1-6  (Telegram reports exact value)
  🎳 Bowling — value 1-6  (Telegram maps to pins: 6=strike)
  🎯 Darts   — value 1-6  (Telegram: 1=miss, 2=outer, 3-4=middle, 5=bull outer, 6=bullseye)
               3 throws each, total score

Modes:
  normal       — highest total wins
  crazy        — lowest total wins
  double       — 2 rolls each, totalled
  double_crazy — 2 rolls each, lowest total wins

Darts scoring per throw:
  6 = Bullseye     → 50 pts
  5 = Outer bull   → 25 pts
  4 = Triple ring  → 15 pts
  3 = Double ring  → 10 pts
  2 = Single       →  5 pts
  1 = Miss         →  0 pts
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Dict, List, Optional

from .config import cfg, GAME_TYPES, GAME_MODES

_DECIMAL_PREC = Decimal("0.00000001")


# ── Darts scoring table ───────────────────────────────────────────────────────
DART_SCORES: Dict[int, int] = {
    1: 0,    # Miss
    2: 5,    # Single
    3: 10,   # Double ring
    4: 15,   # Triple ring
    5: 25,   # Outer bull
    6: 50,   # Bullseye
}

DART_LABELS: Dict[int, str] = {
    1: "Miss 💨",
    2: "Single 🎯",
    3: "Double Ring 🎯🎯",
    4: "Triple Ring 🎯🎯🎯",
    5: "Outer Bull ⭕",
    6: "BULLSEYE! 🎯💥",
}


# ── Game data classes ─────────────────────────────────────────────────────────

class GameStatus(str, Enum):
    WAITING   = "waiting"     # created, waiting for 2nd player
    ACTIVE    = "active"      # both joined, waiting for emoji rolls
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED   = "expired"     # join timeout hit


class GameType(str, Enum):
    DICE    = "dice"
    BOWLING = "bowling"
    DARTS   = "darts"


class GameMode(str, Enum):
    NORMAL       = "normal"
    CRAZY        = "crazy"
    DOUBLE       = "double"
    DOUBLE_CRAZY = "double_crazy"

    @property
    def rolls_needed(self) -> int:
        """How many emoji sends each player needs."""
        if self in (GameMode.DOUBLE, GameMode.DOUBLE_CRAZY):
            return 2
        if self == GameMode.NORMAL and False:  # darts handled separately
            return 1
        return 1

    @property
    def lowest_wins(self) -> bool:
        return self in (GameMode.CRAZY, GameMode.DOUBLE_CRAZY)


@dataclass
class PlayerRolls:
    rolls:  List[int] = field(default_factory=list)  # raw Telegram dice values

    def total(self, game_type: GameType, game_mode: GameMode) -> int:
        """Compute this player's final score."""
        if not self.rolls:
            return 0
        if game_type == GameType.DARTS:
            return sum(DART_SCORES.get(v, 0) for v in self.rolls)
        # Dice / Bowling: sum of raw values
        return sum(self.rolls)

    def is_complete(self, game_type: GameType, game_mode: GameMode) -> bool:
        rolls_per_player = 3 if game_type == GameType.DARTS else game_mode.rolls_needed
        return len(self.rolls) >= rolls_per_player

    def emoji_name(self, game_type: GameType) -> str:
        return {"dice": "🎲", "bowling": "🎳", "darts": "🎯"}[game_type.value]


@dataclass
class GroupGame:
    """A live game running in a group chat. All amounts in USD."""
    game_id:       str
    chat_id:       int
    message_id:    int           # the join-lobby message to edit
    game_type:     GameType
    game_mode:     GameMode
    wager_usd:     Decimal       # wager in USD (e.g. Decimal("5.00"))

    creator_id:    int
    creator_name:  str
    joiner_id:     Optional[int]  = None
    joiner_name:   Optional[str]  = None

    status:        GameStatus     = GameStatus.WAITING
    created_at:    float          = field(default_factory=time.time)
    resolved_at:   Optional[float]= None

    # Rolls collected during ACTIVE phase
    p1_rolls:      PlayerRolls    = field(default_factory=PlayerRolls)
    p2_rolls:      PlayerRolls    = field(default_factory=PlayerRolls)

    # Results (all USD)
    p1_score:      int            = 0
    p2_score:      int            = 0
    winner_id:     Optional[int]  = None
    winner_payout_usd_usd: Decimal    = Decimal("0")
    house_fee_usd_usd:     Decimal    = Decimal("0")

    @property
    def rolls_per_player(self) -> int:
        if self.game_type == GameType.DARTS:
            return 3
        return self.game_mode.rolls_needed

    @property
    def emoji(self) -> str:
        return {"dice": "🎲", "bowling": "🎳", "darts": "🎯"}[self.game_type.value]

    def add_roll(self, user_id: int, value: int) -> bool:
        """Add a roll value. Returns True if this player is now done rolling."""
        if user_id == self.creator_id and not self.p1_rolls.is_complete(self.game_type, self.game_mode):
            self.p1_rolls.rolls.append(value)
            return self.p1_rolls.is_complete(self.game_type, self.game_mode)
        elif user_id == self.joiner_id and not self.p2_rolls.is_complete(self.game_type, self.game_mode):
            self.p2_rolls.rolls.append(value)
            return self.p2_rolls.is_complete(self.game_type, self.game_mode)
        return False

    def both_done(self) -> bool:
        return (self.p1_rolls.is_complete(self.game_type, self.game_mode) and
                self.p2_rolls.is_complete(self.game_type, self.game_mode))

    def resolve(self) -> None:
        """Calculate winner and payouts once both players have rolled."""
        p1 = self.p1_rolls.total(self.game_type, self.game_mode)
        p2 = self.p2_rolls.total(self.game_type, self.game_mode)
        self.p1_score = p1
        self.p2_score = p2

        pool = (self.wager_usd * 2).quantize(_DECIMAL_PREC)
        fee  = (pool * cfg.house_fee_pct).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)
        net  = (pool - fee).quantize(_DECIMAL_PREC)
        self.house_fee_usd = fee

        # In crazy mode, lower score wins
        if self.game_mode.lowest_wins:
            if p1 < p2:
                self.winner_id = self.creator_id
                self.winner_payout_usd = net
            elif p2 < p1:
                self.winner_id = self.joiner_id
                self.winner_payout_usd = net
            else:
                self.winner_id = None
                self.winner_payout_usd = (net / 2).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)
        else:
            if p1 > p2:
                self.winner_id = self.creator_id
                self.winner_payout_usd = net
            elif p2 > p1:
                self.winner_id = self.joiner_id
                self.winner_payout_usd = net
            else:
                self.winner_id = None
                self.winner_payout_usd = (net / 2).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)

        self.status      = GameStatus.COMPLETED
        self.resolved_at = time.time()


# ── Result text formatters ────────────────────────────────────────────────────

def _dart_roll_label(rolls: List[int]) -> str:
    return "  ".join(f"{DART_LABELS.get(v,'?')} ({DART_SCORES.get(v,0)}pts)" for v in rolls)


def result_text(game: GroupGame) -> str:
    """Full result message shown after game resolves."""
    p1_name = game.creator_name
    p2_name = game.joiner_name or "Player 2"
    # no token_symbol - amounts are in USD
    mode_label = GAME_MODES[game.game_mode.value]["label"]

    if game.game_type == GameType.DARTS:
        p1_detail = _dart_roll_label(game.p1_rolls.rolls)
        p2_detail = _dart_roll_label(game.p2_rolls.rolls)
        score_txt = (
            f"🎯 *{p1_name}*\n{p1_detail}\nTotal: *{game.p1_score} pts*\n\n"
            f"🎯 *{p2_name}*\n{p2_detail}\nTotal: *{game.p2_score} pts*"
        )
    elif game.game_type == GameType.DICE:
        p1_rolls = " + ".join(str(r) for r in game.p1_rolls.rolls)
        p2_rolls = " + ".join(str(r) for r in game.p2_rolls.rolls)
        score_txt = (
            f"🎲 *{p1_name}*: {p1_rolls} = *{game.p1_score}*\n"
            f"🎲 *{p2_name}*: {p2_rolls} = *{game.p2_score}*"
        )
    else:  # bowling
        p1_rolls = " + ".join(str(r) for r in game.p1_rolls.rolls)
        p2_rolls = " + ".join(str(r) for r in game.p2_rolls.rolls)
        score_txt = (
            f"🎳 *{p1_name}*: {p1_rolls} = *{game.p1_score}*\n"
            f"🎳 *{p2_name}*: {p2_rolls} = *{game.p2_score}*"
        )

    if game.winner_id is None:
        outcome = f"🤝 *It's a tie!*\nBoth players get *{game.winner_payout_usd:.2f}* back"
    elif game.winner_id == game.creator_id:
        outcome = f"🏆 *{p1_name} wins!*\nPayout: *{game.winner_payout_usd:.2f}*"
    else:
        outcome = f"🏆 *{p2_name} wins!*\nPayout: *{game.winner_payout_usd:.2f}*"

    crazy_note = "\n_Crazy mode: lowest score wins_ 🤪" if game.game_mode.lowest_wins else ""

    return (
        f"{'━'*22}\n"
        f"{game.emoji} *{GAME_TYPES[game.game_type.value]['label']}* — {mode_label}{crazy_note}\n"
        f"Wager: *${game.wager_usd:.2f} USD*\n"
        f"{'━'*22}\n\n"
        f"{score_txt}\n\n"
        f"{outcome}\n\n"
        f"🏦 House fee: ${game.house_fee_usd:.2f}\n"
        f"_Use /withdraw to cash out in your preferred coin_"
    )


def lobby_text(game: GroupGame) -> str:
    """Message shown while waiting for second player."""
    mode_label = GAME_MODES[game.game_mode.value]["label"]
    desc       = GAME_MODES[game.game_mode.value]["description"]
    parts = [
        f"{game.emoji} *{GAME_TYPES[game.game_type.value]['label']}*",
        f"Mode: *{mode_label}* \u2014 _{desc}_",
        f"Wager: *${game.wager_usd:.2f} USD*",
        "",
        f"\U0001f464 *{game.creator_name}* is waiting for an opponent\u2026",
        "",
        "_Tap Join to play!_",
    ]
    return "\n".join(parts)


def active_text(game: GroupGame) -> str:
    """Message shown while collecting rolls."""
    rolls_needed = game.rolls_per_player
    p1_done = game.p1_rolls.is_complete(game.game_type, game.game_mode)
    p2_done = game.p2_rolls.is_complete(game.game_type, game.game_mode)
    p1_status = f"✅ done ({len(game.p1_rolls.rolls)}/{rolls_needed})" if p1_done else f"⏳ {len(game.p1_rolls.rolls)}/{rolls_needed} rolls"
    p2_status = f"✅ done ({len(game.p2_rolls.rolls)}/{rolls_needed})" if p2_done else f"⏳ {len(game.p2_rolls.rolls)}/{rolls_needed} rolls"
    return (
        f"{game.emoji} *Game in progress!*\n\n"
        f"👤 *{game.creator_name}*: {p1_status}\n"
        f"👤 *{game.joiner_name}*: {p2_status}\n\n"
        f"_Send {game.emoji} in this chat to roll!_\n"
        f"_Each player needs {rolls_needed} roll{'s' if rolls_needed > 1 else ''}._"
    )
