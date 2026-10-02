const KEY = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const DESKTOP_ID = /^[A-Za-z0-9_.-]+\.desktop$/;
const KINDS = new Set(['native', 'browser', 'pwa']);

export function normalizeAllowlist(value) {
    if (!Array.isArray(value) || value.length > 32)
        throw new Error('favorite allowlist is invalid');
    const seen = new Set();
    return value.map(row => {
        if (!Array.isArray(row) || row.length !== 3 ||
            !row.every(item => typeof item === 'string') ||
            !KEY.test(row[0]) || !DESKTOP_ID.test(row[1]) || !KINDS.has(row[2]) ||
            seen.has(row[0]))
            throw new Error('favorite identity is invalid or duplicated');
        seen.add(row[0]);
        return {key: row[0], appId: row[1], kind: row[2]};
    });
}

export function unknownSnapshot(rows) {
    return rows.map(row => [row.key, 'unknown', 'none', 0]);
}

export function calculateSnapshot(rows, observation) {
    if (!observation?.active || !(observation.knownAppIds instanceof Set) ||
        !Array.isArray(observation.windowAppIds) ||
        observation.windowAppIds.some(id => typeof id !== 'string') ||
        !Number.isInteger(observation.unmappedWindowCount) ||
        observation.unmappedWindowCount < 0 ||
        typeof observation.startupPending !== 'boolean')
        return unknownSnapshot(rows);

    return rows.map(row => {
        // A Shell app ID cannot distinguish browser profiles or installed PWAs.
        if (row.kind !== 'native' || !observation.knownAppIds.has(row.appId))
            return [row.key, 'unknown', 'none', 0];
        const count = observation.windowAppIds.filter(id => id === row.appId).length;
        if (count > 0)
            return [row.key, 'present', 'shell-app-association', count];
        if (observation.unmappedWindowCount > 0 || observation.startupPending)
            return [row.key, 'unknown', 'none', 0];
        return [row.key, 'absent', 'window-inventory', 0];
    });
}
