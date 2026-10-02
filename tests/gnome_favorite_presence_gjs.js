import {calculateSnapshot, normalizeAllowlist} from
    '../extensions/favorite-presence@rpembry.github.io/presence-core.js';

const rows = normalizeAllowlist([
    ['editor', 'org.example.Editor.desktop', 'native'],
    ['browser', 'google-chrome.desktop', 'browser'],
]);
const result = calculateSnapshot(rows, {
    active: true,
    knownAppIds: new Set(['org.example.Editor.desktop', 'google-chrome.desktop']),
    windowAppIds: ['org.example.Editor.desktop', 'google-chrome.desktop'],
    unmappedWindowCount: 0,
    startupPending: false,
});
if (JSON.stringify(result) !== JSON.stringify([
    ['editor', 'present', 'shell-app-association', 1],
    ['browser', 'unknown', 'none', 0],
]))
    throw new Error('GJS presence mapping changed');
