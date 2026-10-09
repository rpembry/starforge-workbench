import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {validateConfig, assignmentFor, monitorFor} from './placement-core.js';

export default class FavoritePlacement extends Extension {
    enable() {
        this._pending = new Set();
        this._placed = new WeakSet();
        this._created = global.display.connect('window-created', (_display, window) => this._queue(window));
        this._service = Gio.DBusExportedObject.wrapJSObject(`<node>
          <interface name="org.starforge.Workbench.FavoritePlacement">
            <method name="Snapshot"><arg type="s" direction="out"/></method>
            <method name="PlaceConfigured"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
          </interface></node>`, this);
        this._service.export(Gio.DBus.session, '/org/starforge/Workbench/FavoritePlacement');
        // Existing windows are preserved on enable; placement is for new windows.
    }

    disable() {
        this._service?.unexport();
        this._service = null;
        if (this._created) global.display.disconnect(this._created);
        for (const source of this._pending) GLib.source_remove(source);
        this._pending.clear();
        this._created = null;
    }

    Snapshot() {
        if (Main.screenShield?.locked !== false) return '[]';
        return JSON.stringify(global.display.list_all_windows().filter(w =>
            w.get_window_type() === Meta.WindowType.NORMAL).map(w => ({
                classes: [w.get_wm_class(), w.get_wm_class_instance(), w.get_gtk_application_id()],
                title: w.get_title(), monitor: w.get_monitor()})));
    }

    PlaceConfigured(name) {
        // Explicit manual placement can include existing windows. No activation,
        // workspace changes, resizing, or application launch is performed here.
        this._placed = new WeakSet();
        for (const window of global.display.list_all_windows()) this._place(window, name);
        return this.Snapshot();
    }

    _queue(window) {
        // Clients often publish their identity after window-created. Bounded retries
        // stop after success so later manual moves remain where the user put them.
        for (const delay of [250, 1000, 3000, 10000]) {
            let source;
            source = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
                this._pending.delete(source);
                try { this._place(window); } catch (error) { console.error(error); }
                return GLib.SOURCE_REMOVE;
            });
            this._pending.add(source);
        }
    }

    _place(window, selectedName = null) {
        if (this._placed.has(window) || Main.sessionMode.currentMode !== 'user' ||
            Main.screenShield?.locked !== false || window.get_window_type() !== Meta.WindowType.NORMAL)
            return;
        const file = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_config_dir(),
            'starforge-ai-workbench', 'favorite-placement.json']));
        const [ok, bytes] = file.load_contents(null);
        if (!ok) return;
        const config = validateConfig(JSON.parse(new TextDecoder().decode(bytes)));
        const row = assignmentFor(config, {classes: [window.get_wm_class(),
            window.get_wm_class_instance(), window.get_gtk_application_id()].filter(Boolean),
            title: window.get_title()});
        if (!row || (selectedName !== null && row.name !== selectedName)) return;
        const manager = global.backend.get_monitor_manager();
        const monitors = (manager.get_monitors() ?? []).map(m => ({connector: m.get_connector(),
            serial: m.get_serial(), index: manager.get_monitor_for_connector(m.get_connector())}))
            .filter(m => m.index >= 0);
        const index = monitorFor(config.monitors[row.monitor], monitors);
        if (index === null) return;
        if (window.get_monitor() !== index) window.move_to_monitor(index);
        this._placed.add(window);
    }
}
