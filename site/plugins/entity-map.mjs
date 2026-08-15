import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import matter from 'gray-matter';

/**
 * Словарь «сущность → канонический URL» для автоперелинковки (§7.2).
 *
 * Приоритет источников: ручные переопределения → pillar → услуга → статья.
 * Так «Честный Знак» всегда ведёт на pillar, а не на случайную статью,
 * которая упомянула эту сущность во frontmatter.
 */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CONTENT = path.join(ROOT, 'src', 'content');
const OVERRIDES = path.join(ROOT, 'src', 'data', 'entity-links.json');

const SOURCES = [
  { dir: 'pillars', prefix: '/baza/', priority: 1 },
  { dir: 'uslugi', prefix: '/uslugi/', priority: 2 },
  { dir: 'baza', prefix: '/baza/', priority: 3 },
  { dir: 'kejsy', prefix: '/kejsy/', priority: 4 },
];

let cache = null;
let cacheStamp = -1;

const listFiles = (dir) => {
  const abs = path.join(CONTENT, dir);
  if (!fs.existsSync(abs)) return [];
  return fs
    .readdirSync(abs, { recursive: true })
    .filter((f) => typeof f === 'string' && /\.mdx?$/.test(f))
    .map((f) => path.join(abs, f));
};

const stamp = () => {
  let latest = 0;
  for (const source of SOURCES) {
    for (const file of listFiles(source.dir)) {
      const mtime = fs.statSync(file).mtimeMs;
      if (mtime > latest) latest = mtime;
    }
  }
  if (fs.existsSync(OVERRIDES)) {
    latest = Math.max(latest, fs.statSync(OVERRIDES).mtimeMs);
  }
  return latest;
};

const build = () => {
  /** @type {Map<string, {url: string, priority: number, file: string}>} */
  const map = new Map();

  const claim = (entity, url, priority, file) => {
    const key = entity.trim();
    if (!key) return;
    const existing = map.get(key.toLowerCase());
    if (existing && existing.priority <= priority) return;
    map.set(key.toLowerCase(), { entity: key, url, priority, file });
  };

  for (const source of SOURCES) {
    for (const file of listFiles(source.dir)) {
      let data;
      try {
        ({ data } = matter(fs.readFileSync(file, 'utf8')));
      } catch {
        continue;
      }
      if (!data?.slug || data.noindex === true) continue;
      const url = `${source.prefix}${data.slug}/`;
      // Pillar и услуга представляют свою тему целиком — заголовок тоже сущность
      if (source.priority <= 2 && data.h1) claim(data.h1, url, source.priority, file);
      for (const entity of data.entities ?? []) claim(entity, url, source.priority, file);
    }
  }

  if (fs.existsSync(OVERRIDES)) {
    const overrides = JSON.parse(fs.readFileSync(OVERRIDES, 'utf8'));
    for (const [entity, url] of Object.entries(overrides)) {
      map.set(entity.trim().toLowerCase(), { entity, url, priority: 0, file: OVERRIDES });
    }
  }

  // Длинные сущности матчатся первыми: «ТН ВЭД 8708 30» важнее «ТН ВЭД»
  return [...map.values()].sort((a, b) => b.entity.length - a.entity.length);
};

/** @returns {{entity: string, url: string, priority: number, file: string}[]} */
export const getEntityMap = () => {
  const current = stamp();
  if (!cache || current !== cacheStamp) {
    cache = build();
    cacheStamp = current;
  }
  return cache;
};
