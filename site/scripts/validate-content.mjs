#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import matter from 'gray-matter';
import { PILLAR_SLUGS, ARTICLE_TYPES } from '../site.config.mjs';

/**
 * Валидация контента до сборки (§7.3).
 *
 * Схема коллекций в src/content.config.ts ловит ошибки типов, но не видит
 * корпус целиком и не умеет показывать номер строки. Этот скрипт закрывает
 * оба пробела: правила из ТЗ, отчёт с файлом и строкой, ненулевой код
 * возврата — сборка падает раньше, чем потратит время на рендер.
 *
 * Запуск: npm run validate
 */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CONTENT = path.join(ROOT, 'src', 'content');
const MIN_BODY_CHARS = 2500;

const SECTIONS = [
  { dir: 'baza', prefix: '/baza/', kind: 'статья', full: true },
  { dir: 'pillars', prefix: '/baza/', kind: 'pillar', full: false },
  { dir: 'uslugi', prefix: '/uslugi/', kind: 'услуга', full: false },
  { dir: 'kejsy', prefix: '/kejsy/', kind: 'кейс', full: false },
];

/** Статические маршруты, на которые контент имеет право ссылаться */
const STATIC_ROUTES = new Set([
  '/',
  '/uslugi/',
  '/tarify/',
  '/baza/',
  '/instrumenty/',
  '/instrumenty/kalkulyator-ozon/',
  '/instrumenty/komissii-fbo-fbs/',
  '/instrumenty/nuzhna-li-markirovka/',
  '/kejsy/',
  '/o-nas/',
  '/kontakty/',
  '/politika-konfidencialnosti/',
  '/llms.txt',
  '/llms-full.txt',
  '/sitemap-index.xml',
]);

const problems = [];
const notes = [];

const add = (file, line, message) =>
  problems.push({ file: path.relative(ROOT, file), line, message });

const warn = (file, line, message) =>
  notes.push({ file: path.relative(ROOT, file), line, message });

/** Номер строки, на которой встречается поле frontmatter или подстрока */
const lineOf = (raw, needle) => {
  const index = raw.indexOf(needle);
  if (index < 0) return 1;
  return raw.slice(0, index).split('\n').length;
};

const words = (value) => String(value).trim().split(/\s+/).filter(Boolean).length;

const files = [];
for (const section of SECTIONS) {
  const dir = path.join(CONTENT, section.dir);
  if (!fs.existsSync(dir)) continue;
  for (const name of fs.readdirSync(dir, { recursive: true })) {
    if (typeof name !== 'string' || !/\.mdx?$/.test(name)) continue;
    files.push({ section, file: path.join(dir, name) });
  }
}

const seenSlugs = new Map();
const seenTitles = new Map();
const knownUrls = new Set(STATIC_ROUTES);

// Первый проход: собираем адреса, чтобы во втором проверить внутренние ссылки
const parsed = files.map(({ section, file }) => {
  const raw = fs.readFileSync(file, 'utf8');
  const { data, content } = matter(raw);
  if (data.slug) knownUrls.add(`${section.prefix}${data.slug}/`);
  return { section, file, raw, data, content };
});

