import assert from 'node:assert/strict';
import test from 'node:test';

import {
    calculateSnapshot,
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
