"""
house_games.py — All house games (player vs bot).

Games:
  CoinFlip  — heads or tails, 2x payout
  RPS       — rock paper scissors, 2x payout  
  Roulette  — bet type + number/color, standard payouts
  Blackjack — player vs dealer, standard rules
  Baccarat  — player/banker/tie bet
  Keno      — pick 1-10 numbers from 1-80, match to win
  Crash     — multiplier grows, cash out before crash
  Plinko    — ball drops, lands in multiplier slot
  Mines     — pick tiles, avoid mines, cash out anytime
  Limbo     — target multiplier, random result must exceed
  Tower     — pick safe tile per floor, climb for multiplier

All house games:
  - Played in private chat with bot
  - House pays from house_balance_usd on wins
  - House earns on losses (no rake — house edge built in)
  - Results are provably fair using server seed + client seed
"""
from __future__ import annotations

import hashlib
import hmac
import random
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ── House edge per game ───────────────────────────────────────────────────────
HOUSE_EDGE = {
    "coinflip":  Decimal("0.01"),   # 1% — pays 1.98x
    "rps":       Decimal("0.01"),   # 1% — pays 1.98x
    "roulette":  Decimal("0.027"),  # 2.7% — standard European
    "blackjack": Decimal("0.005"),  # 0.5% — with basic strategy
    "baccarat":  Decimal("0.012"),  # 1.2% on banker bet
    "keno":      Decimal("0.25"),   # 25% — standard keno house edge
    "crash":     Decimal("0.01"),   # 1%
    "plinko":    Decimal("0.01"),   # 1%
    "mines":     Decimal("0.01"),   # 1%
    "limbo":     Decimal("0.01"),   # 1%
    "tower":     Decimal("0.01"),   # 1%
}


# ── Provably fair ─────────────────────────────────────────────────────────────

def generate_server_seed() -> str:
    return secrets.token_hex(32)

