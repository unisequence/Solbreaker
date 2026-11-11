"""Applied progression rules between the persistent garage and the research catalog.

Maps real XML research data onto GarageStore transactions: grants, purchases,
module research, module installation and the sandbox XP rule. No native wire
fields; callers decide who may mutate and when.
"""
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

from .dvpl import read
from .garage import GarageStore
from .tech_tree import load_tech_tree, lookup_vehicle, load_inventory_catalog
from .vehicle_data import VehicleConfiguration, load_ammunition, load_vehicle, overlay, resolve_gun

KINDS = dict(vehicle=1, chassis=2, turret=3, gun=4, engine=5, fuel_tank=6, radio=7)
SHELL_KIND = 10
_MODULE_SLOTS = ('chassis', 'turret', 'gun', 'engine', 'fuel_tank', 'radio')
_CATALOGS = dict(chassis='chassis', turret='turrets', gun='guns', engine='engines',
                 fuel_tank='fuelTanks', radio='radios')
_LOADABLE_KINDS = ('ARMOR_PIERCING', 'ARMOR_PIERCING_CR', 'HOLLOW_CHARGE', 'HIGH_EXPLOSIVE')
_DESCRIPTOR = struct.Struct('<2B6HB')


def item_cd(kind, ident, nation_id):
    """Typed compact descriptor: (id<<8)|(nation_id<<4)|kind; kind is a name or number."""
    number = KINDS[kind] if isinstance(kind, str) and kind in KINDS else kind
    if type(number) is not int or number not in KINDS.values():
        raise ValueError('Unknown item kind')
    if type(ident) is not int or not 0 <= ident < 1 << 16:
        raise ValueError('Invalid item id')
    if type(nation_id) is not int or not 0 <= nation_id < 16:
        raise ValueError('Invalid nation id')
    return (ident << 8) | (nation_id << 4) | number


def _root(game):
    return Path(game) / 'Data/XML/item_defs/vehicles'


@lru_cache(maxsize=4)
def _nations(game):
    root = ET.fromstring(read(_root(game) / 'common/nations.xml.dvpl'))
    return {int(n.findtext('id')): n.tag for n in root if (n.findtext('id') or '').isdigit()}


@lru_cache(maxsize=16)
def _shells(game, nation):
    return ET.fromstring(read(_root(game) / nation / 'components/shells.xml.dvpl'))


def _installed_cds(definition):
    nation = definition.nation_id
    return sorted({item_cd('vehicle', definition.ident, nation)} | {
        item_cd(slot, getattr(definition, slot if slot != 'fuel_tank' else 'fuel').ident, nation)
        for slot in _MODULE_SLOTS})


def descriptor_configuration(game, descriptor):
    """(research, shell-less configuration, raw) from a stored descriptor's module IDs only.

    Unlike ProgressionService._configuration it needs no store and ignores the
    shell vector, so read-only tools can decode rows whose shell IDs are stale.
    """
    game = Path(game)
    if not isinstance(descriptor, str):
        raise ValueError('Invalid vehicle descriptor')
    try:
        raw = bytes.fromhex(descriptor)
    except ValueError:
        raise ValueError('Invalid vehicle descriptor') from None
    if len(raw) != _DESCRIPTOR.size or raw[0] & 0xF != KINDS['vehicle'] or raw[-1] != 0:
        raise ValueError('Invalid vehicle descriptor')
    head, ident, chassis, engine, fuel, radio, turret, gun, _ = _DESCRIPTOR.unpack(raw)
    nation = _nations(game).get(head >> 4)
    if nation is None:
        raise ValueError('Unknown nation')
    type_cd = (ident << 8) | head
    research = next((v for v in load_tech_tree(game, nation) if v.type_cd == type_cd), None)
    if research is None:
        raise ValueError('Unknown vehicle')
    ids = dict(chassis=chassis, turret=turret, gun=gun, engine=engine, fuel_tank=fuel, radio=radio)
    tags = {}
    for slot, value in ids.items():
        tag = next((m.tag for m in research.modules if m.slot == slot and m.ident == value), None)
        if tag is None:
            raise ValueError(f'Descriptor module is not part of the vehicle: {slot}/{value}')
        tags[slot] = tag
    return research, VehicleConfiguration(**tags), raw


