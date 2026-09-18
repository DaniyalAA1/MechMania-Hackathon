"""Our MechMania 32 bot.

Start at TUNING. Those constants are the whole strategy in miniature -- you can move
this bot a long way without reading a line below them.

How a tick flows:

    play()              one tick, start to finish
      assign_roles()    decide who mines, who heals, who fights, who escorts
      act_miner()       \
      act_healer()       > what a single bot does, given its role
      act_fighter()     /
      choose_build()    what the fabricator makes next
      should_rush()     whether to spend tokens hurrying it along

Everything is split that way so you can change one decision without touching the
others. If the bot is losing fights, you only need to read act_fighter.
"""

from . import *

# ===================================================================================
# TUNING
# ===================================================================================
# These are the knobs. Change one, run a match, see what happens, change it back if it
# was worse. That loop IS the strategy work -- the code below is just what makes the
# loop possible.

# How many bots should be mining the deposit at once. More tokens, fewer guns.
EXTRACTORS_WANTED = 1

# How many healers to keep. 0 disables healers entirely.
HEALERS_WANTED = 1

# Do not build the first healer until we have at least this many bots -- a healer with
# nobody to heal is a wasted build slot early on.
HEALER_AFTER_N_BOTS = 3

# How many bots sit on the payload instead of hunting. The payload is how you win, so
# this should probably never be 0.
ESCORTS_WANTED = 1

# Stand back from the deposit instead of hugging it. An extractor mines anything within
# conf.bot.base_extract_range that it has a sightline to, so there is no reason to be
# hull to hull with the ring where everyone can shoot you. 1.0 = hugging the ring,
# higher = further back. Raise this if our extractors keep dying.
MINING_STANDOFF = 1.6

# When our blaster is reloading and the enemy is closer than this fraction of our
# blaster range, back off instead of standing there eating damage. 0.0 disables kiting.
KITE_RANGE_FRACTION = 0.8

# Focus fire: when picking which enemy the whole fleet shoots, weigh "already hurt"
# against "close by". Higher = care more about finishing wounded targets, lower = care
# more about shooting whatever is nearest.
FOCUS_HEALTH_WEIGHT = 0.5

# Heal an ally once it drops below this fraction of full health.
HEAL_BELOW_FRACTION = 0.9

# If our compute budget drops under this many ticks, fall back to the cheap plan rather
# than risk an overspend. An overspend is a debt repaid in ticks we do not get to act.
BUDGET_FLOOR = 4

# ===================================================================================
# roles
# ===================================================================================

MINER = "miner"
HEALER = "healer"
ESCORT = "escort"
FIGHTER = "fighter"


def get_strategy(team: int) -> Strategy:
    """The engine mirrors the world for the top-right team, so to us we are always
    bottom-left. There is nothing for a side to specialise in -- both teams get the
    same brain."""
    return play


# ===================================================================================
# one tick
# ===================================================================================

def play(state: GameState) -> FleetAction:
    conf = get_config()
    action = FleetAction.new()

    # No bot is built in the last conf.endgame_ticks, so from that point tokens buy
    # nothing and a bot standing at the deposit is a bot not contesting the payload.
    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks

    # Our compute allowance, in engine ticks. Nothing below is expensive (no path_length
    # or route_waypoints calls -- navigate_to is one step and cheap), so this should not
    # trip. It is here so the guard already exists when you add a real search.
    tight_budget = get_budget().remaining < BUDGET_FLOOR

    roles = assign_roles(state, conf, in_endgame)

    fighters = [b for b in state.fleet_me if roles.get(b.id) in (FIGHTER, ESCORT)]

    if tight_budget:
        target = nearest_enemy(state, fighters)
    else:
        target = choose_focus_target(state, fighters)

    # Somewhere safe to back off to when reloading. Our own deposit is behind our lines
    # and navigate_to routes around walls to get there.
    retreat_point = state.deposit_me.pos

    for bot in state.fleet_me:
        bot_action = action.bots[bot.id]
        role = roles.get(bot.id, FIGHTER)

        if role == MINER:
            act_miner(bot, bot_action, state, conf)
        elif role == HEALER:
            act_healer(bot, bot_action, state, conf, target)
        elif role == ESCORT:
            act_escort(bot, bot_action, state, conf, target, retreat_point)
        else:
            act_fighter(bot, bot_action, state, conf, target, retreat_point)

    action.fabricator_next = int(choose_build(state, conf, roles, in_endgame))
    action.rush_order = should_rush(state, conf, in_endgame)
    return action


