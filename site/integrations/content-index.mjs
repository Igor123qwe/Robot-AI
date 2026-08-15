import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import matter from 'gray-matter';

/**
 * Плоский индекс всего контента для интеграций сборки: карты сайта,
 * llms.txt, llms-full.txt и OG-картинки читают его вместо коллекций Astro,
 * чтобы не тянуть рантайм фреймворка в node-скрипты.
 */

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CONTENT = path.join(ROOT, 'src', 'content');

const SECTIONS = [
  { dir: 'uslugi', prefix: '/uslugi/', kind: 'service' },
  { dir: 'pillars', prefix: '/baza/', kind: 'pillar' },
  { dir: 'baza', prefix: '/baza/', kind: 'article' },
  { dir: 'kejsy', prefix: '/kejsy/', kind: 'case' },
];

const walk = (dir) => {
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir, { recursive: true })
    .filter((f) => typeof f === 'string' && /\.mdx?$/.test(f))
    .map((f) => path.join(dir, f));
};

/** @returns {{url:string,kind:string,file:string,rel:string,data:object,body:string}[]} */
export const readContent = () => {
  const entries = [];
  for (const section of SECTIONS) {
    for (const file of walk(path.join(CONTENT, section.dir))) {
      const raw = fs.readFileSync(file, 'utf8');
      const { data, content } = matter(raw);
      if (!data.slug) continue;
      entries.push({
        url: `${section.prefix}${data.slug}/`,
        kind: section.kind,
        file,
        rel: path.relative(ROOT, file).replace(/\\/g, '/'),
        data,
        body: content,
      });
    }
  }
  return entries;
};

/** Текст без разметки — для llms-full.txt и подсчёта объёма */
export const toPlainMarkdown = (body) =>
  body
    .replace(/^import\s.+$/gm, '')
    .replace(/^export\s.+$/gm, '')
    .replace(/^:::\w+(\[[^\]]*\])?\s*$/gm, '')
    .replace(/^:::\s*$/gm, '')
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