def hash_seed(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()

def fair_float(server_seed: str, client_seed: str, nonce: int) -> float:
    """Returns a float 0.0–1.0 using HMAC-SHA256."""
    msg = f"{client_seed}:{nonce}".encode()
    h   = hmac.new(server_seed.encode(), msg, hashlib.sha256).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF

def fair_int(server_seed: str, client_seed: str, nonce: int, low: int, high: int) -> int:
    f = fair_float(server_seed, client_seed, nonce)
    return low + int(f * (high - low + 1))


# ── Game session states ───────────────────────────────────────────────────────

class HouseGameStatus(str, Enum):
    PENDING   = "pending"    # waiting for player action
    ACTIVE    = "active"     # game in progress (multi-step games)
    COMPLETED = "completed"
    CASHED_OUT = "cashed_out"  # for crash/mines/tower
    BUSTED     = "busted"      # for crash


@dataclass
class HouseGameSession:
    """Base session for all house games."""
    session_id:   str
    user_id:      int
    game:         str
    wager_usd:    Decimal
    status:       HouseGameStatus = HouseGameStatus.PENDING

    server_seed:  str = field(default_factory=generate_server_seed)
    client_seed:  str = field(default_factory=lambda: secrets.token_hex(8))
    nonce:        int = 0

    created_at:   float = field(default_factory=time.time)

    # Results
    payout_usd:   Decimal = Decimal("0")
    multiplier:   Decimal = Decimal("0")
    profit_usd:   Decimal = Decimal("0")   # negative = house wins

    # Game-specific state (varies per game)
    state:        Dict    = field(default_factory=dict)

    def next_float(self) -> float:
        self.nonce += 1
        return fair_float(self.server_seed, self.client_seed, self.nonce)

    def next_int(self, low: int, high: int) -> int:
        self.nonce += 1
        return fair_int(self.server_seed, self.client_seed, self.nonce, low, high)

    def proof(self) -> str:
        return (
            f"🔍 *Provably Fair*\n"
            f"Server seed hash: `{hash_seed(self.server_seed)}`\n"
            f"Server seed: `{self.server_seed}`\n"
            f"Client seed: `{self.client_seed}`\n"
            f"Nonce: `{self.nonce}`\n\n"
            f"Verify: HMAC-SHA256(server\\_seed, client\\_seed:nonce)"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  COINFLIP
# ══════════════════════════════════════════════════════════════════════════════

COIN_SIDES = {"heads": "🪙 Heads", "tails": "🔄 Tails"}

def play_coinflip(session: HouseGameSession, choice: str) -> HouseGameSession:
    result = "heads" if session.next_float() < 0.5 else "tails"
    won    = result == choice
    mult   = Decimal("1.98") if won else Decimal("0")
    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    session.state    = {"choice": choice, "result": result, "won": won}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def coinflip_result_text(session: HouseGameSession) -> str:
    s   = session.state
    won = s["won"]
    return (
        f"🪙 *CoinFlip*\n\n"
        f"Your pick: {COIN_SIDES[s['choice']]}\n"
        f"Result: {COIN_SIDES[s['result']]}\n\n"
        f"{'🏆 You win!' if won else '💀 You lose!'}\n"
        f"{'Payout: *$' + str(session.payout_usd) + '*' if won else 'Lost: *$' + str(session.wager_usd) + '*'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ROCK PAPER SCISSORS
# ══════════════════════════════════════════════════════════════════════════════

RPS_CHOICES  = {"rock": "✊ Rock", "paper": "🖐 Paper", "scissors": "✂️ Scissors"}
RPS_BEATS    = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
RPS_HOUSE    = ["rock", "paper", "scissors"]

def play_rps(session: HouseGameSession, choice: str) -> HouseGameSession:
    house  = RPS_HOUSE[session.next_int(0, 2)]
    if choice == house:
        result = "tie"
        mult   = Decimal("1")
    elif RPS_BEATS[choice] == house:
        result = "win"
        mult   = Decimal("1.98")
    else:
        result = "lose"
        mult   = Decimal("0")

    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.state      = {"choice": choice, "house": house, "result": result}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def rps_result_text(session: HouseGameSession) -> str:
    s = session.state
    r = s["result"]
    return (
        f"✊ *Rock Paper Scissors*\n\n"
        f"You: {RPS_CHOICES[s['choice']]}\n"
        f"House: {RPS_CHOICES[s['house']]}\n\n"
        f"{'🤝 Tie! Wager returned.' if r=='tie' else '🏆 You win! Payout: *$' + str(session.payout_usd) + '*' if r=='win' else '💀 You lose! Lost: *$' + str(session.wager_usd) + '*'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ROULETTE
# ══════════════════════════════════════════════════════════════════════════════

ROULETTE_RED    = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_BLACK  = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

def roulette_color(n: int) -> str:
    if n == 0: return "green"
    return "red" if n in ROULETTE_RED else "black"

def roulette_color_emoji(n: int) -> str:
    c = roulette_color(n)
    return {"green":"🟢","red":"🔴","black":"⚫"}[c]

def play_roulette(session: HouseGameSession, bet_type: str, bet_value: str) -> HouseGameSession:
    number = session.next_int(0, 36)
    color  = roulette_color(number)
    emoji  = roulette_color_emoji(number)

    mult = Decimal("0")
    if bet_type == "number" and str(number) == bet_value:
        mult = Decimal("35")
    elif bet_type == "color" and color == bet_value and number != 0:
        mult = Decimal("2")
    elif bet_type == "even" and number != 0 and number % 2 == 0:
        mult = Decimal("2")
    elif bet_type == "odd" and number != 0 and number % 2 == 1:
        mult = Decimal("2")
    elif bet_type == "low" and 1 <= number <= 18:
        mult = Decimal("2")
    elif bet_type == "high" and 19 <= number <= 36:
        mult = Decimal("2")
    elif bet_type == "dozen":
        dozen = int(bet_value)
        if dozen == 1 and 1 <= number <= 12: mult = Decimal("3")
        elif dozen == 2 and 13 <= number <= 24: mult = Decimal("3")
        elif dozen == 3 and 25 <= number <= 36: mult = Decimal("3")

    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.state      = {"number": number, "color": color, "emoji": emoji,
                          "bet_type": bet_type, "bet_value": bet_value, "mult": str(mult)}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def roulette_result_text(session: HouseGameSession) -> str:
    s   = session.state
    won = session.payout_usd > 0
    return (
        f"🎡 *Roulette*\n\n"
        f"Result: {s['emoji']} **{s['number']}** ({s['color'].title()})\n\n"
        f"{'🏆 Win! Payout: *$' + str(session.payout_usd) + '*' if won else '💀 No win! Lost: *$' + str(session.wager_usd) + '*'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BLACKJACK
# ══════════════════════════════════════════════════════════════════════════════

BJ_SUITS  = ["♠️","♥️","♦️","♣️"]
BJ_VALUES = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]

def _bj_deck(session: HouseGameSession) -> List[Tuple[str,int]]:
    deck = []
    for s in BJ_SUITS:
        for v in BJ_VALUES:
            face = v + s
            val  = min(10, BJ_VALUES.index(v) + 1) if v != "A" else 11
            deck.append((face, val))
    # Shuffle using fair random
    for i in range(len(deck) - 1, 0, -1):
        j = session.next_int(0, i)
        deck[i], deck[j] = deck[j], deck[i]
    return deck

def _bj_total(hand: List[Tuple[str,int]]) -> int:
    total = sum(v for _,v in hand)
    aces  = sum(1 for f,_ in hand if "A" in f)
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def _bj_hand_str(hand: List[Tuple[str,int]]) -> str:
    return " ".join(f for f,_ in hand)

def start_blackjack(session: HouseGameSession) -> HouseGameSession:
    deck = _bj_deck(session)
    player = [deck[0], deck[2]]
    dealer = [deck[1], deck[3]]
    session.state = {
        "deck":   [(f,v) for f,v in deck[4:]],
        "player": player,
        "dealer": dealer,
        "done":   False,
    }
    session.status = HouseGameStatus.ACTIVE
    return session

def blackjack_hit(session: HouseGameSession) -> HouseGameSession:
    s    = session.state
    deck = s["deck"]
    s["player"].append(deck.pop(0))
    total = _bj_total(s["player"])
    if total >= 21:
        session = blackjack_stand(session)
    return session

def blackjack_stand(session: HouseGameSession) -> HouseGameSession:
    s      = session.state
    dealer = s["dealer"]
    deck   = s["deck"]
    # Dealer hits until 17+
    while _bj_total(dealer) < 17:
        dealer.append(deck.pop(0))
    p_total = _bj_total(s["player"])
    d_total = _bj_total(dealer)
    bust    = p_total > 21
    if bust:
        mult = Decimal("0")
    elif p_total == 21 and len(s["player"]) == 2:
        mult = Decimal("2.5")   # blackjack pays 3:2
    elif p_total > d_total or d_total > 21:
        mult = Decimal("2")
    elif p_total == d_total:
        mult = Decimal("1")     # push
    else:
        mult = Decimal("0")
    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    s["done"] = True
    return session

def blackjack_status_text(session: HouseGameSession, reveal_dealer: bool = False) -> str:
    s = session.state
    p = _bj_total(s["player"])
    d = _bj_total(s["dealer"]) if reveal_dealer else "?"
    dealer_show = _bj_hand_str(s["dealer"]) if reveal_dealer else f"{s['dealer'][0][0]} 🂠"
    result = ""
    if session.status == HouseGameStatus.COMPLETED:
        if session.payout_usd > session.wager_usd:
            result = f"\n\n🏆 *You win! Payout: ${session.payout_usd}*"
        elif session.payout_usd == session.wager_usd:
            result = f"\n\n🤝 *Push — wager returned*"
        else:
            result = f"\n\n💀 *You lose! Lost: ${session.wager_usd}*"
    return (
        f"🃏 *Blackjack*\n\n"
        f"Your hand: {_bj_hand_str(s['player'])} = **{p}**\n"
        f"Dealer: {dealer_show} = **{d}**"
        f"{result}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BACCARAT
# ══════════════════════════════════════════════════════════════════════════════

def _bac_val(card: int) -> int:
    return min(card % 13, 9)

def _bac_total(hand: List[int]) -> int:
    return sum(_bac_val(c) for c in hand) % 10

def play_baccarat(session: HouseGameSession, bet: str) -> HouseGameSession:
    cards  = [session.next_int(0, 51) for _ in range(6)]
    player = [cards[0], cards[2]]
    banker = [cards[1], cards[3]]
    p_tot  = _bac_total(player)
    b_tot  = _bac_total(banker)
    # Natural check
    if p_tot < 8 and b_tot < 8:
        if p_tot <= 5:
            player.append(cards[4])
            p_tot = _bac_total(player)
        if b_tot <= 5:
            banker.append(cards[5])
            b_tot = _bac_total(banker)
    outcome = "player" if p_tot > b_tot else ("banker" if b_tot > p_tot else "tie")
    mult = Decimal("0")
    if bet == "player" and outcome == "player": mult = Decimal("2")
    elif bet == "banker" and outcome == "banker": mult = Decimal("1.95")  # 5% commission
    elif bet == "tie"    and outcome == "tie":    mult = Decimal("8")
    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.state      = {"p_tot": p_tot, "b_tot": b_tot, "outcome": outcome, "bet": bet}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def baccarat_result_text(session: HouseGameSession) -> str:
    s   = session.state
    won = session.payout_usd > 0
    outcome_labels = {"player":"👤 Player","banker":"🏦 Banker","tie":"🤝 Tie"}
    return (
        f"💎 *Baccarat*\n\n"
        f"Player total: **{s['p_tot']}**\n"
        f"Banker total: **{s['b_tot']}**\n"
        f"Result: {outcome_labels[s['outcome']]}\n\n"
        f"Your bet: {outcome_labels[s['bet']]}\n"
        f"{'🏆 Win! Payout: *$' + str(session.payout_usd) + '*' if won else '💀 Lost: *$' + str(session.wager_usd) + '*'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  KENO
# ══════════════════════════════════════════════════════════════════════════════

KENO_PAYTABLE = {
    1:  {1: Decimal("3.72")},
    2:  {1: Decimal("1"), 2: Decimal("7")},
    3:  {2: Decimal("2"), 3: Decimal("27")},
    4:  {2: Decimal("1"), 3: Decimal("5"), 4: Decimal("80")},
    5:  {3: Decimal("3"), 4: Decimal("20"), 5: Decimal("400")},
    6:  {3: Decimal("1"), 4: Decimal("8"), 5: Decimal("100"), 6: Decimal("1500")},
    7:  {4: Decimal("5"), 5: Decimal("40"), 6: Decimal("500"), 7: Decimal("5000")},
    8:  {5: Decimal("15"), 6: Decimal("150"), 7: Decimal("2000"), 8: Decimal("10000")},
    9:  {6: Decimal("50"), 7: Decimal("500"), 8: Decimal("5000"), 9: Decimal("50000")},
    10: {6: Decimal("20"), 7: Decimal("200"), 8: Decimal("1000"), 9: Decimal("10000"), 10: Decimal("100000")},
}

def play_keno(session: HouseGameSession, picks: List[int]) -> HouseGameSession:
    picks = picks[:10]
    # Draw 20 numbers from 1-80
    pool    = list(range(1, 81))
    drawn   = []
    for i in range(20):
        idx = session.next_int(0, len(pool) - 1)
        drawn.append(pool.pop(idx))
    matches = len(set(picks) & set(drawn))
    n       = len(picks)
    table   = KENO_PAYTABLE.get(n, {})
    mult    = table.get(matches, Decimal("0"))
    payout  = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.state      = {"picks": picks, "drawn": sorted(drawn), "matches": matches}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def keno_result_text(session: HouseGameSession) -> str:
    s       = session.state
    picks   = set(s["picks"])
    drawn   = set(s["drawn"])
    hits    = picks & drawn
    won     = session.payout_usd > 0
    drawn_display = " ".join(
        f"**{n}**" if n in hits else str(n) for n in sorted(s["drawn"])
    )
    return (
        f"🎯 *Keno*\n\n"
        f"Your picks: {' '.join(str(n) for n in sorted(s['picks']))}\n"
        f"Drawn: {drawn_display}\n"
        f"Matches: **{s['matches']}/{len(s['picks'])}**\n\n"
        f"{'🏆 Win! Payout: *$' + str(session.payout_usd) + '* (' + str(session.multiplier) + 'x)' if won else '💀 No win!'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CRASH
# ══════════════════════════════════════════════════════════════════════════════

def generate_crash_point(session: HouseGameSession) -> Decimal:
    """Generate crash point using provably fair system."""
    f = session.next_float()
    if f < 0.01:  # 1% instant crash
        return Decimal("1.00")
    # Standard crash formula: 99 / (1 - f)
    point = Decimal("99") / Decimal(str(1 - f))
    return point.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

def start_crash(session: HouseGameSession) -> HouseGameSession:
    crash_point = generate_crash_point(session)
    session.state = {
        "crash_point":   str(crash_point),
        "cashed_out_at": None,
        "current_mult":  "1.00",
    }
    session.status = HouseGameStatus.ACTIVE
    return session

def crash_cashout(session: HouseGameSession, current_mult: Decimal) -> HouseGameSession:
    crash_point = Decimal(session.state["crash_point"])
    if current_mult >= crash_point:
        # Too late — already crashed
        session.state["cashed_out_at"] = None
        session.payout_usd = Decimal("0")
        session.profit_usd = -session.wager_usd
        session.status     = HouseGameStatus.BUSTED
    else:
        payout = (session.wager_usd * current_mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        session.state["cashed_out_at"] = str(current_mult)
        session.multiplier = current_mult
        session.payout_usd = payout
        session.profit_usd = payout - session.wager_usd
        session.status     = HouseGameStatus.CASHED_OUT
    return session

def crash_result_text(session: HouseGameSession) -> str:
    s           = session.state
    crash_point = s["crash_point"]
    if session.status == HouseGameStatus.CASHED_OUT:
        return (
            f"🚀 *Crash*\n\n"
            f"Cashed out at: **{s['cashed_out_at']}x**\n"
            f"Crashed at: **{crash_point}x**\n\n"
            f"🏆 *Payout: ${session.payout_usd}*"
        )
    return (
        f"🚀 *Crash*\n\n"
        f"Crashed at: **{crash_point}x** 💥\n\n"
        f"💀 *Lost: ${session.wager_usd}*"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PLINKO
# ══════════════════════════════════════════════════════════════════════════════

# 8-row plinko — 9 slots
PLINKO_MULTIPLIERS = {
    "low":    [Decimal("0.5"), Decimal("1.2"), Decimal("1.4"), Decimal("1.6"), Decimal("2"),
               Decimal("1.6"), Decimal("1.4"), Decimal("1.2"), Decimal("0.5")],
    "medium": [Decimal("0.3"), Decimal("0.6"), Decimal("1"), Decimal("1.5"), Decimal("3"),
               Decimal("1.5"), Decimal("1"), Decimal("0.6"), Decimal("0.3")],
    "high":   [Decimal("0.2"), Decimal("0.3"), Decimal("0.5"), Decimal("1"), Decimal("5"),
               Decimal("1"), Decimal("0.5"), Decimal("0.3"), Decimal("0.2")],
}

PLINKO_ROWS  = 8
PLINKO_EMOJIS = ["⬅️","↖️","⬆️","↗️","➡️"]

def play_plinko(session: HouseGameSession, risk: str = "medium") -> HouseGameSession:
    # Simulate ball path — each row goes left or right
    pos  = 0
    path = []
    for _ in range(PLINKO_ROWS):
        go_right = session.next_float() < 0.5
        if go_right:
            pos += 1
            path.append("➡️")
        else:
            path.append("⬅️")
    mults  = PLINKO_MULTIPLIERS.get(risk, PLINKO_MULTIPLIERS["medium"])
    mult   = mults[min(pos, len(mults)-1)]
    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.state      = {"path": path, "slot": pos, "risk": risk}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def plinko_result_text(session: HouseGameSession) -> str:
    s    = session.state
    won  = session.payout_usd > session.wager_usd
    path = " ".join(s["path"][-4:])  # last 4 moves
    return (
        f"⚡ *Plinko*\n\n"
        f"Risk: **{s['risk'].title()}**\n"
        f"Path: {path}\n"
        f"Slot: **{s['slot'] + 1}** → **{session.multiplier}x**\n\n"
        f"{'🏆 Win! Payout: *$' + str(session.payout_usd) + '*' if won else '💀 Lost: *$' + str(session.wager_usd) + '*' if session.payout_usd == 0 else '↩️ Partial: *$' + str(session.payout_usd) + '*'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MINES
# ══════════════════════════════════════════════════════════════════════════════

MINES_GRID = 25  # 5x5

def start_mines(session: HouseGameSession, num_mines: int = 3) -> HouseGameSession:
    num_mines = max(1, min(24, num_mines))
    # Place mines
    positions = list(range(MINES_GRID))
    mines = set()
    for _ in range(num_mines):
        idx = session.next_int(0, len(positions) - 1)
        mines.add(positions.pop(idx))
    session.state = {
        "mines":     list(mines),
        "num_mines": num_mines,
        "revealed":  [],
        "gems":      0,
        "safe_cells": MINES_GRID - num_mines,
    }
    session.status = HouseGameStatus.ACTIVE
    return session

def _mines_multiplier(gems: int, num_mines: int) -> Decimal:
    """Calculate current multiplier based on gems found."""
    if gems == 0:
        return Decimal("1")
    safe  = MINES_GRID - num_mines
    mult  = Decimal("1")
    for i in range(gems):
        remaining_safe  = safe - i
        remaining_total = MINES_GRID - i
        mult *= Decimal(remaining_total) / Decimal(remaining_safe)
    return (mult * Decimal("0.99")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

def mines_pick(session: HouseGameSession, cell: int) -> HouseGameSession:
    s = session.state
    if cell in s["mines"]:
        # Hit a mine
        s["revealed"].append({"cell": cell, "mine": True})
        session.payout_usd = Decimal("0")
        session.profit_usd = -session.wager_usd
        session.multiplier = Decimal("0")
        session.status     = HouseGameStatus.BUSTED
    else:
        s["revealed"].append({"cell": cell, "mine": False})
        s["gems"] += 1
        mult = _mines_multiplier(s["gems"], s["num_mines"])
        session.multiplier = mult
        # Check if all safe cells revealed
        if s["gems"] >= s["safe_cells"]:
            session = mines_cashout(session)
    return session

def mines_cashout(session: HouseGameSession) -> HouseGameSession:
    payout = (session.wager_usd * session.multiplier).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.CASHED_OUT
    return session

def mines_status_text(session: HouseGameSession) -> str:
    s    = session.state
    grid = ""
    rev  = {r["cell"]: r["mine"] for r in s["revealed"]}
    for i in range(MINES_GRID):
        if i in rev:
            grid += "💣" if rev[i] else "💎"
        else:
            grid += "🟦"
        if (i+1) % 5 == 0:
            grid += "\n"
    status_line = ""
    if session.status == HouseGameStatus.BUSTED:
        status_line = f"\n💥 *Mine hit! Lost: ${session.wager_usd}*"
    elif session.status == HouseGameStatus.CASHED_OUT:
        status_line = f"\n🏆 *Cashed out! Payout: ${session.payout_usd}*"
    else:
        status_line = f"\nGems: {s['gems']} | Multiplier: **{session.multiplier}x** | Next: ~${(session.wager_usd * session.multiplier).quantize(Decimal('0.01'))}*"
    return f"💣 *Mines* ({s['num_mines']} mines)\n\n{grid}{status_line}"


# ══════════════════════════════════════════════════════════════════════════════
#  LIMBO
# ══════════════════════════════════════════════════════════════════════════════

def play_limbo(session: HouseGameSession, target: Decimal) -> HouseGameSession:
    target  = max(Decimal("1.01"), min(Decimal("1000000"), target))
    f       = session.next_float()
    # Generate result using 99/(1-f) formula, house edge 1%
    if f < 0.01:
        result = Decimal("1.00")
    else:
        result = (Decimal("99") / Decimal(str(1 - f))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    won    = result >= target
    mult   = target if won else Decimal("0")
    payout = (session.wager_usd * mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN) if won else Decimal("0")
    session.state      = {"target": str(target), "result": str(result), "won": won}
    session.multiplier = mult
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.COMPLETED
    return session

def limbo_result_text(session: HouseGameSession) -> str:
    s   = session.state
    won = s["won"]
    return (
        f"🌊 *Limbo*\n\n"
        f"Target: **{s['target']}x**\n"
        f"Result: **{s['result']}x**\n\n"
        f"{'🏆 Win! Payout: *$' + str(session.payout_usd) + '*' if won else '💀 Lost: *$' + str(session.wager_usd) + '*'}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TOWER
# ══════════════════════════════════════════════════════════════════════════════

TOWER_CONFIG = {
    "easy":   {"floors": 8, "tiles_per_floor": 3, "safe_per_floor": 2},
    "medium": {"floors": 8, "tiles_per_floor": 3, "safe_per_floor": 1},  # 2 mines, 1 safe
    "hard":   {"floors": 8, "tiles_per_floor": 4, "safe_per_floor": 1},
}

TOWER_MULTIPLIERS = {
    "easy":   [Decimal("1.20"), Decimal("1.44"), Decimal("1.73"), Decimal("2.07"),
               Decimal("2.49"), Decimal("2.99"), Decimal("3.58"), Decimal("4.30")],
    "medium": [Decimal("1.96"), Decimal("3.84"), Decimal("7.53"), Decimal("14.76"),
               Decimal("28.93"), Decimal("56.69"), Decimal("111.11"), Decimal("217.78")],
    "hard":   [Decimal("3.92"), Decimal("15.37"), Decimal("60.26"), Decimal("236.22"),
               Decimal("925.67"), Decimal("3626.54"), Decimal("14207.63"), Decimal("55661.88")],
}

def start_tower(session: HouseGameSession, difficulty: str = "medium") -> HouseGameSession:
    cfg_t   = TOWER_CONFIG.get(difficulty, TOWER_CONFIG["medium"])
    floors  = cfg_t["floors"]
    tiles   = cfg_t["tiles_per_floor"]
    safe    = cfg_t["safe_per_floor"]
    # Pre-generate safe positions for each floor
    floor_safes = []
    for _ in range(floors):
        positions = list(range(tiles))
        safe_pos  = []
        for _ in range(safe):
            idx = session.next_int(0, len(positions) - 1)
            safe_pos.append(positions.pop(idx))
        floor_safes.append(sorted(safe_pos))
    session.state = {
        "difficulty":  difficulty,
        "floors":      floors,
        "tiles":       tiles,
        "floor_safes": floor_safes,
        "current_floor": 0,
        "alive":       True,
    }
    session.status = HouseGameStatus.ACTIVE
    return session

def tower_pick(session: HouseGameSession, tile: int) -> HouseGameSession:
    s     = session.state
    floor = s["current_floor"]
    safe  = s["floor_safes"][floor]
    diff  = s["difficulty"]
    mults = TOWER_MULTIPLIERS.get(diff, TOWER_MULTIPLIERS["medium"])

    if tile in safe:
        s["current_floor"] += 1
        session.multiplier  = mults[floor]
        if s["current_floor"] >= s["floors"]:
            # Reached top
            session = tower_cashout(session)
    else:
        # Mine hit
        session.payout_usd = Decimal("0")
        session.profit_usd = -session.wager_usd
        session.multiplier = Decimal("0")
        s["alive"]         = False
        session.status     = HouseGameStatus.BUSTED
    return session

def tower_cashout(session: HouseGameSession) -> HouseGameSession:
    payout = (session.wager_usd * session.multiplier).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    session.payout_usd = payout
    session.profit_usd = payout - session.wager_usd
    session.status     = HouseGameStatus.CASHED_OUT
    return session

def tower_status_text(session: HouseGameSession) -> str:
    s    = session.state
    diff = s["difficulty"]
    mults = TOWER_MULTIPLIERS.get(diff, TOWER_MULTIPLIERS["medium"])
    floors_display = ""
    for i in range(s["floors"] - 1, -1, -1):
        if i < s["current_floor"]:
            floors_display += f"✅ Floor {i+1} — cleared\n"
        elif i == s["current_floor"] and s.get("alive", True):
            mult = mults[i] if i < len(mults) else "?"
            floors_display += f"👉 Floor {i+1} — {mult}x\n"
        else:
            mult = mults[i] if i < len(mults) else "?"
            floors_display += f"🔒 Floor {i+1} — {mult}x\n"
    status_line = ""
    if session.status == HouseGameStatus.BUSTED:
        status_line = f"\n💥 *Mine hit! Lost: ${session.wager_usd}*"
    elif session.status == HouseGameStatus.CASHED_OUT:
        status_line = f"\n🏆 *Cashed out! Payout: ${session.payout_usd}*"
    else:
        status_line = f"\nCurrent: **{session.multiplier}x** | Cashout: ~**${(session.wager_usd * session.multiplier).quantize(Decimal('0.01'))}**"
    return f"🏗️ *Tower* ({diff.title()})\n\n{floors_display}{status_line}"
