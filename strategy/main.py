from . import *
from .tactics import (
    act_battle,
    act_extractor,
    act_healer,
    assign_retreat_dests,
    assign_splash_shots,
    by_class,
    closest,
    formation_slot,
    is_critical,
    mining_spot,
    nearest_healer_spot,
    pick_focus,
    spawn_pos,
    toward,
)


class Brain:
    """Win the first lane fight at range, then sit cart. Miners never flee. 1:2 healers."""

    def __init__(self) -> None:
        self.heal_lock = {}
        self.had_lead = False
        self.won_field = False
        self.saw_army = False
        self.lane_hp = 0.0

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

        lane_r = conf.bot.blaster_range + conf.payload.capture_radius + 4.0
        wave_r = conf.bot.blaster_range * 2.0 + conf.payload.capture_radius
        local_enemy = [
            e
            for e in enemies
            if e.class_ != BotClass.Extractor and e.pos.dist(payload) <= lane_r
        ]
        wave = [
            e
            for e in enemies
            if e.class_ != BotClass.Extractor and e.pos.dist(payload) <= wave_r
        ]
        our_hp = sum(b.health for b in battle) + sum(h.health for h in healers)
        en_hp = sum(e.health for e in local_enemy)
        wave_hp = sum(e.health for e in wave)
        dying = bool(local_enemy) and en_hp < self.lane_hp * 0.92
        self.lane_hp = en_hp

        if wave:
            self.saw_army = True
            if n_battle < 3 and len(wave) >= 2:
                self.won_field = False
            elif n_battle >= 3 and len(wave) <= 1:
                self.won_field = True
            elif n_battle >= 4 and our_hp >= wave_hp * 1.5 and len(wave) <= 2:
                self.won_field = True
        elif self.saw_army:
            self.won_field = True

        skirmish = (not in_endgame) and (not self.won_field)
        raid = (not in_endgame) and (not skirmish) and n_enemy_ext > n_ext
        hold = (
            not in_endgame
            and not raid
            and not skirmish
            and self.had_lead
            and capture >= -0.06
        )

        want_extractors = 0
        if not in_endgame:
            want_extractors = max(4, n_enemy_ext)
            if hold:
                want_extractors = max(want_extractors, n_enemy_ext + 1, 5)
            want_extractors = min(want_extractors, 8)

        # 1 healer : 2 fighters. Fill as the army grows, not a late sprinkle.
        want_healers = max(1, n_battle // 2) if n_battle >= 2 else 0

        if n_ext < 1:
            next_bot = BotClass.Extractor
        elif n_battle < 4:
            next_bot = BotClass.Battle
        elif n_ext < 4:
            next_bot = BotClass.Extractor
        elif n_heal < want_healers:
            next_bot = BotClass.Healer
        elif n_ext < want_extractors:
            next_bot = BotClass.Extractor
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

        focus = pick_focus(enemies, payload, deposit, raid, skirmish=skirmish)
        shots = assign_splash_shots(
            battle, enemies, state, conf, focus=focus, skirmish=skirmish
        )

        ring_r = None
        if raid and focus is not None:
            anchor = focus.pos
            disperse = 1.0
        elif skirmish:
            stand = conf.bot.blaster_range * 0.62
            anchor = toward(payload, spawn, stand)
            disperse = 1.25
            ring_r = min(1.55, conf.bot.base_heal_range * 0.5)
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
            b.id: formation_slot(
                anchor, i, len(payload_crew), conf, disperse=disperse, max_radius=ring_r
            )
            for i, b in enumerate(payload_crew)
        }

        heal_anchor = toward(anchor, spawn, min(conf.bot.base_heal_range * 0.65, 1.8))
        healers_sorted = sorted(healers, key=lambda b: b.id)
        heal_slots = {
            h.id: formation_slot(
                heal_anchor,
                i,
                max(len(healers_sorted), 1),
                conf,
                disperse=1.2,
                max_radius=ring_r,
            )
            for i, h in enumerate(healers_sorted)
        }

        dests = {}
        for i, bot in enumerate(sorted(battle, key=lambda b: b.id)):
            if raid and focus is not None:
                dests[bot.id] = formation_slot(focus.pos, i, max(n_battle, 1), conf)
            else:
                dests[bot.id] = slots.get(bot.id, anchor)

        peel = skirmish and capture < -0.25 and len(local_enemy) >= 2 and not dying
        if peel and battle:
            rim = toward(
                payload,
                spawn,
                max(conf.payload.capture_radius - conf.bot.radius - 0.2, 0.6),
            )
            peel_n = max(1, n_battle // 3)
            peel_bots = [b for b in sorted(battle, key=lambda b: -b.health) if not is_critical(b, conf)]
            peel_bots = peel_bots[:peel_n]
            for i, bot in enumerate(peel_bots):
                dests[bot.id] = formation_slot(
                    rim, i, max(len(peel_bots), 1), conf, disperse=1.2, max_radius=0.9, min_radius=0.35
                )

        dests = assign_retreat_dests(
            battle,
            healers,
            dests,
            slots,
            heal_anchor,
            payload,
            spawn,
            conf,
            raid,
            skirmish=skirmish,
        )

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
            # Weakest first (hurt healers included). Break ties by how close they are.

            target = None
            if locked is not None and locked.health < conf.bot.health * 0.98:
                if heal_counts.get(locked.id, 0) < cap:
                    worst = min(wounded, key=lambda a: (a.health, healer.pos.dist_sq(a.pos))) if wounded else None
                    if worst is None or worst.health >= locked.health - 2.0:
                        target = locked
            if target is None and wounded:
                target = min(wounded, key=lambda a: (a.health, healer.pos.dist_sq(a.pos)))
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
                if is_critical(bot, conf):
                    others = [h for h in healers if h.id != bot.id]
                    fallback = nearest_healer_spot(bot, others, heal_anchor, spawn, conf)
                act_healer(bot, bot_action, healer_target(bot), fallback, conf)
                continue

            shoot, fire = shots.get(bot.id, (opp, True))
            dest = dests.get(bot.id, slots.get(bot.id, anchor))
            act_battle(bot, bot_action, dest, shoot, state, conf, fire=fire)

        return action


def get_strategy(team: int) -> Strategy:
    # Engine mirrors team B so both sides see themselves as bottom-left.
    # Same brain either way — do not branch on `team`.
    return Brain()