# ===================================================================================
# who does what
# ===================================================================================

def assign_roles(state: GameState, conf: GameConfig, in_endgame: bool) -> dict:
    """Hand every living bot a job.

    We assign mostly by the class a bot was BUILT as, because its stats are presumably
    tuned for that class. But note that SpecialAction is what decides the class a bot
    ACTS as on any given tick -- so a bot can legally mine when safe and shoot when
    threatened. That is probably the cleverest thing this API allows and we are not
    using it yet. Worth an experiment once you can watch replays.
    """
    roles = {}

    # In the endgame there is nothing to spend tokens on, so mining is dead weight.
    miners_wanted = 0 if in_endgame else EXTRACTORS_WANTED

    miners = 0
    healers = 0
    escorts = 0

    # First pass: honour what each bot was built to be.
    leftovers = []
    for bot in state.fleet_me:
        if bot.class_ == BotClass.Extractor and miners < miners_wanted:
            roles[bot.id] = MINER
            miners += 1
        elif bot.class_ == BotClass.Healer and healers < HEALERS_WANTED:
            roles[bot.id] = HEALER
            healers += 1
        else:
            leftovers.append(bot)

    # Second pass: everyone else escorts the payload, then fights.
    for bot in leftovers:
        if escorts < ESCORTS_WANTED:
            roles[bot.id] = ESCORT
            escorts += 1
        else:
            roles[bot.id] = FIGHTER

    # In the endgame the payload is the only thing that matters -- everybody goes.
    if in_endgame:
        for bot in state.fleet_me:
            if roles.get(bot.id) != HEALER:
                roles[bot.id] = ESCORT

    return roles


# ===================================================================================
# what one bot does
# ===================================================================================

def act_miner(bot: BotState, bot_action: BotAction, state: GameState, conf: GameConfig):
    """Sit off the deposit ring and mine it."""
    # The deposit is a solid disc, so dead-center is inside a wall. +y is the side away
    # from the map center on our half. MINING_STANDOFF pushes us further back than the
    # hull-to-hull minimum.
    offset = (conf.deposit.radius + conf.bot.radius) * MINING_STANDOFF
    mining_spot = state.deposit_me.pos + Vec2(0.0, offset)

    bot_action.move_action = move_bot(navigate_to(bot.pos, mining_spot))
    bot_action.turn_action = turn_towards(state.deposit_me.pos)
    bot_action.special_action = SpecialAction.Extractor(mine=True)


def act_healer(bot: BotState, bot_action: BotAction, state: GameState,
               conf: GameConfig, enemy: Optional[BotState]):
    """Follow the most hurt ally and top it up."""
    patient = most_hurt_ally(bot, state, conf)

    if patient is not None:
        bot_action.move_action = move_bot(navigate_to(bot.pos, patient.pos))
        bot_action.turn_action = turn_towards(patient.pos)
        # NOTE `target` here is the ally we are healing. If the engine rejects this or
        # heals the wrong thing, `target` is probably a Vec2 or a BotState rather than
        # an id -- this is the single most likely line in the file to need a fix on the
        # first real run. Check core/channel.py.
        bot_action.special_action = SpecialAction.Healer(fire=False, target=patient.id)
        return

    # Nobody needs healing -- tuck in behind the fleet near the payload.
    bot_action.move_action = move_bot(navigate_to(bot.pos, state.payload_pos()))
    if enemy is not None:
        bot_action.turn_action = turn_towards(enemy.pos)


def act_escort(bot: BotState, bot_action: BotAction, state: GameState,
               conf: GameConfig, enemy: Optional[BotState], retreat_point: Vec2):
    """Hold the payload. Shoot anything that comes to us, but do not chase it."""
    payload = state.payload_pos()
    bot_action.move_action = move_bot(navigate_to(bot.pos, payload))

    if enemy is None:
        return

    # Only shoot when the shot can land. A shot puts the blaster on cooldown whether or
    # not it hits, so firing at a wall costs us the next real one.
    in_range = (bot.pos.dist(enemy.pos) <= conf.bot.blaster_range
                and line_of_sight(bot.pos, enemy.pos))
    bot_action.turn_action = turn_towards(enemy.pos)
    bot_action.special_action = SpecialAction.Battle(fire=in_range)


