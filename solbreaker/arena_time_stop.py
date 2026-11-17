"""Wire an isolated worker into the authoritative battle lifecycle."""
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count
import time

from . import big_boss
from .arena_commands import decode_graviton_aim
from .collision import Pose
from .drive_replica import build_spec
from .recon_wire import ReconSnapshot
from .sandbox import BRANCH_SHOT_BASE, INPUT_LEASE, MISSILE_VIEW
from .time_stop import (ABILITY_RECIPES, LEVELS, EffectLedger, Entity, ProjectileTrace, Rules, Snapshot,
                        merge_frame, place_returning_body, restore_aim_lock, return_spawn, safe_return)
from .time_stop_worker import Failure, Frame, Prepared, Step, Worker


@dataclass(frozen=True)
class PendingActivation:
    account: int
    owner: Entity
    expires_at: float


class BattleTimeStop:
    PREPARE_WAIT = 5.
    REPLY_TIMEOUT = 2.
    RETURN_RETRY = .1
    MERGE_TRAP_VISUAL_SECONDS = .4

    def __init__(self, arena, *, rules=None):
        self.arena = arena
        self.rules = Rules.from_environment() if rules is None else rules
        self.level = None
        self._player_levels = {}
        self.geometry = None
        self.geometry_dirty = True
        self.worker = None
        self.ready = False
        self.pending = None
        self.snapshot = None
        self.frame = None
        self.reply_deadline = None
        self.return_retry_at = 0.
        self.account = None
        self.next_action = 0
        self.origin = None
        self.markers = ()
        self.flags = 0
        self.last_input = 0.
        self.aim = self.point = self.lock = None
        self.missile_camera = None
        self.missile_update_at = 0.
        self.actions = []
        self.finishing = False
        self.rearm = False
        self.retiring = False
        self.merge_hits = []
        self.merge_visuals = []
        self.merge_traps = []
        self.frozen_traps = ()
        self._merge_order = count()
        self._merging = False
        self._finish_after_merge = None
        self._warm()

    @property
    def entity(self):
        return self.snapshot.owner.ident if self.snapshot is not None else None

    def defer_finish(self, winner, reason, now):
        if not self._merging:
            return False
        # A lethal effect can select a winner inside the return transaction.
        if self._finish_after_merge is None:
            self._finish_after_merge = winner, reason, now
        return True

    def set_player_level(self, account, level):
        """Set a player's ability level for subsequent activations in this battle."""
        if account not in self.arena.players:
            raise ValueError('Unknown Time Stop player')
        if type(level) is not int or not 1 <= level <= len(LEVELS):
            raise ValueError('Invalid Time Stop player level')
        self._player_levels[account] = level

    def _warm(self):
        self.ready = False
        if self.geometry_dirty:
            self.geometry = build_spec(self.arena.world, include_armor=True)
            self.geometry_dirty = False
        self.worker = Worker(None, self.geometry, rules=self.rules)

    def _try_warm(self):
        try:
            self._warm()
        except (OSError, RuntimeError, ValueError) as error:
            self.pending = None
            self.arena.emit('time_stop_worker_error', reason=str(error))
            return False
        return True

    def refresh_geometry(self):
        self.geometry_dirty = True
        if self.snapshot is None:
            self.ready = False
            self.retiring = self.rearm = True

    def cancel(self, account):
        if self.pending is not None and self.pending.account == account:
            self.pending = None
        if self.account != account:
            return False
        self.abort()
        self.rearm = True
        return True

    def _reject(self, account, reason):
        player = self.arena.players.get(account)
        self.arena.emit('time_stop_rejected', player.vehicle_id if player else 0,
                        account_id=account, reason=reason)
        return False

    def toggle(self, account):
        if account == self.account:
            self.finishing = True
            return True
        if self.snapshot is not None or self.pending is not None and self.pending.account != account:
            return self._reject(account, 'branch_busy')
        arena = self.arena
        player = arena.players.get(account)
        if (not arena.started or arena.finished_at or player is None
                or not player.attached or not player.loaded or account in arena.hangar_players):
            return self._reject(account, 'player_unavailable')
        owner = arena.vehicle_for(account)
        if not owner.alive or owner.entity_id in arena.respawn_at:
            return self._reject(account, 'owner_respawning')
        if arena._fire_active(owner):
            return self._reject(account, 'owner_on_fire')
        if any(slot and slot.active_until > arena.simulation_time and slot.spec.ident not in ABILITY_RECIPES
               for slot in owner.consumables):
            return self._reject(account, 'active_equipment')
        if not self.ready:
            if self.pending is None:
                self.pending = PendingActivation(account, Entity.of(owner), time.monotonic()+self.PREPARE_WAIT)
                arena.emit('time_stop_queued', owner.entity_id, account_id=account)
            if self.retiring:
                self.rearm = True
            elif self.worker is None:
                return self._try_warm()
            return True
        try:
            snapshot = Snapshot.capture(arena, account, time.monotonic())
        except ValueError:
            return self._reject(account, 'snapshot_unavailable')
        level = LEVELS[self._player_levels.get(account, self.rules.starting_level) - 1]
        if not self.worker.activate(snapshot, level=level):
            return self._reject(account, 'worker_unavailable')
        flags = arena._active_control_flags(owner, arena.simulation_time) & 15
        self.frame, self.snapshot, self.account = None, snapshot, account
        boss = arena.big_boss
        self.frozen_traps = (*boss.traps, *self.merge_traps) if boss is not None else ()
        self.level = level
        self.reply_deadline = snapshot.captured_at + self.REPLY_TIMEOUT
        self.flags = flags
        self.last_input = owner.last_input if flags else arena.simulation_time
        self.aim = self.point = self.lock = None
        self.missile_camera = None
        self.missile_update_at = 0.
        self.actions.clear()
        self.finishing = False
        self.next_action = 0
        self.ready = False
        origin = owner.pose
        self.origin = origin
        low_x, low_z, _, _ = arena.world.assets.bounds
        # Keep the entity ID and health for arena results. Its sensor colliders
        # are disabled. The remote pose hides the retained native client entity
        # and keeps older physics libraries outside the playable area.
        parked = Pose((low_x-3000., origin.position[1], low_z-3000.), origin.yaw)
        arena.world.relocate_training_body(owner.entity_id, parked)
        arena.world.set_tracked_presence(owner.entity_id, False)
        owner.pose, owner.speed, owner.flags = parked, 0., 0
        arena._landing_skids.pop(owner.entity_id, None)
        if arena.big_boss is not None:
            arena.big_boss.heal_waves.pop(owner.entity_id, None)
            arena.big_boss.shields.pop(owner.entity_id, None)
            arena.big_boss.poison.pop(owner.entity_id, None)
        arena._lose_target(owner.entity_id)
        if arena.team_spotting:
            self.markers = tuple(sorted(tank.identity.ident for tank in snapshot.tanks
                if tank.alive and tank.team != owner.team and tank.visible))
        if arena.recon is not None:
            self.markers = tuple(sorted(tank.identity.ident for tank in snapshot.tanks
                if tank.alive and tank.team != owner.team and arena.recon.visible(
                    owner.entity_id, owner.team, tank.identity.ident, arena.simulation_time)))
            arena.recon.remove_vehicle(owner.entity_id)
        arena.emit('time_stop_begin', owner.entity_id, position=origin.position, parked=parked,
                   seconds=self.rules.duration, energy=self.level.energy, branch=snapshot.ident,
                   traces=snapshot.projectiles)
        return True

    def peer_clock(self, account):
        if self.snapshot is not None and account != self.account:
            remaining = max(0., self.snapshot.captured_at+self.rules.duration-time.monotonic())
            return self.origin.position, remaining
        return None

    def recon_snapshot(self, entity, battle_id, sequence):
        if self.entity == entity:
            return ReconSnapshot(battle_id, sequence, False, self.markers)
        return None

    def accept(self, command):
        if command.account_id != self.account or command.action == 'sandbox':
            return False
        if self.retiring or (self.finishing and command.action != 'control'):
            return True
        args = command.arguments
        if command.action == 'control':
            self.flags = args[0] & 15
            self.last_input = self.arena.simulation_time
        elif command.action == 'aim_angles':
            self.aim, self.point = args, None
        elif command.action == 'aim_point':
            self.point, self.aim = (args[0], False), None
        elif command.action == 'aim_point_entity' and args[0] == self.entity:
            self.point, self.aim = (args[1], args[2]), None
        elif command.action in ('autoaim', 'autoaim_point') and args[0] in (0, self.entity):
            self.lock = args[1:]
            self.point = None
        elif command.action == 'fire':
            if not any(name == 'fire' for _, name, _ in self.actions):
                self._queue_action('fire', ())
        elif command.action == 'select_shell':
            self._queue_action('select_shell', args)
        elif command.action == 'consumable' and args[0] == self.entity:
            ident, extra = args[1:]
            if not extra or ident in big_boss.GRAVITON_INPUT_IDS:
                self.request_ability(ident, extra)
        return True

    def missile_control(self, control):
        self.missile_camera = None
        if control.flags & MISSILE_VIEW and control.shot >= BRANCH_SHOT_BASE:
            self.missile_camera = (control.shot-BRANCH_SHOT_BASE, control.direction(),
                                   self.arena.simulation_time+INPUT_LEASE)

    def _queue_action(self, name, args):
        if len(self.actions) < 8:
            self.next_action += 1
            self.actions.append((self.next_action, name, args))

    def request_ability(self, ident=None, extra=b''):
        if (self.snapshot is None or self.finishing or self.retiring
                or any(name == 'ability' for _, name, _ in self.actions)):
            return
        installed = tuple(slot.spec.ident for slot in self.snapshot.consumables
                          if slot.spec.ident in ABILITY_RECIPES)
        if ident is None:
            ident = installed[0] if installed else None
        elif ident not in installed and ident in big_boss.ROLE_INPUT_IDS:
            ident = next((slot for slot in installed if slot in big_boss.ROLE_INPUT_IDS), None)
        if ident in installed:
            if extra and ident in big_boss.GRAVITON_INPUT_IDS:
                try:
                    view = decode_graviton_aim(extra)
                except ValueError:
                    return
                self._queue_action('ability', (ident, view))
            else:
                self._queue_action('ability', (ident,))

    def tick(self, now):
        while self.merge_hits and self.merge_hits[0][0] <= now + 1e-9:
            _, _, effect = heappop(self.merge_hits)
            EffectLedger.apply_committed_hit(self.arena, effect)
        self._show_merge_visuals(now)
        worker = self.worker
        prepare_timed_out = self.pending is not None and time.monotonic() >= self.pending.expires_at
        if prepare_timed_out:
            self._reject(self.pending.account, 'prepare_timeout')
            self.pending = None
        if self.retiring:
            if now + 1e-9 >= self.return_retry_at:
                self._restore_aborted_owner(now)
            if worker is not None and worker.advance_close():
                self.worker = None
            if self.worker is not None or self.snapshot is not None:
                return
            self.retiring = False
            if self.rearm:
                self.rearm = False
                self._try_warm()
            return
        if worker is None:
            return
        reply = worker.poll()
        if isinstance(reply, Prepared):
            self.ready = True
            pending, self.pending = self.pending, None
            if pending is not None:
                owner = self.arena.vehicles.get(pending.owner.ident)
                if owner is not None and Entity.of(owner) == pending.owner:
                    self.toggle(pending.account)
                else:
                    self._reject(pending.account, 'life_changed')
        elif isinstance(reply, Failure):
            self.arena.emit('time_stop_worker_error', self.entity or 0, reason=reply.reason)
            self.abort()
            # A failed worker is never rearmed on each tick.
            return
        elif prepare_timed_out:
            # Release the stalled worker so the next activation can retry.
            self.abort()
            return
        elif isinstance(reply, Frame):
            previous = self.frame
            if self.snapshot is not None:
                # The final ledger is due by the real-time deadline, even if
                # a faulty worker keeps sending intermediate frames.
                ends_at = self.snapshot.captured_at + self.rules.duration
                self.reply_deadline = min(time.monotonic(), ends_at) + self.REPLY_TIMEOUT
            if self.snapshot is not None and reply.ledger is None:
                self._present_abilities(reply, previous)
            if self.snapshot is not None and previous is not None:
                if reply.energy != previous.energy:
                    self.arena.emit('time_stop_energy', self.entity, energy=reply.energy)
                for shot, count in reply.shots:
                    self.arena.emit('time_stop_shot', self.entity, shot=shot, shooting_count=count)
                for shot, point, guided, explosion in reply.shot_ends:
                    self.arena.emit('time_stop_shot_end', self.entity, shot=shot,
                                    point=point, guided=guided, explosion=explosion)
                if reply.autoaim_lost:
                    self.arena.emit('time_stop_autoaim_lost', self.entity)
                if reply.ledger is None and reply.seconds+1e-9 >= self.missile_update_at:
                    self.missile_update_at = reply.seconds+self.arena.STEP*self.arena.MISSILE_UPDATE_STEPS
                    for trace in reply.projectiles:
                        if trace.guided:
                            self.arena.emit('time_stop_missile_move', self.entity, trace=trace)
            if (self.snapshot is not None and reply.ledger is None
                    and (previous is None or reply.gun_revision != previous.gun_revision)):
                selected_changed = previous is not None and reply.selected_shell != previous.selected_shell
                reload_changed = (previous is None or reply.reload_at != previous.reload_at
                                  or reply.pump_at != previous.pump_at
                                  or selected_changed and (reply.reload_at or reply.pump_at))
                weapon = self.snapshot.gun_loadout
                pump = ()
                if weapon.pump_times:
                    pump = tuple(value/self.level.cooldown_multiplier
                                 for value in (weapon.full_reload, weapon.interval, *weapon.pump_times))
                self.arena.emit('time_stop_gun_state', self.entity,
                    shell=reply.selected_shell, counts=reply.ammo_by_shell, clip=reply.clip,
                    selected_changed=selected_changed,
                    remaining=reply.reload_remaining if reload_changed else None,
                    total=reply.reload_total, pump=pump,
                    refill=reply.pump_remaining if reply.pump_at else None)
            self.frame = reply
        if self.snapshot is None:
            return
        if self.frame is not None and self.frame.ledger is not None:
            if now + 1e-9 < self.return_retry_at:
                return
            self._merging = True
            try:
                self._merge_final_frame(now, worker)
            finally:
                self._merging = False
                finish, self._finish_after_merge = self._finish_after_merge, None
            if finish is not None:
                self.arena.finish(*finish)
            return
        if self.reply_deadline is not None and time.monotonic() >= self.reply_deadline:
            self.arena.emit('time_stop_worker_error', self.entity, reason='reply_timeout')
            self.abort()
            return
        if self.frame is None:
            return
        flags = self.flags if now-self.last_input <= self.arena.CONTROL_INPUT_TIMEOUT else 0
        camera = self.missile_camera
        camera = camera[:2] if camera is not None and now <= camera[2] else None
        if worker.submit(Step(flags=flags, aim=self.aim, point=self.point, actions=tuple(self.actions),
                              finish=self.finishing, lock=self.lock, missile_camera=camera)):
            self.aim = self.point = self.lock = None
            self.actions.clear()

    def _merge_final_frame(self, now, worker):
        outcome = merge_frame(self.arena, self.snapshot, self.frame, rules=self.rules,
                              defer_hit=self._queue_merge_hit)
        if self.snapshot is None:
            return
        if self.frame.ledger.aborted:
            self.abort()
            return
        if outcome is None:
            self.return_retry_at = now + self.RETURN_RETRY
            return
        owner = self.arena.vehicles[self.entity]
        if owner.alive and now-self.last_input < self.arena.CONTROL_INPUT_TIMEOUT:
            owner.flags, owner.last_input = self.flags, self.last_input
        self._stage_merge_visuals(now, self.frame.merge_visuals, outcome.damage_ticks)
        self._end(refund=outcome.refund, aborted=False)
        self._show_merge_visuals(now)
        if self.geometry_dirty or not worker.prepare_next():
            self.retiring = self.rearm = True
            worker.advance_close()

    def _queue_merge_hit(self, tick, effect):
        at = self.arena.simulation_time + tick * self.arena.STEP
        heappush(self.merge_hits, (at, next(self._merge_order), effect))

    def _stage_merge_visuals(self, now, visuals, damage_ticks):
        # An area effect plays once, at the first damage tick for that impact.
        # Effects without a corresponding hit still play over the return window.
        unmatched = []
        for visual in visuals:
            tick = damage_ticks.get((visual.at, visual.action))
            if tick is None:
                unmatched.append(visual.event)
                continue
            at = now + tick * self.arena.STEP
            heappush(self.merge_visuals, (at, next(self._merge_order), visual.event))
        last_index = max(1, len(unmatched)-1)
        for index, event in enumerate(unmatched):
            at = now + .04 + .36 * index/last_index
            heappush(self.merge_visuals, (at, next(self._merge_order), event))

    def _show_merge_visuals(self, now):
        arena = self.arena
        boss = arena.big_boss
        traps_changed = False
        if self.merge_traps and now >= min(trap.expires_at for trap in self.merge_traps):
            self.merge_traps[:] = [trap for trap in self.merge_traps if now < trap.expires_at]
            traps_changed = True
        while self.merge_visuals and self.merge_visuals[0][0] <= now + 1e-9:
            _, _, event = heappop(self.merge_visuals)
            if event.kind == 'time_stop_graviton_merge':
                if boss is not None:
                    trap = event.data['trap']
                    self.merge_traps.append(trap._replace(ident=boss.next_trap,
                        expires_at=now+self.MERGE_TRAP_VISUAL_SECONDS))
                    boss.next_trap += 1
                    traps_changed = True
                continue
            if event.kind == 'provision_effect':
                owner = arena.vehicles.get(event.actor)
                if (owner is None or not owner.alive or owner.generation != event.data['generation']
                        or event.actor == self.entity):
                    continue
            arena.emit(event.kind, event.actor, event.target, **event.data)
        if traps_changed:
            arena.emit('gravitrap_visual_change')

    def _present_abilities(self, frame, previous):
        # The client counts down locally. Send a restart even if the worker's
        # snapshots straddle a ready state and show the same state on both sides.
        previous_states = ({ident: (state, uses) for ident, state, _, _, uses in previous.ability_views}
                           if previous else {})
        for ident, state, remaining, total, uses in frame.ability_views:
            if previous_states.get(ident) != (state, uses):
                self.arena.emit('time_stop_consumable', self.entity, compact_id=ident,
                                state=state, remaining=remaining, total=total)
        if frame.abilities_used > (previous.abilities_used if previous else 0):
            kind, level, compact_id = frame.last_ability
            self.arena.emit('time_stop_ability', self.entity, kind_name=kind, level=level, compact_id=compact_id)
        if frame.traps != (previous.traps if previous else ()):
            self.arena.emit('time_stop_traps', self.entity, traps=(*self.frozen_traps, *frame.traps))
        active = frame.heal_wave is not None
        if active != bool(previous and previous.heal_wave):
            owner = self.arena.vehicles[self.entity]
            modifiers = tuple(self.arena.state_modifiers(owner))
            if active:
                modifiers += (big_boss.HEAL_AURA_MODIFIERS[frame.heal_wave[3]],)
            self.arena.emit('time_stop_modifiers', self.entity, modifiers=modifiers)

    def _end(self, *, refund, aborted):
        arena = self.arena
        retired = tuple(trace for trace in self.snapshot.projectiles if trace.shot not in arena.projectiles)
        live = tuple(ProjectileTrace.capture(projectile) for projectile in arena.projectiles.values())
        missed_destructions = tuple(key for key in arena.destructions if key not in self.snapshot.destroyed)
        owner = arena.vehicles.get(self.entity)
        arena.emit('time_stop_end', self.entity, position=owner.pose.position if owner else self.origin.position,
                   refund=refund, aborted=aborted, branch=self.snapshot.ident,
                   retired_traces=retired, live_traces=live,
                   missed_destructions=missed_destructions)
        self.snapshot = self.frame = self.account = None
        self.frozen_traps = ()
        self.level = None
        self.reply_deadline = None
        self.return_retry_at = 0.

    def _restore_aborted_owner(self, now, *, battle_ended=False):
        snapshot = self.snapshot
        if snapshot is None:
            return
        arena = self.arena
        entity = snapshot.owner.ident
        owner = arena.vehicles.get(entity)
        if owner is not None and owner.alive and Entity.of(owner) == snapshot.owner:
            origin = next(tank.body.pose for tank in snapshot.tanks if tank.identity == snapshot.owner)
            world, drive = arena.world, snapshot.drive
            pose = safe_return(world, entity, origin, origin, grounded=drive.motion.grounded,
                               activation_grounded=drive.motion.grounded)
            if pose is None:
                pose = return_spawn(arena, owner)
            if pose is None and not battle_ended:
                self.return_retry_at = now + self.RETURN_RETRY
                return
            if pose is not None:
                motion = drive if pose is origin and not drive.motion.grounded else None
                owner.pose = place_returning_body(world, entity, pose, motion)
                owner.speed = drive.speed if motion is not None else 0.
                restore_aim_lock(arena, owner, snapshot.aim_lock)
                if owner.burst_pending:
                    owner.reload_at = now + snapshot.reload_remaining
                if snapshot.heal_wave is not None and arena.big_boss is not None:
                    due, count, level = snapshot.heal_wave
                    arena.big_boss.heal_waves[entity] = (now + due, count, level)
                snapshot.restore_owner_status(arena)
        self._end(refund=0, aborted=True)

    def abort(self, *, battle_ended=False):
        if battle_ended:
            self.merge_visuals.clear()
            if self.merge_traps:
                self.merge_traps.clear()
                self.arena.emit('gravitrap_visual_change')
        already_retiring = self.retiring
        self.ready = False
        self.pending = None
        self.retiring = True
        self.rearm = False
        if self.snapshot is not None and not self.arena.retiring:
            if self.frame is not None and not already_retiring:
                for trace in self.frame.projectiles:
                    self.arena.emit('time_stop_shot_end', self.entity, shot=trace.shot, point=trace.position,
                                    guided=trace.guided, explosion=None)
            self.frame = None
            self.actions.clear()
            if not already_retiring or battle_ended:
                self._restore_aborted_owner(self.arena.simulation_time, battle_ended=battle_ended)
        if self.worker is not None:
            return self.worker.advance_close()
        return True
