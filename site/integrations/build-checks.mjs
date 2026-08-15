import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SITE } from '../site.config.mjs';

/**
 * Проверки готовой сборки (§7.3, §10). Часть правил валидации можно
 * проверить только на выходе: битые внутренние ссылки появляются в том числе
 * из автоперелинковки, а alt у картинок — из Markdown.
 *
 * Любая ошибка роняет сборку: молча выложенный сайт с битыми ссылками хуже,
 * чем красная сборка.
 */

const listFiles = (dir, ext) => {
  const out = [];
  const walk = (current) => {
    for (const item of fs.readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, item.name);
      if (item.isDirectory()) walk(full);
      else if (!ext || item.name.endsWith(ext)) out.push(full);
    }
  };
  walk(dir);
  return out;
};

const attr = (tag, name) => {
  const match = tag.match(new RegExp(`${name}=("([^"]*)"|'([^']*)')`, 'i'));
  return match ? (match[2] ?? match[3] ?? '') : null;
};

export function buildChecks() {
  return {
    name: 'build-checks',
    hooks: {
      'astro:build:done': async ({ dir, logger }) => {
        const distDir = fileURLToPath(dir);
        const htmlFiles = listFiles(distDir, '.html');
        const allFiles = new Set(
          listFiles(distDir).map((f) => `/${path.relative(distDir, f).replace(/\\/g, '/')}`),
        );

        const errors = [];
        const warnings = [];
        const titles = new Map();

        for (const file of htmlFiles) {
          const rel = `/${path.relative(distDir, file).replace(/\\/g, '/')}`;
          const pageUrl = rel.replace(/index\.html$/, '');
          const html = fs.readFileSync(file, 'utf8');
          const where = pageUrl;

          // --- заголовок и описание ---
          const title = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]?.trim() ?? '';
          if (!title) errors.push(`${where}: нет <title>`);
          if (title.length > 65) errors.push(`${where}: title длиннее 65 символов (${title.length})`);
          else if (title.length > 60) warnings.push(`${where}: title длиннее рекомендованных 60 символов (${title.length})`);

          if (pageUrl !== '/404.html' && titles.has(title)) {
            errors.push(`${where}: дублируется title с ${titles.get(title)}`);
          } else {
            titles.set(title, where);
          }

          const descTag = html.match(/<meta[^>]+name=["']description["'][^>]*>/i)?.[0];
          const description = descTag ? (attr(descTag, 'content') ?? '') : '';
          if (!description) errors.push(`${where}: нет meta description`);
          else if (description.length < 120 || description.length > 170) {
            errors.push(`${where}: description ${description.length} символов, допустимо 120–170`);
          } else if (description.length < 140 || description.length > 160) {
            warnings.push(`${where}: description ${description.length} символов, рекомендуется 140–160`);
          }

          // --- обязательная структура страницы ---
          const h1count = (html.match(/<h1[\s>]/gi) ?? []).length;
          if (h1count !== 1) errors.push(`${where}: на странице ${h1count} тегов H1, должен быть один`);
          if (!/<link[^>]+rel=["']canonical["']/i.test(html)) errors.push(`${where}: нет canonical`);
          if (!/lang=["']ru["']/i.test(html)) errors.push(`${where}: нет lang="ru"`);
          if (!/<script[^>]+type=["']application\/ld\+json["']/i.test(html)) {
            errors.push(`${where}: нет разметки JSON-LD`);
          }

          // Блок прямого ответа обязателен для контентных страниц (§5.3)
          const isContent = /class="[^"]*\barticle-body\b/.test(html);
          if (isContent && !/data-answer="true"/.test(html)) {
            errors.push(`${where}: нет блока прямого ответа с data-answer="true"`);
          }

          // --- изображения без alt (§7.3) ---
          for (const tag of html.match(/<img[^>]*>/gi) ?? []) {
            if (attr(tag, 'alt') === null) errors.push(`${where}: <img> без alt — ${tag.slice(0, 80)}`);
            if (attr(tag, 'width') === null || attr(tag, 'height') === null) {
              warnings.push(`${where}: <img> без width/height — риск CLS: ${tag.slice(0, 80)}`);
            }
          }

          // --- битые внутренние ссылки (§7.3) ---
          for (const tag of html.match(/<a[^>]*>/gi) ?? []) {
            const href = attr(tag, 'href');
            if (!href) continue;
            if (/^(https?:|mailto:|tel:|#|data:)/i.test(href)) continue;

            const clean = href.split('#')[0].split('?')[0];
            if (!clean) continue;
            const target = clean.startsWith('/') ? clean : path.posix.join(pageUrl, clean);

            const candidates = [
              target,
              `${target.replace(/\/$/, '')}/index.html`,
              target.endsWith('/') ? `${target}index.html` : `${target}/index.html`,
            ];
            if (!candidates.some((candidate) => allFiles.has(candidate))) {
              errors.push(`${where}: битая внутренняя ссылка ${href}`);
            }
            if (clean.startsWith('/') && !clean.endsWith('/') && !path.extname(clean)) {
              errors.push(`${where}: ссылка без завершающего слеша — ${href} (§3.2)`);
            }
          }
        }

        // --- обязательные служебные файлы (§5.1, §5.2, §8.2) ---
        for (const required of ['/robots.txt', '/llms.txt', '/llms-full.txt', '/sitemap-index.xml']) {
          if (!allFiles.has(required)) errors.push(`нет файла ${required}`);
        }

        const robots = allFiles.has('/robots.txt')
          ? fs.readFileSync(path.join(distDir, 'robots.txt'), 'utf8')
          : '';
        for (const agent of ['GPTBot', 'ClaudeBot', 'PerplexityBot', 'YandexBot']) {
          if (!robots.includes(agent)) errors.push(`robots.txt: не разрешён ${agent}`);
        }
        if (!robots.includes(SITE.domain)) warnings.push('robots.txt: домен не совпадает с конфигурацией');

        for (const warning of warnings) logger.warn(warning);

        if (errors.length > 0) {
          const shown = errors.slice(0, 40).map((error) => `  • ${error}`).join('\n');
          const rest = errors.length > 40 ? `\n  … и ещё ${errors.length - 40}` : '';
          throw new Error(`Проверка сборки не пройдена (${errors.length}):\n${shown}${rest}`);
        }

        logger.info(
          `проверено страниц: ${htmlFiles.length}, ошибок нет, предупреждений: ${warnings.length}`,
        );
      },
    },
  };
}
