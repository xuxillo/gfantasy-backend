# scoring_engine.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from fantasy_config import (
    SCORING_BASE,
    POSITION_ROLES,
    CAPTAIN_BONUS,
    POSITION_BONUSES,
    BONUS_BLOCK_SIZES,
    BONUS_STAT_ATTRS,
)


@dataclass
class PlayerMatchStats:
    """Raw stats from SofaScore for a single player in a single match."""
    player_id: int
    position: str  # POR, DEF, MED, DEL
    role: Optional[str]
    minutes: int = 0
    goals: int = 0
    assists: int = 0
    shots_on_target: int = 0
    shots_off_target: int = 0
    key_passes: int = 0
    dribbles_completed: int = 0
    aerials_won: int = 0
    interceptions: int = 0
    clearances: int = 0
    tackles_won: int = 0
    fouls_committed: int = 0
    fouls_suffered: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    second_yellow: bool = False
    penalties_scored: int = 0
    penalties_missed: int = 0
    penalties_conceded: int = 0
    penalties_saved: int = 0
    clean_sheet: bool = False
    own_goals: int = 0
    goals_conceded: int = 0
    saves: int = 0
    ball_recovery: int = 0
    blocked_scoring_attempt: int = 0
    rating: float = 0.0
    is_starter: bool = True
    is_captain: bool = False


@dataclass
class ScoringParams:
    """Parámetros de puntuación (Canon §4.2 y §4.3)."""
    goal: dict[str, int]
    assist: dict[str, int]
    penalty_scored: int
    penalty_missed: int
    penalty_conceded: int
    yellow_card: int
    second_yellow: int
    red_card: int
    clean_sheet: dict[str, int]
    penalty_saved: int
    own_goal: int
    goals_conceded: dict[str, int]
    fouls_committed_per_3: int
    captain_bonus: int = 2
    position_bonuses: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def from_base(
        cls,
        base: Optional[dict] = None,
        position_bonuses: Optional[dict] = None,
    ) -> "ScoringParams":
        base = base if base is not None else SCORING_BASE
        return cls(
            goal=base.get("goal", {"DEL": 4, "MED": 5, "DEF": 6, "POR": 7}),
            assist=base.get("assist", {"DEL": 3, "MED": 4, "DEF": 5, "POR": 6}),
            penalty_scored=base.get("penalty_scored", 3),
            penalty_missed=base.get("penalty_missed", -2),
            penalty_conceded=base.get("penalty_conceded", -2),
            yellow_card=base.get("yellow", -2),
            second_yellow=base.get("double_yellow", -4),
            red_card=base.get("red", -6),
            clean_sheet=base.get("clean_sheet", {"POR": 2, "DEF": 2, "MED": 0, "DEL": 0}),
            penalty_saved=base.get("penalty_saved", 4),
            own_goal=base.get("own_goal", -6),
            goals_conceded=base.get("goals_conceded", {"POR": 0, "DEF": 0, "MED": 0, "DEL": 0}),
            fouls_committed_per_3=base.get("fouls_committed_per_3", -1),
            captain_bonus=base.get("captain_bonus", CAPTAIN_BONUS),
            position_bonuses=position_bonuses if position_bonuses is not None else POSITION_BONUSES,
        )

    @classmethod
    def default(cls) -> "ScoringParams":
        return cls.from_base(SCORING_BASE, POSITION_BONUSES)


def calculate_points(stats: PlayerMatchStats, params: Optional[ScoringParams] = None) -> int:
    """Calcula los puntos fantasy de un jugador en un partido."""
    if params is None:
        params = ScoringParams.default()

    if stats.minutes == 0:
        return 0

    position = stats.position
    points = 0

    # --- Eventos universales (Canon §4.2) ---
    points += stats.goals * params.goal.get(position, 0)
    points += stats.assists * params.assist.get(position, 0)
    points += stats.penalties_scored * params.penalty_scored
    points += stats.penalties_missed * params.penalty_missed
    points += stats.penalties_conceded * params.penalty_conceded
    points += stats.yellow_cards * params.yellow_card
    if stats.second_yellow:
        points += params.second_yellow
    points += stats.red_cards * params.red_card
    points += stats.own_goals * params.own_goal
    points += stats.penalties_saved * params.penalty_saved

    # Portería a cero: requiere al menos 60 minutos (Canon §4.3)
    if stats.clean_sheet and stats.minutes >= 60:
        points += params.clean_sheet.get(position, 0)

    if stats.goals_conceded > 0:
        points += stats.goals_conceded * params.goals_conceded.get(position, 0)

    if stats.fouls_committed >= 3:
        points += (stats.fouls_committed // 3) * params.fouls_committed_per_3

    # --- Bonus por posición/rol (Canon §4.3) ---
    bonuses = params.position_bonuses.get(position, {})
    for key, weight in bonuses.items():
        if not weight:
            continue
        attr = BONUS_STAT_ATTRS.get(key)
        if not attr:
            continue
        amount = getattr(stats, attr, 0) or 0
        if key in BONUS_BLOCK_SIZES:
            block = BONUS_BLOCK_SIZES[key]
            if amount >= block:
                points += (amount // block) * weight
        else:
            points += amount * weight

    # --- Capitán (Canon §3): +2 puntos ---
    if stats.is_captain:
        points += params.captain_bonus

    return max(points, -20)


def simulate_league_scoring(
    all_match_stats: list,
    params: Optional[ScoringParams] = None,
) -> dict[int, list[tuple[int, int]]]:
    """Simula la puntuación de un conjunto de jugadores."""
    if params is None:
        params = ScoringParams.default()

    results: dict[int, list[tuple[int, int]]] = {}
    for item in all_match_stats:
        if isinstance(item, tuple):
            matchday, stats = item
        else:
            stats = item
            matchday = getattr(stats, "matchday", 0)
        pts = calculate_points(stats, params)
        results.setdefault(matchday, []).append((stats.player_id, pts))
    return results