for (const { section, file, raw, data, content } of parsed) {
  const url = data.slug ? `${section.prefix}${data.slug}/` : null;

  // --- обязательные поля и их границы ---
  if (!data.title) add(file, 1, 'нет поля title');
  else if (String(data.title).length > 65) {
    add(file, lineOf(raw, 'title:'), `title длиннее 65 символов (${String(data.title).length})`);
  } else if (String(data.title).length > 60) {
    warn(file, lineOf(raw, 'title:'), `title длиннее рекомендованных 60 символов (${String(data.title).length})`);
  }

  if (!data.description) add(file, 1, 'нет поля description');
  else {
    const length = String(data.description).length;
    if (length < 120 || length > 170) {
      add(file, lineOf(raw, 'description:'), `description ${length} символов, допустимо 120–170`);
    } else if (length < 140 || length > 160) {
      warn(file, lineOf(raw, 'description:'), `description ${length} символов, рекомендуется 140–160`);
    }
  }

  if (!data.direct_answer) {
    add(file, 1, 'нет блока прямого ответа direct_answer (§5.3)');
  } else {
    const count = words(data.direct_answer);
    if (count < 30) add(file, lineOf(raw, 'direct_answer:'), `direct_answer ${count} слов, нужно не меньше 30`);
    if (count > 90) add(file, lineOf(raw, 'direct_answer:'), `direct_answer ${count} слов, нужно не больше 90`);
  }

  if (!data.slug) {
    add(file, 1, 'нет поля slug');
  } else {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(data.slug)) {
      add(file, lineOf(raw, 'slug:'), `slug «${data.slug}»: только строчная латиница, цифры и дефис (§3.2)`);
    }
    const key = `${section.prefix}${data.slug}`;
    if (seenSlugs.has(key)) {
      add(file, lineOf(raw, 'slug:'), `slug дублируется с ${seenSlugs.get(key)}`);
    } else {
      seenSlugs.set(key, path.relative(ROOT, file));
    }
  }

  if (data.title) {
    const key = String(data.title).toLowerCase();
    if (seenTitles.has(key)) {
      add(file, lineOf(raw, 'title:'), `title дублируется с ${seenTitles.get(key)}`);
    } else {
      seenTitles.set(key, path.relative(ROOT, file));
    }
  }

  if (section.dir === 'baza') {
    if (!ARTICLE_TYPES.includes(data.type)) {
      add(file, lineOf(raw, 'type:'), `type «${data.type}»: ожидается один из ${ARTICLE_TYPES.join(', ')}`);
    }
    if (!PILLAR_SLUGS.includes(data.pillar)) {
      add(file, lineOf(raw, 'pillar:'), `pillar «${data.pillar}»: раздела нет в site.config.mjs`);
    }
    if (data.type === 'howto' && (!Array.isArray(data.steps) || data.steps.length < 2)) {
      add(file, 1, 'тип howto требует поле steps минимум с двумя шагами (Schema HowTo)');
    }
    if (data.type === 'troubleshoot' && (!Array.isArray(data.faq) || data.faq.length < 2)) {
      add(file, 1, 'тип troubleshoot требует минимум два вопроса в faq (Schema FAQPage)');
    }
  }

  // --- тело статьи ---
  const body = content.trim();
  const plain = body
    .replace(/```[\s\S]*?```/g, '')
    .replace(/^:::.*$/gm, '')
    .replace(/[#>*_`|]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  if (section.full && plain.length < MIN_BODY_CHARS) {
    add(file, 1, `статья короче ${MIN_BODY_CHARS} знаков (сейчас ${plain.length})`);
  }

  const headings = body.match(/^##\s+.+$/gm) ?? [];
  if (section.full && headings.length === 0) {
    add(file, 1, 'в статье нет ни одного H2');
  }

  // Местоимение в начале абзаца ломает извлечение фрагмента (§5.3)
  for (const match of body.matchAll(/^(Он|Она|Оно|Они|Это|Этот|Эта|Эти)\s/gm)) {
    warn(file, lineOf(raw, match[0]), `абзац начинается с местоимения «${match[1]}» — замените на сущность (§5.3)`);
  }

  // --- изображения без alt ---
  for (const match of body.matchAll(/!\[([^\]]*)\]\(([^)]+)\)/g)) {
    if (!match[1].trim()) add(file, lineOf(raw, match[0]), `изображение без alt: ${match[2]}`);
  }
  for (const match of body.matchAll(/<img\b[^>]*>/g)) {
    if (!/\balt=/.test(match[0])) add(file, lineOf(raw, match[0]), 'тег <img> без атрибута alt');
  }

  // --- внутренние ссылки ---
  for (const match of body.matchAll(/\[[^\]]*\]\((\/[^)\s]*)\)/g)) {
    const href = match[1].split('#')[0].split('?')[0];
    if (!href) continue;
    if (!knownUrls.has(href)) {
      add(file, lineOf(raw, match[0]), `битая внутренняя ссылка ${href}`);
    }
    if (!href.endsWith('/') && !path.extname(href)) {
      add(file, lineOf(raw, match[0]), `ссылка без завершающего слеша: ${href} (§3.2)`);
    }
  }

  if (data.related_service) {
    const target = `/uslugi/${data.related_service}/`;
    if (!knownUrls.has(target)) {
      add(file, lineOf(raw, 'related_service:'), `related_service «${data.related_service}»: услуги нет`);
    }
  }

  if (url && data.entities?.some((entity) => String(entity).trim() === '')) {
    add(file, lineOf(raw, 'entities:'), 'пустое значение в entities');
  }
}

// --- отчёт ---
const format = (item) => `${item.file}:${item.line}\n    ${item.message}`;

if (notes.length > 0) {
  console.log(`\nПредупреждения (${notes.length}):`);
  for (const note of notes) console.log(`  ${format(note)}`);
}

if (problems.length > 0) {
  console.error(`\nВалидация контента не пройдена. Ошибок: ${problems.length}\n`);
  for (const problem of problems) console.error(`  ${format(problem)}`);
  console.error('\nПравила описаны в §7.3 ТЗ и в README.\n');
  process.exit(1);
}

console.log(
  `\nВалидация контента пройдена: ${parsed.length} файлов, предупреждений ${notes.length}.\n`,
);