@lru_cache(maxsize=256)
def _ap_shells(game, nation, name, turret, gun):
    """Loadable shot tags of one gun in declared order, resolved as the loader does."""
    root = _root(game)
    shells = _shells(game, nation)
    vehicle = ET.fromstring(read(root / nation / (name + '.xml.dvpl')))
    local_turret = vehicle.find('turrets0/' + turret)
    if local_turret is None:
        raise ValueError(f'Incompatible vehicle component: turret/{turret}')
    catalogs = {}

    def resolve(kind, local):
        shared = None
        if (local.text or '').strip() == 'shared':
            if kind not in catalogs:
                catalogs[kind] = ET.fromstring(read(root / nation / 'components' / (kind + '.xml.dvpl')))
            shared = catalogs[kind].find('shared/' + local.tag)
            if shared is None:
                raise ValueError(f'Missing shared component: {kind}/{local.tag}')
        return resolve_gun(shared, local) if kind == 'guns' else overlay(shared, local)
    node = resolve('turrets', local_turret)
    local_gun = node.find('guns/' + gun)
    if local_gun is None:
        raise ValueError(f'Incompatible vehicle component: gun/{gun}')
    resolved = resolve('guns', local_gun)
    shots = resolved.find('shots')
    found = []
    for shot in (shots if shots is not None else ()):
        source = shells.find(shot.tag)
        if source is None:
            continue
        if shot.find('shell') is not None:
            source = overlay(source, shot.find('shell'))
        if source.findtext('kind') in _LOADABLE_KINDS:
            found.append(shot.tag)
    return tuple(found)