def act_fighter(bot: BotState, bot_action: BotAction, state: GameState,
                conf: GameConfig, enemy: Optional[BotState], retreat_point: Vec2):
    """Hunt the fleet's focus target, and back off while reloading."""
    if enemy is None:
        # No enemies on the board -- go be useful on the payload.
        bot_action.move_action = move_bot(navigate_to(bot.pos, state.payload_pos()))
        return

    dist = bot.pos.dist(enemy.pos)
    in_range = dist <= conf.bot.blaster_range and line_of_sight(bot.pos, enemy.pos)
    reloading = bot.next_fire_tick > state.tick

    # Always face the target: turning takes ticks, so start now even while closing.
    bot_action.turn_action = turn_towards(enemy.pos)

    if reloading and dist < conf.bot.blaster_range * KITE_RANGE_FRACTION:
        # Our gun is empty and they are close. Standing here just donates health.
        bot_action.move_action = move_bot(navigate_to(bot.pos, retreat_point))
    elif not in_range:
        bot_action.move_action = move_bot(navigate_to(bot.pos, enemy.pos))
    # else: in range and loaded -- hold position and shoot. No move_action set.

    bot_action.special_action = SpecialAction.Battle(fire=in_range)


# ===================================================================================
# picking targets
# ===================================================================================

def choose_focus_target(state: GameState, fighters: List[BotState]) -> Optional[BotState]:
    """One target for the whole fleet.

    The starter had every bot pick its own nearest enemy, which spreads damage across
    the enemy fleet and kills nothing. Concentrating fire removes enemy guns from the
    board, and a dead bot deals no damage for the rest of the match.
    """
    if not fighters:
        return None

    best = None
    best_score = None
    for enemy in state.fleet_other:
        if enemy.invulnerable_until_tick > state.tick:
            continue

        nearest = min(f.pos.dist(enemy.pos) for f in fighters)
        score = enemy.health * FOCUS_HEALTH_WEIGHT + nearest

        if best_score is None or score < best_score:
            best = enemy
            best_score = score

    return best


def nearest_enemy(state: GameState, fighters: List[BotState]) -> Optional[BotState]:
    """The cheap version of the above, for when the compute budget is tight."""
    if not fighters:
        return None
    anchor = fighters[0].pos
    best = None
    for enemy in state.fleet_other:
        if best is None or anchor.dist_sq(enemy.pos) < anchor.dist_sq(best.pos):
            best = enemy
    return best


def most_hurt_ally(healer: BotState, state: GameState,
                   conf: GameConfig) -> Optional[BotState]:
    """The ally in the worst shape, or None if everyone is healthy enough."""
    threshold = conf.bot.health * HEAL_BELOW_FRACTION
    worst = None
    for ally in state.fleet_me:
        if ally.id == healer.id:
            continue
        if ally.health >= threshold:
            continue
        if worst is None or ally.health < worst.health:
            worst = ally
    return worst


# ===================================================================================
# the fabricator
# ===================================================================================

def choose_build(state: GameState, conf: GameConfig, roles: dict,
                 in_endgame: bool) -> BotClass:
    """What to build next.

    This is the biggest single lever in the game and it is currently three if-statements
    driven by the constants at the top of the file. Economy versus army is the whole
    question: every extractor is a gun you do not have, and every battle bot is income
    you are not earning.
    """
    fleet_size = len(state.fleet_me)

    miners = sum(1 for r in roles.values() if r == MINER)
    healers = sum(1 for r in roles.values() if r == HEALER)

    # Replace a dead extractor before anything else -- income compounds, so an
    # extractor lost early costs far more than one lost late.
    if not in_endgame and miners < EXTRACTORS_WANTED:
        return BotClass.Extractor

    if healers < HEALERS_WANTED and fleet_size >= HEALER_AFTER_N_BOTS:
        return BotClass.Healer

    return BotClass.Battle


def should_rush(state: GameState, conf: GameConfig, in_endgame: bool) -> bool:
    """Rush orders are the only thing tokens buy, so unspent tokens at the final whistle
    are pure waste. But no bot is built in the last conf.endgame_ticks, so a rush bought
    then buys nothing."""
    if in_endgame:
        return False
    return state.fabricator_me.tokens >= conf.fabricator.rush_cost
