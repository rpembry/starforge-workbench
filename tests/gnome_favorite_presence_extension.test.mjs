import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

import {
    calculateSnapshot,
    desktopAppId,
    normalizeAllowlist,
    unknownSnapshot,
} from '../extensions/favorite-presence@rpembry.github.io/presence-core.js';

const sourcePath = new URL('../extensions/favorite-presence@rpembry.github.io/extension.js',
    import.meta.url);
const source = readFileSync(sourcePath, 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replace('export default class FavoritePresenceExtension',
        'class FavoritePresenceExtension') +
    '\nglobalThis.TestExtension = FavoritePresenceExtension;';

function desktopApp(id) {
    return {
        get_id: () => id,
        is_window_backed: () => false,
        get_app_info: () => ({get_id: () => id}),
    };
}

function fixture() {
    const appId = 'org.example.Editor.desktop';
    const installed = desktopApp(appId);
    const events = {exports: 0, unexports: 0, unowns: 0};
    const windows = [];
    let callbacks;
    let service;
    const Gio = {
        BusType: {SESSION: 1},
        BusNameOwnerFlags: {NONE: 0},
        bus_own_name(_bus, _name, _flags, busAcquired, nameAcquired, nameLost) {
            callbacks = {busAcquired, nameAcquired, nameLost};
            return 7;
        },
        bus_unown_name(id) {
            assert.equal(id, 7);
            events.unowns++;
        },
        DBusExportedObject: {
            wrapJSObject(_xml, instance) {
                service = instance;
                return {
                    export() { events.exports++; },
                    unexport() { events.unexports++; },
                };
            },
        },
    };
    const context = {
        Gio,
        GLib: {get_monotonic_time: () => 123456},
        Shell: {
            AppSystem: {get_default: () => ({
                lookup_app: id => id === appId ? installed : null,
            })},
            WindowTracker: {get_default: () => ({
                get_window_app: window => window.app,
                get_startup_sequences: () => [],
            })},
        },
        Main: {sessionMode: {currentMode: 'user'}, screenShield: {locked: false}},
        Extension: class {
            getSettings() {
                return {get_value: () => ({deepUnpack: () => [
                    ['editor', appId, 'native'],
                ]})};
            }
        },
        calculateSnapshot,
        desktopAppId,
        normalizeAllowlist,
        unknownSnapshot,
        Set,
        global: {display: {list_all_windows: () => windows}},
    };
    runInNewContext(source, context, {filename: 'extension.js'});
    const extension = new context.TestExtension();
    extension.enable();
    return {appId, callbacks: () => callbacks, events, extension, service: () => service, windows};
}

test('synthetic Shell app makes native absence unknown while exact match stays present', () => {
    const state = fixture();
    state.callbacks().nameAcquired({});
    state.windows.push({app: {
        get_id: () => 'window:7',
        is_window_backed: () => true,
        get_app_info: () => null,
    }});
    const snapshot = () => JSON.parse(JSON.stringify(state.service().Snapshot()[1]));
    assert.deepEqual(snapshot(), [['editor', 'unknown', 'none', 0]]);
    state.windows.push({app: desktopApp(state.appId)});
    assert.deepEqual(snapshot(), [['editor', 'present', 'shell-app-association', 1]]);
    state.windows.length = 0;
    assert.deepEqual(snapshot(), [['editor', 'absent', 'window-inventory', 0]]);
    state.extension.disable();
});

test('D-Bus name loss and reacquisition reexports the same narrow service', () => {
    const state = fixture();
    const callbacks = state.callbacks();
    assert.equal(callbacks.busAcquired, null);
    assert.equal(state.events.exports, 0);
    callbacks.nameAcquired({});
    assert.equal(state.events.exports, 1);
    const service = state.service();
    callbacks.nameLost();
    assert.equal(state.events.unexports, 1);
    callbacks.nameAcquired({});
    assert.equal(state.events.exports, 2);
    assert.equal(state.service(), service);
    assert.deepEqual(JSON.parse(JSON.stringify(service.Snapshot()[1])),
        [['editor', 'absent', 'window-inventory', 0]]);
    state.extension.disable();
    assert.equal(state.events.unexports, 2);
    assert.equal(state.events.unowns, 1);
});
