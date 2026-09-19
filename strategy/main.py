from . import *
from .tactics import (
    act_battle,
    act_extractor,
    act_healer,
    assign_splash_shots,
    by_class,
    closest,
    formation_slot,
    mining_spot,
    pick_focus,
    spawn_pos,
    toward,
)


class Brain:
    """Max bodies. 4+ miners that never flee. Raid if behind on miners. Hold a payload lead."""

    def __init__(self) -> None:
        self.heal_lock = {}
        self.had_lead = False

    def __call__(self, state: GameState) -> FleetAction:
        conf = get_config()
        action = FleetAction.new()

        payload = state.payload_pos()
        deposit = state.deposit_me.pos
        spawn = spawn_pos(conf)
        enemies = [e for e in state.fleet_other]
        extractors, healers, battle = by_class(state.fleet_me)
        enemy_miners = [e for e in enemies if e.class_ == BotClass.Extractor]

        endgame_start = conf.max_ticks - conf.endgame_ticks
        in_endgame = state.tick >= endgame_start
        capture = state.capture
        if capture >= 0.10:
            self.had_lead = True

        n_ext, n_heal, n_battle = len(extractors), len(healers), len(battle)
        n_enemy_ext = len(enemy_miners)
        raid = (not in_endgame) and n_enemy_ext > n_ext
        hold = (
            not in_endgame
            and not raid
            and self.had_lead
            and capture >= -0.06
        )

        want_extractors = 0
        if not in_endgame:
            want_extractors = max(4, n_enemy_ext)
            if hold:
                want_extractors = max(want_extractors, n_enemy_ext + 1, 5)
            want_extractors = min(want_extractors, 8)

        want_healers = 0
        if n_battle >= 6:
            want_healers = 1
        if n_battle >= 12:
            want_healers = 2

        if n_ext < 1:
            next_bot = BotClass.Extractor
        elif n_battle < 4:
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
            bot.id: mining_spot(conf, deposit, i, max(n_ext, want_extractors, 4))
            for i, bot in enumerate(extractors)
        }

        focus = pick_focus(enemies, payload, deposit, raid)
        shots = assign_splash_shots(battle, enemies, state, conf, focus=focus)

        if raid and focus is not None:
            anchor = focus.pos
            disperse = 1.0
        elif hold:
            anchor = toward(payload, spawn, 0.9)
            disperse = 1.45
        else:
            anchor = payload
            disperse = 1.0

        payload_crew = list(battle) + list(healers)
        if in_endgame:
            payload_crew.extend(extractors)
        payload_crew = sorted(payload_crew, key=lambda b: b.id)
        slots = {
            b.id: formation_slot(anchor, i, len(payload_crew), conf, disperse=disperse)
            for i, b in enumerate(payload_crew)
        }

        heal_anchor = toward(anchor, spawn, min(conf.bot.base_heal_range * 0.65, 1.8))
        healers_sorted = sorted(healers, key=lambda b: b.id)
        heal_slots = {
            h.id: formation_slot(heal_anchor, i, max(len(healers_sorted), 1), conf, disperse=1.2)
            for i, h in enumerate(healers_sorted)
        }

        heal_counts = {}

        def healer_target(healer: BotState) -> Optional[BotState]:
            cap = int(conf.bot.heal_stack_cap)
            locked_id = self.heal_lock.get(healer.id)
            locked = state.fleet_me.get(locked_id) if locked_id is not None else None

            wounded = []
            for ally in state.fleet_me:
                if ally.id == healer.id or ally.class_ == BotClass.Extractor:
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
                    target = closest(anchor, blob) if blob else None

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
                    slot = slots.get(bot.id, payload)
                    bot_action.move_action = move_bot(navigate_to(bot.pos, slot))
                    bot_action.turn_action = turn_towards(payload)
                    bot_action.special_action = SpecialAction.Extractor(mine=False)
                else:
                    act_extractor(
                        bot,
                        bot_action,
                        spots.get(bot.id, mining_spot(conf, deposit, 0, 4)),
                        deposit,
                    )
                continue

            if bot.class_ == BotClass.Healer:
                fallback = heal_slots.get(bot.id, toward(anchor, spawn, 1.5))
                act_healer(bot, bot_action, healer_target(bot), fallback, conf)
                continue

            shoot, fire = shots.get(bot.id, (opp, True))
            dest = slots.get(bot.id, anchor)
            if raid and focus is not None:
                dest = formation_slot(focus.pos, bot.id, max(n_battle, 1), conf)
            act_battle(bot, bot_action, dest, shoot, state, conf, fire=fire)

        return action


def get_strategy(team: int) -> Strategy:
    # Engine mirrors team B so both sides see themselves as bottom-left.
    # Same brain either way — do not branch on `team`.
    return Brain()
