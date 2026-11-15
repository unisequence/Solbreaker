"""Wire an isolated worker into the authoritative battle lifecycle."""
import time

from . import big_boss
from .collision import Pose
from .drive_replica import build_spec
from .recon_wire import ReconSnapshot
from .time_stop import ABILITY_RECIPES, LEVELS, ProjectileTrace, Rules, Snapshot, merge_frame, safe_return
from .time_stop_worker import Failure, Frame, Prepared, Step, Worker


class BattleTimeStop:
    def __init__(self, arena, *, rules=Rules(), level=LEVELS[0]):
        self.arena, self.level, self.rules = arena, level, rules
        self.geometry = build_spec(arena.world, include_armor=True)
        self.worker = None
        self.ready = False
        self.snapshot = None
        self.frame = None
        self.account = None
        self.next_action = 0
        self.origin = None
        self.markers = ()
        self.flags = 0
        self.last_input = 0.
        self.aim = self.point = None
        self.ability = self.fire = None
        self.finishing = False
        self.rearm = False
        self.retiring = False
        self._warm()

    @property
    def entity(self):
        return self.snapshot.owner.ident if self.snapshot is not None else None

    def _warm(self):
        self.ready = False
        self.worker = Worker(None, self.geometry, level=self.level, rules=self.rules)

    def toggle(self, account):
        if account == self.account:
            self.finishing = True
            return True
        if self.snapshot is not None or not self.ready:
            return False
        arena = self.arena
        owner = arena.vehicle_for(account)
        assets = arena.world.assets_for(owner.entity_id)
        # Prototype support is restricted to normal tracked life cycles. A
        # divergent hull layout cannot use a worker that was warmed earlier.
        if (owner.entity_id in arena.world.dynamic_bodies
                or not arena.world.motion[owner.entity_id].grounded
                or arena._fire_active(owner) or owner.burst_pending
                or any(slot and slot.active_until > arena.simulation_time and slot.spec.ident not in ABILITY_RECIPES
                       for slot in owner.consumables)
                or assets.definition != self.geometry.vehicle_assets[owner.entity_id].definition):
            return False
        try:
            snapshot = Snapshot.capture(arena, account, time.monotonic())
        except ValueError:
            return False
        if not self.worker.activate(snapshot):
            return False
        self.frame, self.snapshot, self.account = None, snapshot, account
        self.flags = 0
        self.last_input = arena.simulation_time
        self.aim = self.point = None
        self.ability = self.fire = None
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
        if arena.recon is not None:
            self.markers = tuple(sorted(tank.identity.ident for tank in snapshot.tanks
                if tank.alive and tank.team != owner.team and arena.recon.visible(
                    owner.entity_id, owner.team, tank.identity.ident, arena.simulation_time)))
            arena.recon.remove_vehicle(owner.entity_id)
        arena.emit('time_stop_begin', owner.entity_id, position=origin.position, parked=parked,
                   seconds=self.rules.duration, energy=self.level.energy,
                   traces=snapshot.projectiles)
        return True

    def peer_clock(self, account):
        if self.snapshot is not None and account != self.account:
            remaining = self.snapshot.captured_at+self.rules.duration-time.monotonic()
            if remaining > 0.:
                return self.origin.position, remaining
        return None

    def recon_snapshot(self, entity, battle_id, sequence):
        if self.entity == entity:
            return ReconSnapshot(battle_id, sequence, False, self.markers)
        return None

    def accept(self, command):
        if command.account_id != self.account or command.action == 'sandbox':
            return False
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
        elif command.action == 'fire' and self.fire is None:
            self.next_action += 1
            self.fire = self.next_action
        elif command.action == 'consumable' and args[0] == self.entity:
            ident, extra = args[1:]
            if not extra or ident in big_boss.GRAVITON_INPUT_IDS and len(extra) == 36:
                self.request_ability(ident)
        return True

    def request_ability(self, ident=None):
        if self.ability is not None or self.snapshot is None:
            return
        installed = tuple(slot.spec.ident for slot in self.snapshot.consumables
                          if slot.spec.ident in ABILITY_RECIPES)
        if ident is None:
            ident = installed[0] if installed else None
        elif ident not in installed and ident in big_boss.ROLE_INPUT_IDS:
            ident = next((slot for slot in installed if slot in big_boss.ROLE_INPUT_IDS), None)
        if ident in installed:
            self.next_action += 1
            self.ability = (self.next_action, ident)

    def tick(self, now):
        worker = self.worker
        if worker is None:
            return
        if self.retiring:
            if worker.advance_close() and self.rearm:
                self.retiring = self.rearm = False
                self.worker = None
                try:
                    self._warm()
                except (OSError, RuntimeError) as error:
                    self.arena.emit('time_stop_worker_error', reason=str(error))
            return
        reply = worker.poll()
        if isinstance(reply, Prepared):
            self.ready = True
        elif isinstance(reply, Failure):
            self.arena.emit('time_stop_worker_error', self.entity or 0, reason=reply.reason)
            self.abort()
            # A failed worker is never rearmed on each tick.
            return
        elif isinstance(reply, Frame):
            previous = self.frame
            if self.snapshot is not None and reply.ledger is None:
                self._present_abilities(reply, previous)
            if self.snapshot is not None and previous is not None:
                if reply.energy != previous.energy:
                    self.arena.emit('time_stop_energy', self.entity, energy=reply.energy)
                if reply.shots_fired > previous.shots_fired:
                    self.arena.emit('time_stop_shot', self.entity, shot=reply.last_shot,
                                    ammo=reply.ammo, clip=reply.clip, remaining=reply.reload_remaining)
                elif previous.reload_remaining and not reply.reload_remaining:
                    self.arena.emit('time_stop_reload', self.entity)
                for shot, point, explosion in reply.shot_ends:
                    self.arena.emit('time_stop_shot_end', self.entity, shot=shot,
                                    point=point, explosion=explosion)
            self.frame = reply
        if self.snapshot is None:
            return
        if self.frame is not None and self.frame.ledger is not None:
            outcome = merge_frame(self.arena, self.snapshot, self.frame, rules=self.rules)
            if self.frame.ledger.aborted:
                self.abort()
                return
            if outcome is not None:
                self._end(refund=outcome.refund, aborted=False)
                self.retiring = self.rearm = True
                worker.advance_close()
            return
        if self.frame is None:
            return
        owner = self.arena.vehicles[self.entity]
        assets = self.arena.world.assets_for(self.entity)
        flags = self.flags if now-self.last_input <= self.arena.CONTROL_INPUT_TIMEOUT else 0
        direction = int(bool(flags & 1))-int(bool(flags & 2))
        turn = int(bool(flags & 8))-int(bool(flags & 4))
        distance = direction * (assets.forward_speed if direction > 0 else assets.reverse_speed)
        fire = None
        if self.fire is not None and (shell := self.arena.shell_for(owner)) is not None:
            weapon = owner.definition.weapon if owner.definition is not None else None
            cooldown = self.arena._reload_duration(owner, 'reload', weapon.reload if weapon else 6.)
            fire = (self.fire, shell, cooldown)
        if worker.submit(Step(distance*self.arena.STEP, turn*assets.rotation_speed*self.arena.STEP,
                              aim=self.aim, point=self.point, fire=fire, ability=self.ability,
                              finish=self.finishing)):
            self.aim = self.point = None
            self.ability = self.fire = None

    def _present_abilities(self, frame, previous):
        # Clients count down each nominal cooldown locally. Refresh state
        # transitions and the immediately accepted activation.
        previous_states = {ident: view.state for ident, view in previous.ability_views} if previous else {}
        for ident, view in frame.ability_views:
            if previous_states.get(ident) != view.state:
                self.arena.emit('time_stop_consumable', self.entity, compact_id=ident,
                                state=view.state, remaining=view.remaining, total=view.total)
        if frame.abilities_used > (previous.abilities_used if previous else 0):
            kind, level, compact_id = frame.last_ability
            self.arena.emit('time_stop_ability', self.entity, kind_name=kind, level=level, compact_id=compact_id)
        if frame.traps != (previous.traps if previous else ()):
            self.arena.emit('time_stop_traps', self.entity, traps=frame.traps)
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
        arena.emit('time_stop_end', self.entity, position=arena.vehicles[self.entity].pose.position,
                   refund=refund, aborted=aborted, retired_traces=retired, live_traces=live,
                   missed_destructions=missed_destructions)
        self.snapshot = self.frame = self.account = None

    def abort(self):
        self.ready = False
        self.retiring = True
        self.rearm = False
        if self.snapshot is not None:
            entity = self.entity
            if self.frame is not None:
                for shot, point, _ in self.frame.projectiles:
                    self.arena.emit('time_stop_shot_end', entity, shot=shot, point=point, explosion=None)
            origin = next(tank.body.pose for tank in self.snapshot.tanks
                          if tank.identity == self.snapshot.owner)
            world = self.arena.world
            # The pre-fork pose also remains the last-resort placement if
            # every alternative has become occupied in the meantime.
            pose = safe_return(world, entity, origin, origin) or origin
            world.relocate_training_body(entity, pose)
            world.set_tracked_presence(entity, True)
            self.arena.vehicles[entity].pose = pose
            if self.snapshot.heal_wave is not None and self.arena.big_boss is not None:
                due, count, level = self.snapshot.heal_wave
                self.arena.big_boss.heal_waves[entity] = (self.arena.simulation_time+due, count, level)
            self.snapshot.restore_owner_status(self.arena)
            self._end(refund=0, aborted=True)
        if self.worker is not None:
            return self.worker.advance_close()
        return True
