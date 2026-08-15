import { execFileSync } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * dateModified берём из git-истории файла (§5.5): дата последнего коммита,
 * изменившего контент. Одним вызовом git на всю сборку — по файлу это были
 * бы сотни процессов.
 */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const CONTENT_REL = 'src/content';

let cache = null;

const load = () => {
  /** @type {Map<string, string>} */
  const map = new Map();
  try {
    const out = execFileSync(
      'git',
      ['log', '--pretty=format:@%cI', '--name-only', '--', CONTENT_REL],
      { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 },
    );
    let current = null;
    for (const line of out.split('\n')) {
      if (line.startsWith('@')) {
        current = line.slice(1).trim();
        continue;
      }
      const file = line.trim();
      if (!file || !current) continue;
      // git отдаёт коммиты от новых к старым — первая запись и есть последняя правка
      if (!map.has(file)) map.set(file, current);
    }
  } catch {
    // Не git-репозиторий или git недоступен — молча падаем на frontmatter
  }
  return map;
};

/**
 * @param {string} relPath путь от корня сайта, например `src/content/baza/x.mdx`
 * @param {Date|string} fallback дата из frontmatter
 * @returns {Date}
 */
export const gitModified = (relPath, fallback) => {
  cache ??= load();
  // Незакоммиченный файл: git о нём не знает, но mtime честнее frontmatter
  const iso = cache.get(relPath.replace(/\\/g, '/'));
  if (iso) return new Date(iso);
  const abs = path.join(ROOT, relPath);
  if (fs.existsSync(abs) && !cache.size) return new Date(fs.statSync(abs).mtimeMs);
  return fallback instanceof Date ? fallback : new Date(fallback);
};

/** Путь файла коллекции относительно корня сайта */
export const collectionPath = (collection, id) => {
  const dirs = { baza: 'baza', pillars: 'pillars', uslugi: 'uslugi', kejsy: 'kejsy' };
  const dir = dirs[collection] ?? collection;
  const base = path.join(ROOT, CONTENT_REL, dir);
  for (const ext of ['.mdx', '.md']) {
    const candidate = path.join(base, `${id}${ext}`);
    if (fs.existsSync(candidate)) return path.relative(ROOT, candidate).replace(/\\/g, '/');
  }
  return `${CONTENT_REL}/${dir}/${id}.mdx`;
};
