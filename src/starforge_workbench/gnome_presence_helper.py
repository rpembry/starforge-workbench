"""System-Python GObject bridge; emits only a verified state as JSON."""
import json
import sys
import time

from gi.repository import Gio, GLib


BUS = 'org.starforge.Workbench.FavoritePresence'
PATH = '/org/starforge/Workbench/FavoritePresence'
IFACE = BUS


def call(bus, destination, path, interface, method, parameters=None):
    return bus.call_sync(destination, path, interface, method, parameters,
                         None, Gio.DBusCallFlags.NONE, 1500, None).unpack()


def snapshot(desktop_id):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    owner = call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                 'org.freedesktop.DBus', 'GetNameOwner', GLib.Variant('(s)', (BUS,)))[0]
    shell_owner = call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                       'org.freedesktop.DBus', 'GetNameOwner',
                       GLib.Variant('(s)', ('org.gnome.Shell',)))[0]
    # The service is trusted only when the Shell process itself owns it.
    for name in (owner, shell_owner):
        if not name.startswith(':'):
            raise ValueError('Invalid bus owner')
    get_pid = lambda name: call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                'org.freedesktop.DBus', 'GetConnectionUnixProcessID',
                                GLib.Variant('(s)', (name,)))[0]
    if get_pid(owner) != get_pid(shell_owner):
        raise ValueError('Presence service is not owned by GNOME Shell')
    settings = Gio.Settings.new('org.gnome.shell.extensions.starforge-favorite-presence')
    matches = [row for row in settings.get_value('favorites').deep_unpack()
               if row[1] == desktop_id]
    if len(matches) != 1:
        raise ValueError('Desktop ID is not uniquely allowlisted')
    key, _, kind = matches[0]
    if kind != 'native':
        return 'unknown'
    observed, rows = call(bus, BUS, PATH, IFACE, 'Snapshot')
    if observed > GLib.get_monotonic_time() or GLib.get_monotonic_time() - observed > 2_000_000:
        raise ValueError('Stale presence sample')
    found = [row for row in rows if row[0] == key]
    if len(found) != 1:
        raise ValueError('Missing or duplicate snapshot key')
    _, state, confidence, count = found[0]
    if state == 'present' and confidence == 'shell-app-association' and count > 0:
        return 'present'
    if state == 'absent' and confidence == 'window-inventory' and count == 0:
        return 'absent'
    return 'unknown'


if __name__ == '__main__':
    try:
        state = snapshot(sys.argv[1])
    except (Exception, IndexError):
        state = 'unknown'
    print(json.dumps({'state': state}))
