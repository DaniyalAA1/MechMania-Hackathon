from . import *


def get_strategy(team: int) -> Strategy:
    # Same strategy on both sides: the engine mirrors the world for team 1.
    return smart_strategy


def smart_strategy(state: GameState) -> FleetAction:
    """
    Plan:
      - Slot 0 is the extractor; it mines from its own edge of the deposit.
      - If the extractor dies, the fabricator rebuilds it first.
      - Battle bots split into roles:
          * defenders: engage any enemy that gets near our deposit
          * one payload contester: pushes the payload when the coast is clear
          * hunters: focus the enemy closest to them
      - Only fire when in range AND with a clear line of sight.
      - Spend tokens on rush orders whenever affordable (not in the endgame).
    """
    conf = get_config()
    action = FleetAction.new()

    payload = state.payload_pos()
    deposit = state.deposit_me.pos
    mining_spot = deposit + Vec2(0.0, conf.deposit.radius + conf.bot.radius)

    enemies = [e for e in state.fleet_other]

    # Threat = enemies close to our deposit (protect the extractor).
    guard_radius = conf.bot.blaster_range * 2.0
    threats = [e for e in enemies if e.pos.dist(deposit) <= guard_radius]

    # ---- Fabricator: replace the extractor first, otherwise build battle bots.
    next_bot = BotClass.Battle
    if not state.fleet_me.get(0):
        next_bot = BotClass.Extractor
    action.fabricator_next = int(next_bot)

    def closest(bot_pos, candidates):
        best = None
        best_d = None
        for e in candidates:
            d = bot_pos.dist_sq(e.pos)
            if best_d is None or d < best_d:
                best, best_d = e, d
        return best

    assigned_contester = False

    for bot in state.fleet_me:
        bot_action = action.bots[bot.id]

        # ---- Extractor: mine, and nothing else.
        if bot.class_ == BotClass.Extractor:
            bot_action.move_action = move_bot(navigate_to(bot.pos, mining_spot))
            bot_action.turn_action = turn_towards(deposit)
            bot_action.special_action = SpecialAction.Extractor(mine=True)
            continue

        # ---- Battle bots.
        # Pick a target: threats near home first, otherwise the nearest enemy.
        target = closest(bot.pos, threats) if threats else None

        # One bot contests the payload when nothing threatens home.
        if target is None and not assigned_contester:
            assigned_contester = True
            bot_action.move_action = move_bot(navigate_to(bot.pos, payload))
            # Still shoot opportunistically at anything in range on the way.
            opp = closest(bot.pos, enemies)
            if opp is not None:
                bot_action.turn_action = turn_towards(opp.pos)
                can_hit = (bot.pos.dist(opp.pos) <= conf.bot.blaster_range
                           and line_of_sight(bot.pos, opp.pos))
                bot_action.special_action = SpecialAction.Battle(fire=can_hit)
            continue

        if target is None:
            target = closest(bot.pos, enemies)

        if target is None:
            # No enemies left/alive: help push the payload.
            bot_action.move_action = move_bot(navigate_to(bot.pos, payload))
            continue

        tpos = target.pos
        bot_action.move_action = move_bot(navigate_to(bot.pos, tpos))
        bot_action.turn_action = turn_towards(tpos)

        in_range = (bot.pos.dist(tpos) <= conf.bot.blaster_range
                    and line_of_sight(bot.pos, tpos))
        bot_action.special_action = SpecialAction.Battle(fire=in_range)

    # ---- Rush orders: only useful before the endgame.
    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks
    action.rush_order = (not in_endgame
                         and state.fabricator_me.tokens >= conf.fabricator.rush_cost)

    return action
