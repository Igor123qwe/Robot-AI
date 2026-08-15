#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SITE, abs } from '../site.config.mjs';

/**
 * Пинг IndexNow при деплое (§5.6): сообщаем Яндексу и Bing об изменившихся
 * адресах. Отправляем не весь сайт, а разницу с предыдущим деплоем —
 * иначе поисковики режут такие уведомления как шум.
 *
 * Переменные окружения:
 *   INDEXNOW_KEY   — ключ, он же имя файла в корне домена (обязательно)
 *   INDEXNOW_ALL=1 — отправить все адреса, а не только изменившиеся
 *
 * Запуск: npm run indexnow
 */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const STATE = path.join(ROOT, '.indexnow-state.json');
const ENDPOINT = 'https://api.indexnow.org/IndexNow';

const key = process.env.INDEXNOW_KEY;
if (!key) {
  console.error('IndexNow: не задан INDEXNOW_KEY — пропускаю пинг.');
  process.exit(0);
}

if (!fs.existsSync(DIST)) {
  console.error('IndexNow: нет каталога dist — сначала выполните сборку.');
  process.exit(1);
}

/** Собираем адреса и их lastmod из сгенерированных карт сайта */
const collect = () => {
  const map = new Map();
  for (const file of fs.readdirSync(DIST)) {
    if (!/^sitemap-(?!index).*\.xml$/.test(file)) continue;
    const xml = fs.readFileSync(path.join(DIST, file), 'utf8');
    for (const block of xml.match(/<url>[\s\S]*?<\/url>/g) ?? []) {
      const loc = block.match(/<loc>([^<]+)<\/loc>/)?.[1];
      const lastmod = block.match(/<lastmod>([^<]+)<\/lastmod>/)?.[1] ?? '';
      if (loc) map.set(loc, lastmod);
    }
  }
  return map;
};

const current = collect();
if (current.size === 0) {
  console.error('IndexNow: в картах сайта нет адресов.');
  process.exit(1);
}

const previous = fs.existsSync(STATE)
  ? new Map(Object.entries(JSON.parse(fs.readFileSync(STATE, 'utf8'))))
  : new Map();

const changed =
  process.env.INDEXNOW_ALL === '1' || previous.size === 0
    ? [...current.keys()]
    : [...current.entries()]
        .filter(([loc, lastmod]) => previous.get(loc) !== lastmod)
        .map(([loc]) => loc);

if (changed.length === 0) {
  console.log('IndexNow: изменившихся адресов нет, пинг не нужен.');
  process.exit(0);
}

// IndexNow принимает до 10 000 адресов за раз
const batches = [];
for (let i = 0; i < changed.length; i += 10000) batches.push(changed.slice(i, i + 10000));

let failed = false;

for (const [index, urlList] of batches.entries()) {
  const body = {
    host: SITE.domain,
    key,
    keyLocation: abs(`/${key}.txt`),
    urlList,
  };

  try {
    const response = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
    });
    const ok = response.status >= 200 && response.status < 300;
    console.log(
      `IndexNow: пачка ${index + 1}/${batches.length}, адресов ${urlList.length}, ответ ${response.status} ${response.statusText}`,
    );
    if (!ok) {
      failed = true;
      console.error(`IndexNow: тело ответа — ${(await response.text()).slice(0, 300)}`);
    }
  } catch (error) {
    failed = true;
    console.error(`IndexNow: запрос не ушёл — ${error.message}`);
  }
}

if (!failed) {
  fs.writeFileSync(STATE, JSON.stringify(Object.fromEntries(current), null, 2), 'utf8');
  console.log(`IndexNow: отправлено ${changed.length} адресов, состояние сохранено в ${path.basename(STATE)}.`);
  process.exit(0);
}

console.error('IndexNow: часть пачек не доставлена, состояние не обновлено — повторите пинг.');
process.exit(1);
