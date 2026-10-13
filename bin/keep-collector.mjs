#!/usr/bin/env node
/** Read one configured Google Keep checklist through an existing Chrome CDP session. */
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const DEFAULT_CONFIG = '~/.config/starforge-ai-workbench/keep-collector.json';

function expandHome(value) {
  return value === '~' || value.startsWith('~/') ? path.join(os.homedir(), value.slice(2)) : value;
}

export function parseArgs(argv) {
  const options = { cdpUrl: 'http://127.0.0.1:9222', config: DEFAULT_CONFIG, format: 'json' };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === '--cdp-url') options.cdpUrl = argv[++index];
    else if (value === '--config') options.config = argv[++index];
    else if (value === '--format') options.format = argv[++index];
    else if (value === '--help') options.help = true;
    else throw new Error(`Unknown argument: ${value}`);
  }
  if (!['json', 'markdown'].includes(options.format)) throw new Error('--format must be json or markdown');
  if ((!options.cdpUrl || !options.config) && !options.help) throw new Error('Missing value for an option');
  return options;
}

export function validateConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('Configuration must be a JSON object');
  if (typeof config.note_title !== 'string' || !config.note_title.trim()) throw new Error('Configuration requires a non-empty note_title');
  if (config.require_pinned !== undefined && typeof config.require_pinned !== 'boolean') throw new Error('require_pinned must be a boolean when provided');
  return { noteTitle: config.note_title.trim(), requirePinned: config.require_pinned !== false };
}

export function renderMarkdown(result) {
  const lines = [`# ${result.note.title}`, '', `Collected: ${result.collected_at}`, `Pinned: ${result.note.pinned ? 'yes' : 'no'}`, ''];
  for (const item of result.note.items) lines.push(`- [${item.checked ? 'x' : ' '}] ${item.text}`);
  return `${lines.join('\n')}\n`;
}

export function extractNote(config, root = document) {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const articles = [...root.querySelectorAll('[role="article"]')];
  const cards = articles.length ? articles : [...root.querySelectorAll('[role="listitem"]')]
    .filter(card => card.querySelectorAll('[contenteditable="true"][aria-label="Title"], [role="heading"]').length);
  const candidates = cards.flatMap(card => {
    const titleValues = [...card.querySelectorAll('[contenteditable="true"][aria-label="Title"], [role="heading"]')]
      .map(node => clean(node.textContent)).filter(Boolean);
    const titles = [...new Set(titleValues)];
    if (titles.length !== 1 || titles[0] !== config.noteTitle) return [];
    const attributes = [...card.querySelectorAll('[aria-label], [title], [data-tooltip]')]
      .map(node => `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''} ${node.getAttribute('data-tooltip') || ''}`)
      .join(' ');
    const pinned = /\bunpin\b|\bpinned\b/i.test(attributes);
    return !config.requirePinned || pinned ? [{ card, title: titles[0], pinned }] : [];
  });
  if (!candidates.length) return { status: 'not_found' };
  if (candidates.length > 1) return { status: 'ambiguous' };
  const candidate = candidates[0];
  const checkboxes = [...candidate.card.querySelectorAll('[role="checkbox"], input[type="checkbox"]')];
  const hasListStructure = checkboxes.length > 0 || candidate.card.querySelectorAll(
    '[aria-label="List item"], [aria-label="Add list item"], [data-list-item]').length > 0;
  if (!hasListStructure) return { status: 'invalid_checklist' };
  const items = [];
  for (const checkbox of checkboxes) {
    const ariaChecked = checkbox.getAttribute('aria-checked');
    const checked = ariaChecked === 'true' ? true : ariaChecked === 'false' ? false
      : typeof checkbox.checked === 'boolean' ? checkbox.checked : null;
    const row = checkbox.closest('[role="listitem"], [data-list-item]');
    const values = row ? [...row.querySelectorAll('[contenteditable="true"], [data-item-text]')]
      .map(node => clean(node.textContent)).filter(Boolean) : [];
    const texts = [...new Set(values)];
    if (checked === null || texts.length !== 1) return { status: 'invalid_checklist' };
    items.push({ text: texts[0], checked });
  }
  return { status: 'ok', note: { title: candidate.title, pinned: candidate.pinned, items } };
}

async function loadConfig(configPath) {
  try {
    const resolved = expandHome(configPath);
    const metadata = await fs.stat(resolved);
    if (!metadata.isFile() || metadata.uid !== process.getuid() || (metadata.mode & 0o077)) {
      throw new Error(`Runtime configuration must be an owner-only regular file: ${configPath}`);
    }
    return validateConfig(JSON.parse(await fs.readFile(resolved, 'utf8')));
  } catch (error) {
    if (error.code === 'ENOENT') throw new Error(`Runtime configuration not found: ${configPath}`);
    if (error instanceof SyntaxError) throw new Error(`Invalid JSON in runtime configuration: ${configPath}`);
    throw error;
  }
}

function playwrightFrom(directory) {
  const runtimeDirectory = expandHome(directory || process.env.AI_WORKBENCH_PLAYWRIGHT_DIR || '~/browser-automation');
  try {
    return createRequire(path.join(runtimeDirectory, 'package.json'))('playwright');
  } catch {
    throw new Error(`Playwright is unavailable under ${runtimeDirectory}; set AI_WORKBENCH_PLAYWRIGHT_DIR to its installation directory`);
  }
}

async function collect(page, config) {
  const result = await page.evaluate(extractNote, config);
  if (result.status === 'not_found') throw new Error(`No ${config.requirePinned ? 'pinned ' : ''}Google Keep note exactly matched the configured title`);
  if (result.status === 'ambiguous') throw new Error('Multiple Google Keep notes exactly matched the configured title');
  if (result.status !== 'ok') throw new Error('Matched Google Keep note did not expose a valid checklist structure');
  return result.note;
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help) {
    console.log('Usage: keep-collector.mjs [--config PATH] [--cdp-url URL] [--format json|markdown]');
    return;
  }
  const config = await loadConfig(options.config);
  const { chromium } = playwrightFrom();
  let browser;
  try {
    browser = await chromium.connectOverCDP(options.cdpUrl);
  } catch {
    throw new Error(`Cannot connect to Chrome CDP at ${options.cdpUrl}; start Chrome with remote debugging enabled, then retry`);
  }
  try {
    const context = browser.contexts()[0];
    if (!context) throw new Error('Chrome CDP did not expose a browser context');
    let page = context.pages().find(candidate => candidate.url().startsWith('https://keep.google.com'));
    if (!page) {
      page = await context.newPage();
      await page.goto('https://keep.google.com', { waitUntil: 'domcontentloaded' });
    }
    await page.waitForTimeout(1000);
    const result = { collected_at: new Date().toISOString(), note: await collect(page, config) };
    process.stdout.write(options.format === 'markdown' ? renderMarkdown(result) : `${JSON.stringify(result, null, 2)}\n`);
  } finally {
    await browser?.close();
  }
}

if (import.meta.main) {
  main().catch(error => {
    console.error(`Keep collector error: ${error.message}`);
    process.exitCode = 2;
  });
}
