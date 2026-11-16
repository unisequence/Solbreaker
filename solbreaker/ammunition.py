"""Shared gun loading transitions for battle and isolated timelines."""


def switch_shell(vehicle, shell_id):
    """Select an available type, keeping the previous shell count in reserve."""
    if not shell_id or shell_id == vehicle.shell_id or vehicle.ammo_by_shell.get(shell_id, 0) <= 0:
        return False
    vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
    vehicle.selected_shell = shell_id
    vehicle.ammo = vehicle.ammo_by_shell[shell_id]
    if vehicle.pump_times:
        vehicle.clip = min(vehicle.clip, vehicle.ammo)
        if vehicle.ammo == vehicle.clip:
            vehicle.pump_at = vehicle.pump_total = 0.
            vehicle.pump_waiting = False
    return True


def finish_magazine_reload(vehicle):
    """Close a fire gate, loading rounds only after a full magazine reload."""
    vehicle.reload_at = 0.
    if vehicle.clip:
        return 0
    switched = apply_queued_shell(vehicle)
    vehicle.clip = min(vehicle.clip_size, vehicle.ammo)
    return switched


def apply_queued_shell(vehicle):
    pending, vehicle.next_shell = vehicle.next_shell, 0
    return pending if switch_shell(vehicle, pending) else 0


def apply_burst_shell_switch(vehicle):
    selected, vehicle.burst_switch_shell = vehicle.burst_switch_shell, 0
    if switch_shell(vehicle, selected):
        vehicle.clip = min(vehicle.clip, vehicle.ammo)
        return selected
    return 0


def spend_round(vehicle, unlimited):
    if not unlimited:
        vehicle.ammo -= 1
    vehicle.ammo_by_shell[vehicle.shell_id] = vehicle.ammo
    vehicle.clip -= 1


def magazine_reload_allowed(vehicle):
    return (0 < vehicle.clip < min(vehicle.clip_size, vehicle.ammo)
            and (not vehicle.reload_at or vehicle.reload_kind == 'interval'))


def start_pump_refill(vehicle, at, duration=None):
    vehicle.pump_waiting = False
    if vehicle.clip < len(vehicle.pump_times) and vehicle.ammo > vehicle.clip:
        nominal = vehicle.pump_times[vehicle.clip]
        vehicle.pump_total = nominal if duration is None else duration(nominal)
        vehicle.pump_at = at + vehicle.pump_total
    else:
        vehicle.pump_at = vehicle.pump_total = 0.


def advance_pump(vehicle, until, start_segment):
    """Yield each completed shell after scheduling the next refill."""
    if vehicle.pump_waiting and vehicle.reload_at and vehicle.reload_at <= until:
        start_segment(vehicle.reload_at)
    while vehicle.pump_at and vehicle.pump_at <= until:
        at, total = vehicle.pump_at, vehicle.pump_total
        vehicle.clip += 1
        start_segment(at)
        yield total
