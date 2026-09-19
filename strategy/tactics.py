from __future__ import annotations

import math

from . import *


def vec_from_angle(deg: float, mag: float) -> Vec2:
    rad = math.radians(deg)
    return Vec2(math.cos(rad) * mag, math.sin(rad) * mag)


def heading_to(frm: Vec2, to: Vec2) -> Optional[float]:
    delta = to - frm
    if delta.norm_sq() < 1e-12:
        return None
    return delta.angle_deg()


def err_after_turn(bot: BotState, target_pos: Vec2, turn_speed: float) -> float:
    heading = heading_to(bot.pos, target_pos)
    if heading is None:
        return 0.0
    err = abs(diff_degrees(heading, bot.angle))
    return max(0.0, err - turn_speed)


def disc_between(origin: Vec2, target: Vec2, center: Vec2, radius: float) -> bool:
    if origin.dist(center) >= origin.dist(target):
        return False
    return point_seg_dist(center, origin, target) <= radius


def by_class(fleet) -> tuple:
    extractors, healers, battle = [], [], []
    for bot in fleet:
        if bot.class_ == BotClass.Extractor:
            extractors.append(bot)
        elif bot.class_ == BotClass.Healer:
            healers.append(bot)
        else:
            battle.append(bot)
    return extractors, healers, battle


def closest(origin: Vec2, candidates):
    best = None
    best_d = None
    for bot in candidates:
        d = origin.dist_sq(bot.pos)
        if best_d is None or d < best_d:
            best, best_d = bot, d
    return best


def mining_spot(conf: GameConfig, deposit: Vec2) -> Vec2:
    standoff = min(
        conf.deposit.radius + conf.bot.radius + 1.25,
        conf.bot.base_extract_range * 0.7,
    )
    # Prefer our wall / spawn side so the miner is not standing in the lane.
    for deg in (90.0, 135.0, 45.0, 180.0, 0.0, 225.0, 315.0):
        spot = deposit + vec_from_angle(deg, standoff)
        if point_free(spot) and line_of_sight(spot, deposit):
            return spot
    return deposit + Vec2(0.0, conf.deposit.radius + conf.bot.radius)


def home_threats(state: GameState, conf: GameConfig, enemies):
    radius = conf.bot.blaster_range * 1.5
    deposit = state.deposit_me.pos
    return [e for e in enemies if e.pos.dist(deposit) <= radius]


def pick_focus(enemies, state: GameState):
    if not enemies:
        return None
    live = [e for e in enemies if e.invulnerable_until_tick <= state.tick] or enemies
    miners = [e for e in live if e.class_ == BotClass.Extractor]
    pool = miners or live
    return min(pool, key=lambda e: e.health)


def shot_clear(bot: BotState, target: Vec2, state: GameState, conf: GameConfig) -> bool:
    if disc_between(bot.pos, target, state.payload_pos(), conf.payload.radius):
        return False
    if disc_between(bot.pos, target, state.deposit_me.pos, conf.deposit.radius):
        return False
    if disc_between(bot.pos, target, state.deposit_other.pos, conf.deposit.radius):
        return False
    return line_of_sight(bot.pos, target)


def can_blast(bot: BotState, target: Vec2, state: GameState, conf: GameConfig) -> bool:
    if bot.next_fire_tick > state.tick:
        return False
    dist = bot.pos.dist(target)
    if dist > conf.bot.blaster_range:
        return False
    hull = conf.bot.radius + conf.bot.base_blaster_splash_radius
    half = math.degrees(math.atan2(hull, max(dist, hull)))
    if err_after_turn(bot, target, conf.bot.turn_speed) > max(half, 1.5):
        return False
    return shot_clear(bot, target, state, conf)


def can_heal(healer: BotState, ally: BotState, conf: GameConfig) -> bool:
    if healer.id == ally.id:
        return False
    if healer.pos.dist(ally.pos) > conf.bot.base_heal_range:
        return False
    half = conf.bot.base_heal_arc_deg * 0.5
    if err_after_turn(healer, ally.pos, conf.bot.turn_speed) > half:
        return False
    return line_of_sight(healer.pos, ally.pos)


def move_towards(bot_action, frm: Vec2, to: Vec2) -> None:
    bot_action.move_action = move_bot(navigate_to(frm, to))


def act_extractor(bot, bot_action, spot: Vec2, deposit: Vec2, flee_to: Vec2, threatened: bool) -> None:
    dest = flee_to if threatened else spot
    move_towards(bot_action, bot.pos, dest)
    bot_action.turn_action = turn_towards(deposit)
    bot_action.special_action = SpecialAction.Extractor(mine=not threatened)


def act_healer(bot, bot_action, ally: Optional[BotState], fallback: Vec2, conf: GameConfig) -> None:
    if ally is None:
        move_towards(bot_action, bot.pos, fallback)
        return
    # Sit just inside heal range so we keep facing them.
    away = bot.pos - ally.pos
    if away.norm_sq() < 1e-8:
        dest = ally.pos
    else:
        dest = ally.pos + away.normalize_or_zero() * (conf.bot.base_heal_range * 0.6)
    move_towards(bot_action, bot.pos, dest)
    bot_action.turn_action = turn_towards(ally.pos)
    bot_action.special_action = SpecialAction.Healer(
        fire=can_heal(bot, ally, conf),
        target=ally.id,
    )


def act_battle(
    bot,
    bot_action,
    dest: Vec2,
    shoot: Optional[BotState],
    state: GameState,
    conf: GameConfig,
) -> None:
    if shoot is not None:
        spread = vec_from_angle(bot.id * 47.0, conf.bot.radius * 4.0)
        dest = dest + spread
    move_towards(bot_action, bot.pos, dest)
    if shoot is not None:
        bot_action.turn_action = turn_towards(shoot.pos)
        bot_action.special_action = SpecialAction.Battle(
            fire=can_blast(bot, shoot.pos, state, conf)
        )
    else:
        opp_heading = heading_to(bot.pos, dest)
        if opp_heading is not None:
            bot_action.turn_action = turn_to_angle(opp_heading)
