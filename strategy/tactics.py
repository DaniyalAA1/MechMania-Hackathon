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


def mining_spot(conf: GameConfig, deposit: Vec2, index: int = 0, count: int = 3) -> Vec2:
    """Equal-angle stand-off around the deposit. Each miner gets its own heading."""
    count = max(int(count), 1)
    index = index % count
    sep = splash_cluster_range(conf) + 0.25
    min_r = conf.deposit.radius + conf.bot.radius + 0.2
    if count == 1:
        need = min_r
    else:
        need = sep / (2.0 * math.sin(math.pi / count))
    radius = min(max(min_r, need), conf.bot.base_extract_range * 0.8)
    deg = 90.0 + 360.0 * index / count
    for scale in (1.0, 0.85, 1.12, 0.7, 1.25):
        r = radius * scale
        if r < min_r or r > conf.bot.base_extract_range * 0.92:
            continue
        spot = deposit + vec_from_angle(deg, r)
        if point_free(spot) and line_of_sight(spot, deposit):
            return spot
    return deposit + vec_from_angle(deg, min_r)


def home_threats(state: GameState, conf: GameConfig, enemies):
    # Only peel if they are actually at our node, not halfway across the map.
    radius = conf.bot.blaster_range + 3.0
    deposit = state.deposit_me.pos
    return [e for e in enemies if e.pos.dist(deposit) <= radius]


def splash_cluster_range(conf: GameConfig) -> float:
    # Hull-to-hull: a blast on A still tags B if they are this close.
    return 2.0 * conf.bot.radius + conf.bot.base_blaster_splash_radius + 0.08


def _ring_point(center: Vec2, deg: float, radius: float) -> Vec2:
    slot = center + vec_from_angle(deg, radius)
    if point_free(slot):
        return slot
    tighter = center + vec_from_angle(deg, radius * 0.85)
    if point_free(tighter):
        return tighter
    return slot


def formation_slot(center: Vec2, index: int, count: int, conf: GameConfig) -> Vec2:
    """Rings around `center`, spaced so one splash cannot chain us. Still in capture."""
    if count <= 0:
        return center
    sep = splash_cluster_range(conf) + 0.22
    max_r = conf.payload.capture_radius - conf.bot.radius - 0.15
    min_r = conf.payload.radius + conf.bot.radius + 0.25

    rings = []
    remaining = count
    radius = max_r
    while remaining > 0 and radius >= min_r:
        n = max(1, int((2.0 * math.pi * radius) / sep))
        n = min(n, remaining)
        rings.append((radius, n))
        remaining -= n
        radius -= sep
    if remaining > 0:
        if rings:
            r0, n0 = rings[-1]
            rings[-1] = (r0, n0 + remaining)
        else:
            rings.append((min_r, count))

    i = index % count
    for radius, n in rings:
        if i < n:
            return _ring_point(center, 360.0 * i / n, radius)
        i -= n
    return center


def assign_splash_shots(shooters, enemies, state: GameState, conf: GameConfig):
    """One gun per clump per tick. Extra shots on the same pile are wasted to invuln."""
    cluster_r = splash_cluster_range(conf)
    cluster_r_sq = cluster_r * cluster_r
    claimed = set()
    shots = {}
    payload = state.payload_pos()
    ready = sorted(
        shooters,
        key=lambda b: (b.next_fire_tick > state.tick, b.pos.dist_sq(payload)),
    )
    for bot in ready:
        target = None
        best = None
        for enemy in enemies:
            if enemy.invulnerable_until_tick > state.tick or enemy.id in claimed:
                continue
            clump = 1
            for other in enemies:
                if other.id != enemy.id and enemy.pos.dist_sq(other.pos) <= cluster_r_sq:
                    clump += 1
            # Anyone standing on the cart first: one contester freezes the whole push, so
            # clearing them is what gets it moving. Then bigger clump, closer, lower HP.
            key = (enemy.pos.dist_sq(payload) <= conf.payload.capture_radius ** 2,
                   clump, -bot.pos.dist_sq(enemy.pos), -enemy.health)
            if best is None or key > best:
                best = key
                target = enemy
        # Only a gun that can actually fire at its pick this tick reserves the clump; one that
        # is still turning or reloading just aims, and leaves the clump to a gun that can.
        # Reserving on behalf of a shot that never goes off silences the whole fleet: against a
        # tight enemy blob two such holds used to cover everyone, and we were outshot 2:1.
        fire = target is not None and can_blast(bot, target.pos, state, conf)
        if target is None:
            target = closest(bot.pos, enemies)
        elif fire:
            claimed.add(target.id)
            for other in enemies:
                if target.pos.dist_sq(other.pos) <= cluster_r_sq:
                    claimed.add(other.id)
        shots[bot.id] = (target, fire)
    return shots


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
    dest = fallback
    if ally is not None and bot.pos.dist(ally.pos) > conf.bot.base_heal_range * 0.8:
        away = bot.pos - ally.pos
        if away.norm_sq() < 1e-8:
            dest = fallback
        else:
            dest = ally.pos + away.normalize_or_zero() * (conf.bot.base_heal_range * 0.55)
    move_towards(bot_action, bot.pos, dest)
    if ally is not None:
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
    fire: bool = True,
) -> None:
    move_towards(bot_action, bot.pos, dest)
    if shoot is not None:
        bot_action.turn_action = turn_towards(shoot.pos)
        ready = fire and shoot.invulnerable_until_tick <= state.tick
        bot_action.special_action = SpecialAction.Battle(
            fire=ready and can_blast(bot, shoot.pos, state, conf)
        )
    else:
        opp_heading = heading_to(bot.pos, dest)
        if opp_heading is not None:
            bot_action.turn_action = turn_to_angle(opp_heading)
