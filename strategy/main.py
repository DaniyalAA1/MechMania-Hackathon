from . import *
from .tactics import (
    act_battle,
    act_extractor,
    act_healer,
    assign_splash_shots,
    by_class,
    closest,
    formation_slot,
    home_threats,
    mining_spot,
)


class Brain:
    """Blob the payload. Three miners. Peel a few home only if the node is actually dived."""

    def __init__(self) -> None:
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

        n_ext, n_heal, n_battle = len(extractors), len(healers), len(battle)
        want_extractors = 0 if in_endgame else 3
        # One healer cancels one gun. Two is enough in the clump; a third is a fighter we didn't build.
        want_healers = 0
        if n_battle >= 6:
            want_healers = 1
        if n_battle >= 12:
            want_healers = 2

        # Get a fighting blob on the road before parking the 2nd/3rd miner.
        if n_ext < 1:
            next_bot = BotClass.Extractor
        elif n_battle < 6:
            next_bot = BotClass.Battle
        elif n_ext < want_extractors:
            next_bot = BotClass.Extractor
        elif n_heal < want_healers:
            next_bot = BotClass.Healer
        else:
            next_bot = BotClass.Battle
        action.fabricator_next = int(next_bot)

        natural_due = state.fabricator_me.next_bot_creation <= state.tick
        last_build_tick = state.tick + 1 >= endgame_start
        action.rush_order = (
            not in_endgame
            and not state.fleet_me.is_full()
            and state.fabricator_me.tokens >= conf.fabricator.rush_cost
            and not (natural_due and last_build_tick)
        )

        extractors = sorted(extractors, key=lambda b: b.id)
        spots = {
            bot.id: mining_spot(conf, deposit, i, max(n_ext, 3))
            for i, bot in enumerate(extractors)
        }
        flee_to = closest(deposit, battle).pos if battle else payload

        # ~10% peel, and only while someone is actually on our deposit.
        n_defend = min(n_battle, max(1, n_battle // 10)) if threats else 0
        defender_ids = set()
        defend_index = {}
        if n_defend:
            nearest_home = sorted(battle, key=lambda b: b.pos.dist_sq(deposit))
            defender_ids = {b.id for b in nearest_home[:n_defend]}
            defend_index = {b.id: i for i, b in enumerate(nearest_home[:n_defend])}

        payload_crew = [b for b in battle if b.id not in defender_ids] + list(healers)
        if in_endgame:
            payload_crew.extend(extractors)
        payload_crew = sorted(payload_crew, key=lambda b: b.id)
        payload_slots = {
            b.id: formation_slot(payload, i, len(payload_crew), conf)
            for i, b in enumerate(payload_crew)
        }
        shots = assign_splash_shots(battle, enemies, state, conf)

        heal_counts = {}

        def healer_target(healer: BotState) -> Optional[BotState]:
            cap = int(conf.bot.heal_stack_cap)
            locked_id = self.heal_lock.get(healer.id)
            locked = state.fleet_me.get(locked_id) if locked_id is not None else None

            wounded = []
            for ally in state.fleet_me:
                if ally.id == healer.id:
                    continue
                if ally.class_ == BotClass.Extractor:
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
                    if worst is None or worst.health >= locked.health - 2.0:
                        target = locked
            if target is None and wounded:
                target = min(wounded, key=lambda a: a.health)
            if target is None:
                if locked is not None:
                    target = locked
                else:
                    blob = [b for b in battle if b.id != healer.id]
                    target = closest(payload, blob) if blob else None

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
                if in_endgame:
                    slot = payload_slots.get(bot.id, payload)
                    bot_action.move_action = move_bot(navigate_to(bot.pos, slot))
                    bot_action.turn_action = turn_towards(payload)
                    bot_action.special_action = SpecialAction.Extractor(mine=False)
                else:
                    threatened = False  # miners never flee: stay on the node and mine
                    act_extractor(
                        bot,
                        bot_action,
                        spots.get(bot.id, mining_spot(conf, deposit, 0)),
                        deposit,
                        flee_to,
                        threatened,
                    )
                continue

            if bot.class_ == BotClass.Healer:
                fallback = payload_slots.get(bot.id, payload)
                act_healer(bot, bot_action, healer_target(bot), fallback, conf)
                continue

            shoot, fire = shots.get(bot.id, (opp, True))
            if bot.id in defender_ids and threats:
                mark = closest(bot.pos, threats)
                dest = formation_slot(
                    mark.pos, defend_index.get(bot.id, 0), max(n_defend, 1), conf
                )
                act_battle(bot, bot_action, dest, mark, state, conf, fire=True)
            else:
                dest = payload_slots.get(bot.id, payload)
                act_battle(bot, bot_action, dest, shoot, state, conf, fire=fire)

        return action


def get_strategy(team: int) -> Strategy:
    # Engine mirrors team B so both sides see themselves as bottom-left.
    # Same brain either way — do not branch on `team`.
    return Brain()
