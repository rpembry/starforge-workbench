import assert from 'node:assert/strict';
import test from 'node:test';

import {
    calculateSnapshot,
    desktopAppId,
    normalizeAllowlist,
} from '../extensions/favorite-presence@rpembry.github.io/presence-core.js';

const rows = normalizeAllowlist([
    ['editor', 'org.example.Editor.desktop', 'native'],
    ['chrome-work', 'google-chrome.desktop', 'browser'],
    ['notes-pwa', 'org.example.Notes.desktop', 'pwa'],
]);

function observation(overrides = {}) {
    return {
        active: true,
        knownAppIds: new Set(['org.example.Editor.desktop', 'google-chrome.desktop']),
        windowAppIds: [],
        unmappedWindowCount: 0,
        startupPending: false,
        ...overrides,
    };
}

function shellApp(id, {windowBacked = false, desktopId = id} = {}) {
    return {
        get_id: () => id,
        is_window_backed: () => windowBacked,
        get_app_info: () => desktopId === null ? null : {get_id: () => desktopId},
    };
}

test('only verified desktop-backed Shell apps count as mapped windows', () => {
    const native = shellApp('org.example.Editor.desktop');
    const synthetic = shellApp('window:0x1234', {windowBacked: true, desktopId: null});
    assert.equal(desktopAppId(native), 'org.example.Editor.desktop');
    assert.equal(desktopAppId(synthetic), null);
    assert.equal(desktopAppId(shellApp('window:0x5678', {desktopId: null})), null);
    assert.equal(desktopAppId(shellApp('org.example.Editor.desktop', {
        desktopId: 'org.example.Other.desktop',
    })), null);
    assert.equal(desktopAppId(null), null);

    const mappedIds = [synthetic, native].map(desktopAppId).filter(Boolean);
    const incomplete = observation({
        windowAppIds: mappedIds,
        unmappedWindowCount: 1,
    });
    assert.deepEqual(calculateSnapshot(rows, incomplete)[0],
        ['editor', 'present', 'shell-app-association', 1]);
    assert.deepEqual(calculateSnapshot(rows, observation({
        windowAppIds: [],
        unmappedWindowCount: 1,
    }))[0], ['editor', 'unknown', 'none', 0]);
    assert.deepEqual(calculateSnapshot(rows, observation())[0],
        ['editor', 'absent', 'window-inventory', 0]);
});

test('only exact native Shell app identity establishes window presence', () => {
    const result = calculateSnapshot(rows, observation({
        windowAppIds: ['org.example.Editor.desktop', 'org.example.Editor.desktop'],
    }));
    assert.deepEqual(result, [
        ['editor', 'present', 'shell-app-association', 2],
        ['chrome-work', 'unknown', 'none', 0],
        ['notes-pwa', 'unknown', 'none', 0],
    ]);
    assert.equal(calculateSnapshot(rows, observation({
        windowAppIds: ['org.example.Editor.desktop.evil'],
    }))[0][1], 'absent');
});

test('missing identity, incomplete mapping, startup and inactive session are unknown', () => {
    assert.equal(calculateSnapshot(rows, observation({knownAppIds: new Set()}))[0][1], 'unknown');
    assert.equal(calculateSnapshot(rows, observation({unmappedWindowCount: 1}))[0][1], 'unknown');
    assert.equal(calculateSnapshot(rows, observation({startupPending: true}))[0][1], 'unknown');
    assert.equal(calculateSnapshot(rows, observation({active: false}))[0][1], 'unknown');
    assert.equal(calculateSnapshot(rows, observation({windowAppIds: [null]}))[0][1], 'unknown');
});

test('native zero is only a window-inventory claim, never a process claim', () => {
    assert.deepEqual(calculateSnapshot(rows, observation())[0],
        ['editor', 'absent', 'window-inventory', 0]);
});

test('private allowlist validation rejects collisions and malformed identities', () => {
    assert.deepEqual(normalizeAllowlist([]), []);
    assert.throws(() => normalizeAllowlist([
        ['editor', 'org.example.Editor.desktop', 'native'],
        ['editor', 'org.example.Other.desktop', 'native'],
    ]));
    assert.throws(() => normalizeAllowlist([['bad key', 'org.example.Editor.desktop', 'native']]));
    assert.throws(() => normalizeAllowlist([['editor', '../other.desktop', 'native']]));
    assert.throws(() => normalizeAllowlist([['editor', 'org.example.Editor.desktop', 'other']]));
});
