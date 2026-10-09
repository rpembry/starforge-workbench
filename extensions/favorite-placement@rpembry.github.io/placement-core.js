export function validateConfig(value) {
    if (value?.version !== 1 || !Array.isArray(value.assignments) || value.assignments.length > 32)
        throw new Error('Invalid placement configuration');
    const seen = new Set();
    for (const row of value.assignments) {
        if (!row || !['left', 'middle', 'right'].includes(row.monitor) ||
            !Array.isArray(row.classes) || !row.classes.length ||
            row.classes.some(x => typeof x !== 'string' || !x || seen.has(x)) ||
            (row.title !== undefined && (typeof row.title !== 'string' || !row.title)))
            throw new Error('Invalid or overlapping placement identity');
        row.classes.forEach(x => seen.add(x));
        if (!value.monitors?.[row.monitor]?.connector || !value.monitors[row.monitor].serial)
            throw new Error('Monitor needs connector and serial');
    }
    return value;
}

export function assignmentFor(config, identity) {
    // Class matching is exact. Browser app IDs are unique to each installed PWA.
    const matches = config.assignments.filter(row =>
        row.classes.some(x => identity.classes.includes(x)) &&
        (row.title === undefined || identity.title === row.title));
    return matches.length === 1 ? matches[0] : null;
}

export function monitorFor(expected, monitors) {
    // Serial protects against a replacement display silently inheriting a connector.
    const matches = monitors.filter(m => m.connector === expected.connector && m.serial === expected.serial);
    return matches.length === 1 ? matches[0].index : null;
}
