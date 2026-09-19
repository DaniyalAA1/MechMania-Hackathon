from . import *
from .tactics import (
    act_battle,
    act_extractor,
    act_healer,
    by_class,
    closest,
    home_threats,
    mining_spot,
    pick_focus,
)


class Brain:
    """Default jobs plus three interrupts. Panic sticks until capture recovers."""

    def __init__(self) -> None:
        self.panic = False
        self.heal_lock = {}

    def __call__(self, state: GameState) -> FleetAction:
        conf = get_config()
        action = FleetAction.new()

        payload = state.payload_pos()
        deposit = state.deposit_me.pos
        enemies = [e for e in state.fleet_other]
        extractors, healers, battle = by_class(state.fleet_me)

        endgame_start = conf.max_ticks - conf.endgame_ticks
        in_endgame = state.tick >= endgame_start
        threats = home_threats(state, conf, enemies)
        focus = pick_focus(enemies, state)
        capture = state.capture

        if capture <= -0.20:
            self.panic = True
        elif capture >= -0.08:
            self.panic = False

        hard = in_endgame or capture <= -0.50
        mild = hard or self.panic
        greed = (
            not in_endgame
            and not mild
            and not threats
            and capture >= -0.05
            and len(battle) >= 4
        )

        want_extractors = 2 if greed else 1
        if in_endgame:
            want_extractors = 0
        want_healers = 0
        if len(battle) >= 3:
            want_healers = 1
        if len(battle) >= 8:
            want_healers = 2

        n_ext, n_heal = len(extractors), len(healers)
        if n_ext < want_extractors:
            next_bot = BotClass.Extractor
        elif n_heal < want_healers:
            next_bot = BotClass.Healer
        else:
            next_bot = BotClass.Battle
        action.fabricator_next = int(next_bot)

        # Spend every rush we can. Only skip if a free bot is due on the last
        # pre-endgame tick -- rushing then would pay for one body and delete the free one.
        natural_due = state.fabricator_me.next_bot_creation <= state.tick
        last_build_tick = state.tick + 1 >= endgame_start
        action.rush_order = (
            not in_endgame
            and not state.fleet_me.is_full()
            and state.fabricator_me.tokens >= conf.fabricator.rush_cost
            and not (natural_due and last_build_tick)
        )

        spot = mining_spot(conf, deposit)
        flee_to = battle[0].pos if battle else payload
        sitter = closest(payload, battle) if battle else None
        sitter_id = sitter.id if sitter is not None and not mild else None

        heal_counts = {}

        def healer_target(healer: BotState) -> Optional[BotState]:
            cap = int(conf.bot.heal_stack_cap)
            locked_id = self.heal_lock.get(healer.id)
            locked = state.fleet_me.get(locked_id) if locked_id is not None else None

            wounded = []
            for ally in state.fleet_me:
                if ally.id == healer.id:
                    continue
                if ally.health >= conf.bot.health * 0.98:
                    continue
                if heal_counts.get(ally.id, 0) >= cap:
                    continue
                wounded.append(ally)

            target = None
            if locked is not None and locked.health < conf.bot.health * 0.98:
                if heal_counts.get(locked.id, 0) < cap:
                    worst = min(wounded, key=lambda a: a.health) if wounded else None
                    # Stick unless someone is clearly more hurt.
                    if worst is None or worst.health >= locked.health - 2.0:
                        target = locked
            if target is None and wounded:
                target = min(wounded, key=lambda a: a.health)
            if target is None:
                if locked is not None:
                    target = locked
                else:
                    target = closest(healer.pos, [b for b in battle if b.id != healer.id])

            if target is not None:
                self.heal_lock[healer.id] = target.id
                heal_counts[target.id] = heal_counts.get(target.id, 0) + 1
            else:
                self.heal_lock.pop(healer.id, None)
            return target

        for bot in state.fleet_me:
            bot_action = action.bots[bot.id]
            opp = closest(bot.pos, enemies)

            if bot.class_ == BotClass.Extractor:
                if hard:
                    bot_action.move_action = move_bot(navigate_to(bot.pos, payload))
                    bot_action.turn_action = turn_towards(payload)
                    bot_action.special_action = SpecialAction.Extractor(mine=False)
                else:
                    threatened = any(
                        e.pos.dist(bot.pos) <= conf.bot.blaster_range for e in enemies
                    )
                    act_extractor(bot, bot_action, spot, deposit, flee_to, threatened)
                continue

            if bot.class_ == BotClass.Healer:
                dest = payload if (mild or in_endgame) else (sitter.pos if sitter else payload)
                act_healer(bot, bot_action, healer_target(bot), dest, conf)
                continue

            # Battle
            shoot = None
            if hard or mild:
                dest = payload
                shoot = opp
            elif sitter_id is not None and bot.id == sitter_id:
                dest = payload
                shoot = opp
            elif threats:
                dest = closest(bot.pos, threats).pos
                shoot = closest(bot.pos, threats)
            elif focus is not None:
                dest = focus.pos
                shoot = focus
            else:
                dest = payload
                shoot = opp
            act_battle(bot, bot_action, dest, shoot, state, conf)

        return action


def get_strategy(team: int) -> Strategy:
    return Brain()
