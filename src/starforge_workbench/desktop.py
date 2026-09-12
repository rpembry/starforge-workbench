"""Host GI bridge. Route only to a validated, isolated Ptyxis process."""
import json
import sys
import time
from pathlib import Path
import gi
from gi.repository import Gio, GLib

APP_PATH = "/org/starforge/AIWorkbench"

def connection():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)

def call(bus, owner, path, interface, method, args=None):
    return bus.call_sync(owner, path, interface, method, args, None,
                         Gio.DBusCallFlags.NONE, 5000, None).unpack()

def owner_for_pid(bus, pid):
    for owner in call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                      'org.freedesktop.DBus', 'ListNames')[0]:
        if not owner.startswith(':'):
            continue
        try:
            actual = call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                          'org.freedesktop.DBus', 'GetConnectionUnixProcessID',
                          GLib.Variant('(s)', (owner,)))[0]
            if actual == pid:
                xml = call(bus, owner, APP_PATH, 'org.freedesktop.DBus.Introspectable', 'Introspect')[0]
                if 'org.gtk.Application' in xml:
                    return owner
        except GLib.Error:
            continue
    raise ValueError('Owned Ptyxis instance not available')

def prepare(data):
    # Explicit private backend prevents any writes to the user's dconf database.
    root = Path(data['config'])/'glib-2.0/settings'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backend = Gio.keyfile_settings_backend_new(str(root/'keyfile'), '/', None)
    source = Gio.Settings.new('org.gnome.Ptyxis')
    schema = Gio.SettingsSchemaSource.get_default()
    dest = Gio.Settings.new_full(schema.lookup('org.gnome.Ptyxis', True), backend, None)
    for key in source.props.settings_schema.list_keys():
        dest.set_value(key, source.get_value(key))
    dest.set_boolean('restore-session', False)
    dest.set_boolean('restore-window-size', False)
    dest.set_uint('default-columns', data['columns'])
    dest.set_uint('default-rows', data['rows'])
    profile = source.get_string('default-profile-uuid')
    profile_schema = schema.lookup('org.gnome.Ptyxis.Profile', True)
    source_profile = Gio.Settings.new_with_path('org.gnome.Ptyxis.Profile', '/org/gnome/Ptyxis/Profiles/'+profile+'/')
    boot = 'starforge-bootstrap'
    for identity in [profile, boot]:
        target = Gio.Settings.new_full(profile_schema, backend, '/org/gnome/Ptyxis/Profiles/'+identity+'/')
        for key in profile_schema.list_keys():
            target.set_value(key, source_profile.get_value(key))
        target.set_boolean('use-custom-command', identity == boot)
        if identity == boot:
            target.set_string('custom-command', data['command'])
    dest.set_strv('profile-uuids', [profile, boot])
    Gio.Settings.sync()
    return {'profile': boot}

def focus(title, measure_pid=None):
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
    def tabs(node, depth=0):
        if depth > 25 or node.get_role_name() in ('terminal', 'text', 'entry'):
            return []
        if node.get_role_name() == 'page tab':
            return [node.get_name()]
        result = []
        for child in node:
            result.extend(tabs(child, depth+1))
        return result
    matches = []
    for app in Atspi.get_desktop(0):
        if measure_pid and app.get_process_id() == measure_pid:
            window = app.get_child_at_index(0)
            rect = window.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
            return {'width': rect.width, 'height': rect.height}
        if app.get_name() != 'ptyxis' or measure_pid:
            continue
        for window in app:
            names = tabs(window)
            if title in names:
                matches.append((window, names))
    if len(matches) != 1:
        raise ValueError('Cannot uniquely locate existing tab '+title+'; switch to it manually. No duplicate started.')
    window, names = matches[0]
    action = window.get_action_iface()
    actions = {action.get_action_name(i): i for i in range(action.get_n_actions())}
    for _ in range(len(names)+1):
        if window.get_name() == title:
            return {'title': title, 'selected': True}
        if not action.do_action(actions['page.next']):
            break
        time.sleep(.08)
    raise ValueError('Could not select existing tab; no duplicate started')

def main():
    data = json.loads(sys.argv[1])
    if data['action'] == 'prepare':
        print(json.dumps(prepare(data)))
        return
    if data['action'] == 'geometry':
        print(json.dumps(focus('', data['pid'])))
        return
    if data['action'] == 'focus':
        print(json.dumps(focus(data['title'])))
        return
    bus = connection()
    owner = owner_for_pid(bus, data['pid'])
    if data['action'] == 'tab':
        args = [b'ptyxis\0']
        options = {'tab': GLib.Variant('b', True), 'execute': GLib.Variant('s', data['command']),
                   'title': GLib.Variant('s', data['title']),
                   'working-directory': GLib.Variant('ay', data['cwd'].encode()+b'\0')}
        platform = {'cwd': GLib.Variant('ay', data['cwd'].encode() + b'\0'),
                    'options': GLib.Variant('a{sv}', options)}
        result = call(bus, owner, APP_PATH, 'org.gtk.Application', 'CommandLine',
                      GLib.Variant('(oaaya{sv})', ('/org/starforge/CommandLine', args, platform)))
        if result != (0,):
            raise ValueError('Ptyxis rejected tab request')
    print(json.dumps({'owner': owner}))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, GLib.Error) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