class ProgressionService:
    def __init__(self, store, game, default_definition=None, *, grant_all=False):
        self.store = store
        self.game = Path(game)
        self.default_definition = default_definition
        self.grant_all = grant_all
        self._catalog_entries = None
        self.garage = GarageStore(store)

    # -- catalog helpers -----------------------------------------------
    def nation_name(self, nation_id):
        try:
            return _nations(self.game)[nation_id]
        except KeyError:
            raise ValueError('Unknown nation') from None

    def resolve_type(self, type_cd):
        if type(type_cd) is not int or type_cd < 0 or type_cd & 0xF != KINDS['vehicle']:
            raise ValueError('Not a vehicle type_cd')
        nation = self.nation_name((type_cd >> 4) & 0xF)
        for vehicle in load_tech_tree(self.game, nation):
            if vehicle.type_cd == type_cd:
                return vehicle
        raise ValueError('Unknown vehicle')

    def _research_of(self, vehicle_key):
        parts = vehicle_key.split('/') if isinstance(vehicle_key, str) else ()
        if len(parts) != 2:
            raise ValueError('vehicle_key must be nation/name')
        return lookup_vehicle(self.game, *parts)

    def _shell_tag(self, nation, nation_id, compact_id):
        if type(compact_id) is not int or compact_id & 0xF != SHELL_KIND \
                or (compact_id >> 4) & 0xF != nation_id:
            raise ValueError('Shell does not belong to the vehicle nation')
        ident = str(compact_id >> 8)
        for shell in _shells(self.game, nation):
            if shell.findtext('id') == ident:
                return shell.tag
        raise ValueError('Unknown shell')

    def _configuration(self, descriptor, shells):
        """Decode installed IDs without loading collision or ballistic geometry."""
        research, base, raw = descriptor_configuration(self.game, descriptor)
        if not shells:
            raise ValueError('Vehicle has no shell')
        nation_id = raw[0] >> 4
        nation = self.nation_name(nation_id)
        configuration = VehicleConfiguration(
            shell=self._shell_tag(nation, nation_id, shells[0]),
            **{slot: getattr(base, slot) for slot in
               ('chassis', 'turret', 'gun', 'engine', 'fuel_tank', 'radio')})
        return research, configuration, raw

    def _decode(self, descriptor, shells):
        """Definition for a stored descriptor and supported shell; reproduce the descriptor."""
        return self._decode_with_loader(descriptor, shells, load_vehicle)

    @staticmethod
    def _require_battle_ready(definition):
        """Reject controller layouts whose state transition is not simulated.

        Collision can query an immutable snapshot for a validated
        ``MovableArmor`` vehicle, but the arena does not yet evaluate XML
        health conditions and publish the corresponding state transition.
        Admitting such a vehicle would freeze its plates at an arbitrary
        state, so only default-state controllers are battle-ready for now.
        """
        controller = definition.armor_states_controller
        if controller is not None and any(state.kind == 'health' for state in controller.states):
            raise ValueError('Health-triggered movable armor is not battle-ready')
        return definition

    def _decode_with_loader(self, descriptor, shells, loader):
        """Shared descriptor reproduction with an explicit definition loader."""
        research, configuration, raw = self._configuration(descriptor,shells)
        # Garage slots follow XML order, which can put HE before kinetic shells.
        # The battle loader's default shell is independent of that wire ordering.
        fallback = None
        for cd in shells[::2]:
            tag = self._shell_tag(research.nation,research.nation_id,cd)
            kind = _shells(self.game,research.nation).findtext(tag+'/kind')
            if kind=='HIGH_EXPLOSIVE':
                fallback = fallback or tag
            elif kind in _LOADABLE_KINDS:
                configuration = replace(configuration,shell=tag)
                break
        else:
            if fallback:configuration = replace(configuration,shell=fallback)
        definition = loader(self.game, research.nation, research.name, configuration)
        if definition.descriptor != raw:
            raise ValueError('Stored vehicle does not match its resources')
        return definition

    def battle_definition(self, descriptor, shells):
        """Battle-only definition for a stored garage row.

        Garage, catalog and editor flows intentionally continue through
        :meth:`_decode`.  This route is used only while constructing an arena,
        where the collision geometry guard in ``load_battle_vehicle`` applies.
        """
        from .armor import load_battle_vehicle
        return self._require_battle_ready(
            self._decode_with_loader(descriptor, shells, load_battle_vehicle))

    def definition_for(self, type_cd, configuration=None):
        research = self.resolve_type(type_cd)
        if configuration is None:
            configuration = research.stock
        elif isinstance(configuration, dict):
            configuration = VehicleConfiguration.parse(configuration)
        elif not isinstance(configuration, VehicleConfiguration):
            raise ValueError('Expected VehicleConfiguration')
        if configuration.shell is None:
            if configuration.gun not in (None, research.stock.gun):
                raise ValueError('Explicit configuration needs a supported shell for a non-stock gun')
            configuration = replace(configuration, shell=research.stock.shell)
        if configuration.shell is None:
            raise ValueError('Vehicle has no supported shell')
        return load_vehicle(self.game, research.nation, research.name, configuration)

    def selected_definition(self, account_id):
        return self._vehicle_definition(self.garage.snapshot(account_id), None)[1]

    def selected_inventory(self, account_id):
        """Stored native garage record; selection/room UI needs no battle loader."""
        snapshot = self.prepare(account_id)
        return self._owned_vehicle(snapshot, snapshot['selected_id'])

    @staticmethod
    def _owned_vehicle(snapshot, inventory_id):
        if type(inventory_id) is not int or inventory_id <= 0:
            raise ValueError('inventory_id must be a positive integer')
        vehicle = next((v for v in snapshot['vehicles'] if v['id'] == inventory_id), None)
        if vehicle is None:
            raise ValueError('Vehicle is not in the garage')
        return vehicle

    def _vehicle_definition(self, snapshot, inventory_id):
        wanted = snapshot['selected_id'] if inventory_id is None else inventory_id
        vehicle = next((v for v in snapshot['vehicles'] if v['id'] == wanted), None)
        if vehicle is None:
            raise ValueError('Vehicle is not in the garage')
        return vehicle, self._decode(vehicle['descriptor'], vehicle['shells'])

    # -- state ---------------------------------------------------------
    def prepare(self, account_id):
        inventory = self.store.inventory(account_id, self.game, self.default_definition)
        if not self.garage._initialized(account_id):
            definition = self._decode(inventory['descriptor'], inventory['shells'])
            self.garage.ensure(account_id, researched=_installed_cds(definition))
        if self.grant_all:
            if self._catalog_entries is None:
                self._catalog_entries = tuple(dict(type_cd=v.type_cd, inventory=v.inventory,
                    configuration=asdict(v.configuration), researched=v.researched)
                    for v in load_inventory_catalog(self.game))
            snapshot = self.garage.provision_catalog(account_id, self._catalog_entries)
        else:
            snapshot = self.garage.snapshot(account_id)
        selected = self._owned_vehicle(snapshot,snapshot['selected_id'])
        return self._normalize_ammunition(account_id,selected)

    snapshot = prepare

    # -- actions -------------------------------------------------------
    def grant(self, account_id, vehicle_key, configuration=None, operation_id=None):
        self.prepare(account_id)
        research = self._research_of(vehicle_key)
        definition = self.definition_for(research.type_cd, configuration)
        return self.garage.grant_vehicle(
            account_id, research.type_cd, definition.inventory,
            configuration=asdict(definition.configuration),
            researched=_installed_cds(definition), operation_id=operation_id)

    def grant_xp(self, account_id, amount, inventory_id=None, operation_id=None):
        self.prepare(account_id)
        return self.garage.grant_xp(account_id, amount, inventory_id=inventory_id,
                                    operation_id=operation_id)

    def select(self, account_id, inventory_id, operation_id=None):
        vehicle = self._owned_vehicle(self.prepare(account_id), inventory_id)
        self._normalize_ammunition(account_id,vehicle)
        return self.garage.select_vehicle(account_id, inventory_id, operation_id=operation_id)

    def _normalize_ammunition(self, account_id, vehicle):
        # The native three-row editor requires explicit zero-count entries.
        # Resolve only this vehicle's installed gun; never build battle assets.
        from .tech_tree import load_ammo_layout
        research, configuration, _ = self._configuration(vehicle['descriptor'],vehicle['shells'])
        ids, capacity = load_ammo_layout(self.game,research.nation,research.name,configuration)
        return self.garage.set_ammo_loadout(account_id,vehicle['id'],vehicle['shells'],
                                           allowed_shells=ids,capacity=capacity)

    def set_ammo(self, account_id, inventory_id, shell_cd, count, operation_id=None):
        """Set the owned vehicle's AP loadout quantity, bounded by its gun capacity."""
        snapshot = self.prepare(account_id)
        _, definition = self._vehicle_definition(snapshot, inventory_id)
        return self.garage.set_ammo(account_id, inventory_id, shell_cd, count,
                                    capacity=definition.weapon.ammo, operation_id=operation_id)

    def set_ammo_loadout(self, account_id, inventory_id, pairs, operation_id=None):
        """Store compatible quantities of implemented shell kinds."""
        snapshot = self.prepare(account_id)
        vehicle, definition = self._vehicle_definition(snapshot, inventory_id)
        research = self.resolve_type(vehicle['type_cd'])
        specs = load_ammunition(self.game, research.nation, research.name, definition.configuration)
        flat = self.garage._shell_ids(pairs, 'pairs', 64, distinct=False)
        kinds = {spec.compact_id: spec.kind for spec in specs}
        for cd, count in zip(flat[::2], flat[1::2]):
            if count and kinds.get(cd) not in _LOADABLE_KINDS:
                raise ValueError('Unsupported ammunition kind' if cd in kinds
                                 else 'Shell is not compatible with the vehicle')
        ordered = [spec.compact_id for spec in specs]
        return self.garage.set_ammo_loadout(
            account_id, inventory_id, flat, allowed_shells=ordered,
            capacity=definition.weapon.ammo, operation_id=operation_id)

    def exchange_ammo_loadout(self, account_id, inventory_id, signed_pairs, operation_id=None):
        """Native cmd109 ammunition exchange at the installed gun's XML price.

        Positive CDs pay a premium shell in gold, negative CDs pay gold * AMMO_EXCHANGE_RATE
        credits; credit and zero-priced shells ignore the sign. Unloading refunds the shell's
        sale value in credits (premium: gold * rate, then SELL_PRICE_FACTOR).
        """
        vehicle = self._owned_vehicle(self.prepare(account_id), inventory_id)
        terms, kinds = self._ammo_terms(vehicle['descriptor'], vehicle['shells'])
        pairs = self.garage._signed_pairs(signed_pairs)
        for signed, count in pairs:
            if count and kinds.get(abs(signed)) not in _LOADABLE_KINDS:
                raise ValueError('Unsupported ammunition kind' if abs(signed) in kinds
                                 else 'Shell is not compatible with the vehicle')
        return self.garage.exchange_ammo_loadout(
            account_id, vehicle['id'], [v for pair in pairs for v in pair],
            operation_id=operation_id, **terms)

    def _ammo_terms(self, descriptor, shells):
        """One ammo-only source of authoritative layout, prices and typed module keys."""
        from .catalog import AMMO_EXCHANGE_RATE, sale_credits
        from .tech_tree import load_ammo_layout
        research, configuration, raw = self._configuration(descriptor, shells)
        configuration = replace(configuration, shell=None)
        ids, capacity = load_ammo_layout(self.game, research.nation, research.name, configuration)
        specs = load_ammunition(self.game, research.nation, research.name, configuration)
        if tuple(spec.compact_id for spec in specs) != tuple(ids):
            raise ValueError('Ammunition resources disagree on the gun shells')
        rate = AMMO_EXCHANGE_RATE
        if type(rate) is not int or rate <= 0:
            raise ValueError('Invalid ammunition exchange rate')
        prices = {}
        for spec in specs:
            # The native ammo editor reads baseMoney from the installed gun's
            # shell descriptor; cmd300 supplies priceInfo/discount metadata.
            credits, gold = spec.price_credits, spec.price_gold
            if type(credits) is not int or type(gold) is not int or credits < 0 or gold < 0 or credits and gold:
                raise ValueError('Invalid shell price')
            if gold:
                prices[spec.compact_id] = ((0, gold), (gold * rate, 0), sale_credits(gold * rate))
            else:
                prices[spec.compact_id] = ((credits, 0), (credits, 0), sale_credits(credits))
        _, _, _, _, _, _, turret, gun, _ = _DESCRIPTOR.unpack(raw)
        key = (item_cd('turret', turret, research.nation_id), item_cd('gun', gun, research.nation_id))
        return (dict(allowed_shells=list(ids), capacity=capacity, prices=prices,
                     layout_key=key, expected_descriptor=descriptor),
                {spec.compact_id: spec.kind for spec in specs})

    def battle_ammunition(self, vehicle):
        """Prepare settlement from immutable entry counts and actual live remaining rounds."""
        if vehicle.loadout_shells is None:
            raise ValueError('Battle vehicle has no persistent ammunition snapshot')
        initial = list(vehicle.loadout_shells)
        terms, _ = self._ammo_terms(vehicle.descriptor.hex(), initial)
        remaining = dict(vehicle.ammo_by_shell)
        remaining[vehicle.shell_id] = vehicle.ammo
        return dict(terms, initial=initial, remaining=remaining)

    def set_ammo_setting(self, account_id, inventory_id, setting, enabled, operation_id=None):
        """Native per-vehicle settings flag, e.g. garage.AMMO_AUTO_RESUPPLY; other flags kept."""
        vehicle = self._owned_vehicle(self.prepare(account_id), inventory_id)
        seed = {}
        if enabled and setting == 4:
            terms, _ = self._ammo_terms(vehicle['descriptor'], vehicle['shells'])
            seed = {k:terms[k] for k in ('layout_key', 'allowed_shells', 'expected_descriptor')}
        return self.garage.set_ammo_setting(account_id, inventory_id, setting, enabled,
                                            operation_id=operation_id, **seed)

    def _target_cd(self, research, slot, tag):
        if slot == 'vehicle':
            return lookup_vehicle(self.game, research.nation, tag).type_cd
        module = next((m for m in research.modules if m.slot == slot and m.tag == tag), None)
        if module is None:
            raise ValueError(f'Unlock target is not a module of the vehicle: {slot}/{tag}')
        return item_cd(slot, module.ident, research.nation_id)

    def research(self, account_id, inventory_id, target_cd, allow_free_xp=True, operation_id=None):
        snapshot = self.prepare(account_id)
        vehicle = next((v for v in snapshot['vehicles'] if v['id'] == inventory_id), None)
        if vehicle is None:
            raise ValueError('Vehicle is not in the garage')
        research = self.resolve_type(vehicle['type_cd'])
        cost, parent = self._research_edge(research, set(snapshot['researched']), target_cd)
        return self.garage.research_item(
            account_id, target_cd, cost, inventory_id=inventory_id, prerequisites=(parent,),
            allow_free_xp=allow_free_xp, operation_id=operation_id)

    def research_index(self, account_id, source_type_cd, index, operation_id=None):
        """Native UnlockItemRequest selects an edge by index, not by target CD."""
        if type(index) is not int or index < 0:
            raise ValueError('Invalid research index')
        research = self.resolve_type(source_type_cd)
        if index >= len(research.native_unlocks):
            raise ValueError('Research index is unavailable for this vehicle')
        edge = research.native_unlocks[index]
        if edge is None:
            raise ValueError('Unlock requires a non-XP currency that is not supported')
        snapshot = self.prepare(account_id)
        vehicle = next((v for v in snapshot['vehicles'] if v['type_cd'] == source_type_cd), None)
        if vehicle is None:
            raise ValueError('The research source vehicle must be in your garage')
        target = self._target_cd(research, *edge.target)
        parent = (research.type_cd if edge.source is None
                  else self._target_cd(research, *edge.source))
        return self.garage.research_item(
            account_id, target, edge.xp, inventory_id=vehicle['id'],
            prerequisites=(parent,), operation_id=operation_id)

    def _research_edge(self, research, owned, target_cd):
        """Cheapest unlock edge of `target_cd` whose source is researched: (xp, parent)."""
        candidates = []
        for edge in research.unlocks:
            try:
                if self._target_cd(research, *edge.target) != target_cd:
                    continue
                parent = (research.type_cd if edge.source is None
                          else self._target_cd(research, *edge.source))
            except ValueError:
                continue
            candidates.append((edge.xp, parent))
        if not candidates:
            if any(self._unsupported_matches(research, entry, target_cd)
                   for entry in research.unsupported_unlocks):
                raise ValueError('Unlock requires a non-XP currency that is not supported')
            raise ValueError('Target is not unlocked by this vehicle')
        open_edges = [c for c in candidates if c[1] in owned]
        if not open_edges:
            raise ValueError('Prerequisites are not researched')
        return min(open_edges)

    def _unsupported_matches(self, research, entry, target_cd):
        kind, tag, _ = entry.split(':', 2)
        slot = {'fuelTank': 'fuel_tank'}.get(kind, kind)
        try:
            return self._target_cd(research, slot, tag) == target_cd
        except ValueError:
            return False

    def buy(self, account_id, vehicle_key, operation_id=None):
        self.prepare(account_id)
        research = self._research_of(vehicle_key)
        if research.credits is None or research.gold is not None:
            raise ValueError('Vehicle is not purchasable for credits')
        definition = self.definition_for(research.type_cd)
        return self.garage.buy_vehicle(
            account_id, research.type_cd, definition.inventory, research.credits,
            configuration=asdict(definition.configuration),
            researched=_installed_cds(definition), operation_id=operation_id)

    def sell(self, account_id, inventory_id, operation_id=None):
        snapshot = self.prepare(account_id)
        vehicle = self._owned_vehicle(snapshot, inventory_id)
        research = self.resolve_type(vehicle['type_cd'])
        if research.credits is None or research.gold is not None:
            raise ValueError('Only ordinary credit vehicles can be sold in this sandbox')
        from .catalog import vehicle_sale_credits
        return self.garage.sell_vehicle(
            account_id, inventory_id, vehicle_sale_credits(research.credits),
            expected_descriptor=vehicle['descriptor'], operation_id=operation_id)

    def _plan_install(self, snapshot, inventory_id, slot, tag):
        if slot not in _MODULE_SLOTS:
            raise ValueError('Unknown module slot')
        if inventory_id is not None and type(inventory_id) is not int:
            raise ValueError('inventory_id must be an integer')
        vehicle, current = self._vehicle_definition(snapshot, inventory_id)
        research = self.resolve_type(vehicle['type_cd'])
        module = next((m for m in research.modules if m.slot == slot and m.tag == tag), None)
        if module is None:
            raise ValueError('Module does not belong to the vehicle')
        if module.credits is None:
            raise ValueError('Module is not purchasable for credits')
        configuration = replace(current.configuration, **{slot: tag})
        if slot == 'gun':
            options = _ap_shells(self.game, research.nation, research.name,
                                 configuration.turret, tag)
            if configuration.shell not in options:
                if not options:
                    raise ValueError('Gun has no supported shell')
                configuration = replace(configuration, shell=options[0])
        try:
            definition = load_vehicle(self.game, research.nation, research.name, configuration)
        except ValueError as error:
            if slot == 'turret':
                raise ValueError(f'Turret is incompatible with the installed gun: {error}') from error
            raise
        installed = tag == getattr(current.configuration, slot)
        return dict(
            vehicle=vehicle, research=research, definition=definition,
            target_cd=item_cd(slot, module.ident, research.nation_id),
            credits_cost=0 if installed else module.credits,
            operation_request=dict(op='install_module', inventory_id=vehicle['id'], slot=slot, tag=tag))

    def install(self, account_id, inventory_id, slot, tag, operation_id=None):
        plan = self._plan_install(self.prepare(account_id), inventory_id, slot, tag)
        vehicle, definition = plan['vehicle'], plan['definition']
        return self.garage.install_configuration(
            account_id, vehicle['id'], definition.inventory,
            configuration=asdict(definition.configuration),
            required_research=(plan['target_cd'],),
            credits_cost=plan['credits_cost'], operation_id=operation_id,
            expected_descriptor=vehicle['descriptor'], operation_request=plan['operation_request'])

    def research_and_install(self, account_id, inventory_id, slot, tag, operation_id=None):
        """Research (if needed) and install a module atomically: one transaction, one revision."""
        snapshot = self.prepare(account_id)
        plan = self._plan_install(snapshot, inventory_id, slot, tag)
        vehicle, definition, target = plan['vehicle'], plan['definition'], plan['target_cd']
        if target in snapshot['researched']:
            cost, parents = 0, ()
        else:
            cost, parent = self._research_edge(plan['research'], set(snapshot['researched']), target)
            parents = (parent,)
        return self.garage.research_and_install(
            account_id, vehicle['id'], definition.inventory,
            configuration=asdict(definition.configuration), item_cd=target, xp_cost=cost,
            prerequisites=parents, credits_cost=plan['credits_cost'], operation_id=operation_id,
            expected_descriptor=vehicle['descriptor'],
            operation_request=dict(plan['operation_request'], op='research_and_install'))


def battle_xp(won, damage, *, tier=1, kills=0, survived=False):
    """Compatibility entry point for the shared local reward policy."""
    from .battle_rewards import calculate_rewards
    earned = calculate_rewards(won=won, damage=damage, tier=tier, kills=kills, survived=survived)
    return earned.vehicle_xp, earned.free_xp
