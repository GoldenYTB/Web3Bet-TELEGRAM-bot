"""
game_registry.py — Central registry for all games.

Controls which games are enabled/disabled.
Admin can toggle any game on/off at runtime.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class GameInfo:
    key:         str          # internal key e.g. "dice"
    name:        str          # display name
    emoji:       str
    category:    str          # "pvp" or "house"
    enabled:     bool         # current state
    description: str
    min_wager:   float = 0.50
    max_wager:   float = 1000.0
    toggleable:  bool  = True  # False = cannot be toggled (e.g. Poker until Mini App)


# ── Master game list ──────────────────────────────────────────────────────────
ALL_GAMES: Dict[str, GameInfo] = {

    # ── PvP games (group chat) ────────────────────────────────────────────────
    "dice": GameInfo(
        key="dice", name="Dice", emoji="🎲", category="pvp", enabled=True,
        description="Roll 1–6. Highest wins. Modes: Normal, Crazy, Double, Double Crazy.",
    ),
    "bowling": GameInfo(
        key="bowling", name="Bowling", emoji="🎳", category="pvp", enabled=True,
        description="Bowl 0–6 pins. Highest wins. Same modes as Dice.",
    ),
    "darts": GameInfo(
        key="darts", name="Darts", emoji="🎯", category="pvp", enabled=True,
        description="3 throws each. Bullseye=50pts. Highest total wins.",
    ),
    "poker": GameInfo(
        key="poker", name="Poker", emoji="♠️", category="pvp", enabled=False,
        description="Texas Hold'em — coming in Phase 2 (Mini App).",
        toggleable=False,
    ),

    # ── House games (private chat) ─────────────────────────────────────────────
    "coinflip": GameInfo(
        key="coinflip", name="CoinFlip", emoji="🪙", category="house", enabled=False,
        description="Pick Heads or Tails. Win 1.98x your wager.",
    ),
    "rps": GameInfo(
        key="rps", name="Rock Paper Scissors", emoji="✊", category="house", enabled=False,
        description="Beat the house. Win 1.98x or tie (returned).",
    ),
    "roulette": GameInfo(
        key="roulette", name="Roulette", emoji="🎡", category="house", enabled=False,
        description="European roulette. Bet on number, color, odd/even, dozens.",
    ),
    "blackjack": GameInfo(
        key="blackjack", name="Blackjack", emoji="🃏", category="house", enabled=False,
        description="Beat the dealer. Blackjack pays 2.5x.",
    ),
    "baccarat": GameInfo(
        key="baccarat", name="Baccarat", emoji="💎", category="house", enabled=False,
        description="Bet Player, Banker or Tie. Banker pays 1.95x.",
    ),
    "keno": GameInfo(
        key="keno", name="Keno", emoji="🎯", category="house", enabled=False,
        description="Pick 1-10 numbers from 1-80. Match to win big.",
    ),
    "crash": GameInfo(
        key="crash", name="Crash", emoji="🚀", category="house", enabled=False,
        description="Multiplier grows — cash out before it crashes!",
    ),
    "plinko": GameInfo(
        key="plinko", name="Plinko", emoji="⚡", category="house", enabled=False,
        description="Ball drops through pegs. Pick risk level.",
    ),
    "mines": GameInfo(
        key="mines", name="Mines", emoji="💣", category="house", enabled=False,
        description="Reveal gems, avoid mines. Cash out anytime.",
    ),
    "limbo": GameInfo(
        key="limbo", name="Limbo", emoji="🌊", category="house", enabled=False,
        description="Set a target multiplier. Win if result exceeds it.",
    ),
    "tower": GameInfo(
        key="tower", name="Tower", emoji="🏗️", category="house", enabled=False,
        description="Pick safe tiles floor by floor. Climb for multiplier.",
    ),
}


class GameRegistry:
    """Runtime game state manager. Loaded once, mutated by admin."""

    def __init__(self) -> None:
        # Deep copy so changes don't affect module-level dict
        self._games: Dict[str, GameInfo] = {k: GameInfo(**v.__dict__) for k, v in ALL_GAMES.items()}

    def get(self, key: str) -> Optional[GameInfo]:
        return self._games.get(key)

    def all(self) -> List[GameInfo]:
        return list(self._games.values())

    def enabled(self, category: Optional[str] = None) -> List[GameInfo]:
        return [g for g in self._games.values()
                if g.enabled and (category is None or g.category == category)]

    def toggle(self, key: str, state: bool) -> Optional[str]:
        """Toggle a game. Returns error string or None on success."""
        g = self._games.get(key)
        if not g:
            return f"Unknown game: {key}"
        if not g.toggleable:
            return f"{g.name} cannot be toggled (Phase 2 feature)"
        g.enabled = state
        return None

    def list_text(self) -> str:
        lines = ["🎮 *Game List*\n"]
        pvp_games   = [g for g in self._games.values() if g.category == "pvp"]
        house_games = [g for g in self._games.values() if g.category == "house"]
        lines.append("*PvP Games (group chat):*")
        for g in pvp_games:
            status = "✅" if g.enabled else "❌"
            note   = " _(Phase 2)_" if not g.toggleable else ""
            lines.append(f"  {status} {g.emoji} {g.name}{note}")
        lines.append("\n*House Games (private chat):*")
        for g in house_games:
            status = "✅" if g.enabled else "❌"
            lines.append(f"  {status} {g.emoji} {g.name}")
        return "\n".join(lines)

    def games_keyboard_data(self, category: Optional[str] = None) -> List[GameInfo]:
        """Returns enabled games for building inline keyboards."""
        return self.enabled(category)
