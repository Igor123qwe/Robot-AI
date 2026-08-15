import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { SITE, PILLARS } from '../site.config.mjs';
import { readContent, ROOT } from './content-index.mjs';

/**
 * Автогенерация OG-картинок 1200×630 при сборке (§8.3).
 *
 * Кэш лежит в .og-cache и ключуется хэшем заголовка: при неизменном
 * заголовке картинка не перерисовывается, поэтому пересборка сайта из 200
 * статей не превращается в многоминутный рендер.
 */

const CACHE = path.join(ROOT, '.og-cache');
const WIDTH = 1200;
const HEIGHT = 630;

const FONT_CANDIDATES = [
  [path.join(ROOT, 'assets/fonts/og-regular.ttf'), path.join(ROOT, 'assets/fonts/og-bold.ttf')],
  ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'],
  [
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
  ],
  ['/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'],
];

const findFonts = () => {
  for (const [regular, bold] of FONT_CANDIDATES) {
    if (fs.existsSync(regular) && fs.existsSync(bold)) {
      return [
        { name: 'OG', data: fs.readFileSync(regular), weight: 400, style: 'normal' },
        { name: 'OG', data: fs.readFileSync(bold), weight: 700, style: 'normal' },
      ];
    }
  }
  return null;
};

/** Путь картинки для URL страницы — зеркалит ogPath() из src/lib/schema.mjs */
const ogFileName = (url) => {
  const clean = url.replace(/^\/+|\/+$/g, '');
  return clean ? `${clean.replace(/\//g, '-')}.png` : 'default.png';
};

const sectionLabel = (url, entry) => {
  if (url === '/') return 'Сопровождение маркетплейсов';
  if (url.startsWith('/uslugi/')) return 'Услуга';
  if (url.startsWith('/tarify/')) return 'Тарифы';
  if (url.startsWith('/kejsy/')) return 'Кейс';
  if (url.startsWith('/instrumenty/')) return 'Калькулятор';
  if (url.startsWith('/baza/')) {
    // Для статьи метка берётся из её pillar, для самого хаба — из его slug
    const slug = entry?.data.pillar ?? url.split('/')[2];
    return PILLARS.find((p) => p.slug === slug)?.title ?? 'База знаний';
  }
  return SITE.brand;
};

const decode = (value) =>
  value
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&mdash;/g, '—')
    .replace(/&laquo;/g, '«')
    .replace(/&raquo;/g, '»')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)))
    .replace(/\s+/g, ' ')
    .trim();

const readHeading = (distDir, url) => {
  const file = path.join(distDir, url.replace(/^\//, ''), 'index.html');
  if (!fs.existsSync(file)) return null;
  const html = fs.readFileSync(file, 'utf8');
  const h1 = html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i);
  if (h1) return decode(h1[1]);
  const title = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return title ? decode(title[1]) : null;
};

/** Карточка в формате, который понимает satori: flexbox и ничего лишнего */
const card = (heading, label) => ({
  type: 'div',
  props: {
    style: {
      width: '100%',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'space-between',
      padding: '64px',
      backgroundColor: '#12203f',
      backgroundImage: 'linear-gradient(135deg, #12203f 0%, #1a4fd6 100%)',
      fontFamily: 'OG',
      color: '#ffffff',
    },
    children: [
      {
        type: 'div',
        props: {
          style: { display: 'flex', alignItems: 'center', fontSize: 30, opacity: 0.9 },
          children: [
            {
              type: 'div',
              props: {
                style: {
                  width: 44,
                  height: 44,
                  borderRadius: 12,
                  backgroundColor: '#ffffff',
                  color: '#1a4fd6',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 22,
                  fontWeight: 700,
                  marginRight: 18,
                },
                children: 'МС',
              },
            },
            { type: 'div', props: { children: SITE.brand } },
          ],
        },
      },
      {
        type: 'div',
        props: {
          style: {
            display: 'flex',
            fontSize: heading.length > 90 ? 52 : heading.length > 55 ? 62 : 72,
            fontWeight: 700,
            lineHeight: 1.15,
            letterSpacing: '-0.02em',
          },
          children: heading.length > 130 ? `${heading.slice(0, 127)}…` : heading,
        },
      },
      {
        type: 'div',
        props: {
          style: {
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: 28,
            opacity: 0.85,
          },
          children: [
            { type: 'div', props: { children: label } },
            { type: 'div', props: { children: SITE.domain } },
          ],
        },
      },
    ],
  },
});

/** Запасной вариант без текста: лучше фирменная заливка, чем 404 на og:image */
const plainSvg = () =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}">` +
  '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0%" stop-color="#12203f"/><stop offset="100%" stop-color="#1a4fd6"/>' +
  `</linearGradient></defs><rect width="${WIDTH}" height="${HEIGHT}" fill="url(#g)"/></svg>`;

export function ogImages() {
  return {
    name: 'og-images',
    hooks: {
      'astro:build:done': async ({ dir, pages, logger }) => {
        const distDir = fileURLToPath(dir);
        const outDir = path.join(distDir, 'og');
        fs.mkdirSync(outDir, { recursive: true });
        fs.mkdirSync(CACHE, { recursive: true });

        const fonts = findFonts();
        let satori;
        let Resvg;
        if (fonts) {
          try {
            ({ default: satori } = await import('satori'));
            ({ Resvg } = await import('@resvg/resvg-js'));
          } catch (error) {
            logger.warn(`satori/resvg недоступны, OG-картинки не генерируются: ${error.message}`);
          }
        } else {
          logger.warn('Не найден шрифт для OG-картинок — положите ttf в assets/fonts/og-regular.ttf и og-bold.ttf');
        }

        const entries = new Map(readContent().map((e) => [e.url, e]));
        const urls = pages
          .map((page) => `/${page.pathname}`.replace(/\/+/g, '/'))
          .map((p) => (p.endsWith('/') ? p : `${p}/`));
        if (!urls.includes('/')) urls.push('/');

        let generated = 0;
        let cached = 0;

        for (const url of urls) {
          const entry = entries.get(url);
          const heading = entry?.data.h1 ?? entry?.data.title ?? readHeading(distDir, url) ?? SITE.brand;
          const label = sectionLabel(url, entry);
          const target = path.join(outDir, ogFileName(url));

          if (!satori || !Resvg) {
            if (!fs.existsSync(target)) {
              // Без шрифта пишем нейтральный фон — ссылка og:image остаётся живой
              const { Resvg: R } = await import('@resvg/resvg-js').catch(() => ({ Resvg: null }));
              if (R) fs.writeFileSync(target, new R(plainSvg()).render().asPng());
            }
            continue;
          }

          const key = crypto
            .createHash('sha1')
            .update(`v1|${heading}|${label}|${SITE.brand}|${SITE.domain}`)
            .digest('hex');
          const cacheFile = path.join(CACHE, `${key}.png`);

          if (fs.existsSync(cacheFile)) {
            fs.copyFileSync(cacheFile, target);
            cached += 1;
            continue;
          }

          const svg = await satori(card(heading, label), { width: WIDTH, height: HEIGHT, fonts });
          const png = new Resvg(svg, { fitTo: { mode: 'width', value: WIDTH } }).render().asPng();
          fs.writeFileSync(cacheFile, png);
          fs.writeFileSync(target, png);
          generated += 1;
        }

        logger.info(`OG-картинки: ${generated} новых, ${cached} из кэша`);
      },
    },
  };
}
