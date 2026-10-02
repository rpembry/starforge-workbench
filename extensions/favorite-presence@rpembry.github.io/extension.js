import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {calculateSnapshot, normalizeAllowlist, unknownSnapshot} from './presence-core.js';

const BUS_NAME = 'org.starforge.Workbench.FavoritePresence';
const OBJECT_PATH = '/org/starforge/Workbench/FavoritePresence';
const INTERFACE_XML = `<node>
  <interface name="org.starforge.Workbench.FavoritePresence">
    <method name="Snapshot">
      <arg name="observed_monotonic_us" type="t" direction="out"/>
      <arg name="favorites" type="a(sssu)" direction="out"/>
    </method>
  </interface>
</node>`;

class PresenceService {
    constructor(settings) {
        this._settings = settings;
    }

    Snapshot() {
        const observed = GLib.get_monotonic_time();
        let rows;
        try {
            rows = normalizeAllowlist(this._settings.get_value('favorites').deepUnpack());
        } catch (_error) {
            // A missing or invalid private allowlist cannot establish absence.
            return [observed, []];
        }

        if (Main.sessionMode.currentMode !== 'user' || Main.screenShield?.locked !== false)
            return [observed, unknownSnapshot(rows)];

        try {
            const appSystem = Shell.AppSystem.get_default();
            const tracker = Shell.WindowTracker.get_default();
            const windows = global.display.list_all_windows();
            const windowAppIds = [];
            let unmappedWindowCount = 0;
            for (const window of windows) {
                const app = tracker.get_window_app(window);
                if (app)
                    windowAppIds.push(app.get_id());
                else
                    unmappedWindowCount++;
            }
            const knownAppIds = new Set(rows
                .filter(row => row.kind === 'native' && Boolean(appSystem.lookup_app(row.appId)))
                .map(row => row.appId));
            const startupPending = tracker.get_startup_sequences().length > 0;
            return [observed, calculateSnapshot(rows, {
                active: true,
                knownAppIds,
                windowAppIds,
                unmappedWindowCount,
                startupPending,
            })];
        } catch (_error) {
            return [observed, unknownSnapshot(rows)];
        }
    }
}

export default class FavoritePresenceExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._service = new PresenceService(this._settings);
        this._exported = null;
        this._ownerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            connection => {
                if (!this._service)
                    return;
                this._exported?.unexport();
                this._exported = Gio.DBusExportedObject.wrapJSObject(INTERFACE_XML, this._service);
                this._exported.export(connection, OBJECT_PATH);
            },
            null,
            () => {
                this._exported?.unexport();
                this._exported = null;
            });
    }

    disable() {
        this._exported?.unexport();
        this._exported = null;
        if (this._ownerId)
            Gio.bus_unown_name(this._ownerId);
        this._ownerId = null;
        this._service = null;
        this._settings = null;
    }
}
