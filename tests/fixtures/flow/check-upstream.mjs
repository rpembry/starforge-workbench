// Optional pinned-source compatibility probe; no Workbench runtime dependency.
// node tests/fixtures/flow/check-upstream.mjs /path/to/pinned/tasks.md/packages/parser/dist/index.js
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {dirname, join, resolve} from 'node:path';

if (!process.argv[2]) throw new Error('Pass the built parser path from pinned commit 90bf97361c80f9c479eeaed7cd7fdefa8ad97416');
const {parseTasksContent} = await import(pathToFileURL(resolve(process.argv[2])).href);
const here = dirname(fileURLToPath(import.meta.url));
const read = name => parseTasksContent(readFileSync(join(here, name), 'utf8'), name);

const minimal = read('minimal.md');
assert.equal(minimal.length, 1);
assert.equal(minimal[0].summary, 'Fix the heading typo');
assert.equal(minimal[0].priority, 'P2');
assert.equal(minimal[0].metadata.id, undefined);

const substantial = read('substantial.md');
assert.equal(substantial[0].metadata.id, 'parser-coverage-01');
assert.equal(substantial[0].metadata.details, 'Preserve this first line.\nPreserve this second line as well.');
assert.equal(substantial[0].metadata.customfield, 'Retain this unknown value.');
assert.deepEqual(substantial[0].subtasks, ['Check a nested case']);
assert.deepEqual(substantial[1].metadata.blockedBy, ['parser-coverage-01']);
assert.equal(substantial[1].metadata.blocked, 'Waiting for an external review.');
// Deliberate divergence: the pinned parser incorrectly treats fenced examples as tasks.
assert.equal(substantial[2].summary, 'This fenced example is not a task');

const claimed = read('claimed-and-foreign.md');
assert.deepEqual(claimed.map(task => task.metadata.id), ['open-02']);
assert.equal(read('TODO.md').length, 0);
assert.equal(read('spec-kit-tasks.md').length, 0);
console.log('Pinned upstream parser compatibility fixtures passed');
