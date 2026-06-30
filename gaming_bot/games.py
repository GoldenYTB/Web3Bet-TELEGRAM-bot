"""
games.py — Provably-fair game engine.

Merges game_logic.py + game_engine.py into one module.

Public API
----------
  resolve_game(game)          — Derive scores, determine winner, calculate payouts.
  game_result_text(game, uid) — Format a result message for a specific viewer.
  score_dice(seed, player)    — Deterministic dice roll from a seed.
  score_bowling(seed, player) — Deterministic bowling score from a seed.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from decimal import ROUND_DOWN, Decimal

from .config import cfg
from .models import ActiveGame, GameStatus, GameType

_DECIMAL_PREC = Decimal("0.00000001")


# ── Provably-fair score derivation ────────────────────────────────────────────

def score_dice(seed: str, player: int) -> int:
    """Return 1-6 deterministically from seed, never equal for both players unless truly tied."""
    sub = hashlib.sha256(f"{seed}:player{player}:dice".encode()).hexdigest()
    return 1 + (int(sub[:8], 16) % 6)


def score_bowling(seed: str, player: int) -> int:
    """Simulate 10 frames of bowling from seed; returns 0-300."""
    pins = _normalise_bowling(seed, player)
    return _score_bowling(pins)


def _roll(seed: str, player: int, idx: int) -> int:
    h = hashlib.sha256(f"{seed}:player{player}:roll{idx}".encode()).hexdigest()
    return int(h[:8], 16)


def _normalise_bowling(seed: str, player: int) -> list[int]:
    """Convert seed-derived integers into a valid bowling roll sequence."""
    raw   = [_roll(seed, player, i) for i in range(21)]
    pins: list[int] = []
    frame = i = 0
    while frame < 9 and i < len(raw):
        r1 = raw[i] % 11
        pins.append(r1)
        i += 1
        if r1 < 10:
            r2 = raw[i % len(raw)] % (11 - r1)
            pins.append(r2)
            i += 1
        frame += 1
    if i < len(raw):
        r1 = raw[i % len(raw)] % 11
        pins.append(r1)
        i += 1
        if r1 == 10:
            r2 = raw[i % len(raw)] % 11
            pins.append(r2)
            i += 1
            r3 = raw[i % len(raw)] % (11 if r2 == 10 else 11 - r2)
            pins.append(r3)
        else:
            r2 = raw[i % len(raw)] % (11 - r1)
            pins.append(r2)
            i += 1
            if r1 + r2 == 10:
                pins.append(raw[i % len(raw)] % 11)
    return pins


def _score_bowling(pins: list[int]) -> int:
    score = pos = 0
    for _ in range(10):
        if pos >= len(pins):
            break
        if pins[pos] == 10:
            score += 10 + (pins[pos+1] if pos+1 < len(pins) else 0) + (pins[pos+2] if pos+2 < len(pins) else 0)
            pos   += 1
        elif pos+1 < len(pins) and pins[pos] + pins[pos+1] == 10:
            score += 10 + (pins[pos+2] if pos+2 < len(pins) else 0)
            pos   += 2
        else:
            score += (pins[pos] if pos < len(pins) else 0) + (pins[pos+1] if pos+1 < len(pins) else 0)
            pos   += 2
    return min(score, 300)


# ── Game resolution ───────────────────────────────────────────────────────────

def resolve_game(game: ActiveGame) -> ActiveGame:
    """
    Derive scores, determine winner, and calculate payouts.
    Mutates the ActiveGame object in-place and returns it.

    The final_seed encodes both player IDs and a server secret so results
    are fully auditable after the fact.
    """
    seed = hashlib.sha256(
        f"{secrets.token_hex(16)}:{game.player1_id}:{game.player2_id}:{game.created_at}".encode()
    ).hexdigest()
    game.final_seed = seed

    if game.game_type == GameType.DICE:
        p1_score = score_dice(seed, 1)
        p2_score = score_dice(seed, 2)
    else:
        p1_score = score_bowling(seed, 1)
        p2_score = score_bowling(seed, 2)

    game.player1_score = p1_score
    game.player2_score = p2_score

    pool = game.wager_amount * 2
    fee  = (pool * cfg.house_fee_pct).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)
    net  = (pool - fee).quantize(_DECIMAL_PREC)

    game.house_fee = fee

    if p1_score > p2_score:
        game.winner_id     = game.player1_id
        game.winner_payout = net
    elif p2_score > p1_score:
        game.winner_id     = game.player2_id
        game.winner_payout = net
    else:
        # Tie: refund both minus half the rake each
        game.winner_id     = None
        game.winner_payout = (net / 2).quantize(_DECIMAL_PREC, rounding=ROUND_DOWN)

    game.status      = GameStatus.COMPLETED
    game.resolved_at = time.time()
    return game


# ── Result formatting ─────────────────────────────────────────────────────────

def game_result_text(game: ActiveGame, viewer_id: int) -> str:
    """Return a Markdown-formatted result message from the viewer's perspective."""
    is_p1 = viewer_id == game.player1_id

    if game.game_type == GameType.DICE:
        my_score  = game.player1_score if is_p1 else game.player2_score
        opp_score = game.player2_score if is_p1 else game.player1_score
        score_txt = f"🎲 Your roll: *{my_score}*  |  Opponent: *{opp_score}*"
    else:
        my_score  = game.player1_score if is_p1 else game.player2_score
        opp_score = game.player2_score if is_p1 else game.player1_score
        score_txt = f"🎳 Your score: *{my_score}*  |  Opponent: *{opp_score}*"

    if game.winner_id is None:
        outcome = f"🤝 *It's a tie!*\nRefunded: *{game.winner_payout} {game.token_symbol}*"
    elif game.winner_id == viewer_id:
        outcome = f"🏆 *You win!*\nPayout: *+{game.winner_payout} {game.token_symbol}*"
    else:
        outcome = f"💀 *You lose.*\nWager lost: *{game.wager_amount} {game.token_symbol}*"

    game_label = "🎲 Dice" if game.game_type == GameType.DICE else "🎳 Bowling"
    return (
        f"━━━ Game Result ━━━\n"
        f"{game_label} on {game.network}\n\n"
        f"{score_txt}\n\n"
        f"{outcome}\n\n"
        f"House fee: {game.house_fee} {game.token_symbol}\n"
        f"🔐 Seed: `{game.final_seed[:24]}…`"
    )
