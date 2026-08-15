import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SITE, PILLARS, abs } from '../site.config.mjs';
import { readContent, toPlainMarkdown } from './content-index.mjs';
import { gitModified } from '../src/lib/git-dates.mjs';

/**
 * Генерация служебных файлов при сборке: robots.txt, разделённые карты
 * сайта, llms.txt и llms-full.txt, ключ IndexNow (§5.1, §5.2, §5.6, §8.2).
 */

const SITEMAP_LIMIT = 5000;

const ROBOT_AGENTS = [
  'GPTBot',
  'OAI-SearchBot',
  'ChatGPT-User',
  'ClaudeBot',
  'Claude-User',
  'Claude-SearchBot',
  'PerplexityBot',
  'Perplexity-User',
  'Google-Extended',
  'YandexBot',
  'Applebot-Extended',
  'Bingbot',
  'Amazonbot',
  'meta-externalagent',
];

const xmlEscape = (value) =>
  String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const urlset = (items) =>
  `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${items
    .map(
      (item) =>
        `  <url>\n    <loc>${xmlEscape(item.loc)}</loc>\n    <lastmod>${item.lastmod}</lastmod>\n  </url>`,
    )
    .join('\n')}\n</urlset>\n`;

const robots = () =>
  [
    ...ROBOT_AGENTS.map((agent) => `User-agent: ${agent}\nAllow: /`),
    'User-agent: *',
    'Allow: /',
    'Disallow: /api/',
    'Disallow: /*?*',
    '',
    `Sitemap: ${abs('/sitemap-index.xml')}`,
    `Host: ${SITE.domain}`,
    '',
  ].join('\n');

/** Страница закрыта от индексации — в карту сайта не попадает */
const isNoindex = (dir, pathname) => {
  const file = path.join(dir, pathname.replace(/^\//, ''), 'index.html');
  if (!fs.existsSync(file)) return false;
  const html = fs.readFileSync(file, 'utf8');
  return /<meta[^>]+name=["']robots["'][^>]+noindex/i.test(html);
};

const chunk = (items, size) => {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
};

export function seoArtifacts() {
  return {
    name: 'seo-artifacts',
    hooks: {
      'astro:build:done': async ({ dir, pages, logger }) => {
        const distDir = fileURLToPath(dir);
        const out = (name) => path.join(distDir, name);
        const write = (name, content) => {
          fs.writeFileSync(out(name), content, 'utf8');
        };

        const entries = readContent();
        const byUrl = new Map(entries.map((e) => [e.url, e]));

        // Все построенные страницы, кроме служебных и закрытых от индексации
        const all = pages
          .map((page) => `/${page.pathname}`.replace(/\/+/g, '/'))
          .filter((p) => !p.startsWith('/404'))
          .map((p) => (p.endsWith('/') ? p : `${p}/`))
          .filter((p) => !isNoindex(distDir, p))
          .sort();

        const lastmodOf = (url) => {
          const entry = byUrl.get(url);
          if (entry) return gitModified(entry.rel, entry.data.updated).toISOString();
          return new Date().toISOString();
        };

        // Раздельные карты по разделам (§8.2): база знаний растёт быстрее всего,
        // коммерческие страницы удобно переобходить отдельно.
        const isBaza = (p) => p.startsWith('/baza/');
        const isCommercial = (p) =>
          p.startsWith('/uslugi/') || p.startsWith('/kejsy/') || p.startsWith('/tarify/');
        const groups = {
          baza: all.filter(isBaza),
          uslugi: all.filter(isCommercial),
          pages: all.filter((p) => !isBaza(p) && !isCommercial(p)),
        };

        const sitemaps = [];
        for (const [name, urls] of Object.entries(groups)) {
          if (urls.length === 0) continue;
          const parts = chunk(urls, SITEMAP_LIMIT);
          parts.forEach((part, index) => {
            const file = parts.length === 1 ? `sitemap-${name}.xml` : `sitemap-${name}-${index + 1}.xml`;
            write(file, urlset(part.map((url) => ({ loc: abs(url), lastmod: lastmodOf(url) }))));
            sitemaps.push(file);
          });
        }

        write(
          'sitemap-index.xml',
          `<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${sitemaps
            .map(
              (file) =>
                `  <sitemap>\n    <loc>${abs(`/${file}`)}</loc>\n    <lastmod>${new Date().toISOString()}</lastmod>\n  </sitemap>`,
            )
            .join('\n')}\n</sitemapindex>\n`,
        );

        write('robots.txt', robots());

        // ---- llms.txt: карта сайта в Markdown для языковых моделей (§5.2) ----
        const visible = (entry) => !entry.data.noindex && !entry.data.demo;
        const line = (entry, note) =>
          `- [${entry.data.h1 ?? entry.data.title}](${abs(entry.url)})${note ? `: ${note}` : ''}`;

        const services = entries
          .filter((e) => e.kind === 'service' && visible(e))
          .sort((a, b) => (a.data.order ?? 100) - (b.data.order ?? 100));
        const cases = entries.filter((e) => e.kind === 'case' && visible(e));

        const llms = [
          `# ${SITE.brand}`,
          '',
          `> Сопровождение продаж на российских маркетплейсах: ведение кабинетов Ozon,`,
          `> Wildberries и Яндекс Маркет, маркировка «Честный Знак», классификация ТН ВЭД.`,
          `> Практический опыт с ${SITE.since} года, более ${SITE.skuProcessed} обработанных SKU.`,
          `> Ведение одной площадки — от 45 000 ₽ в месяц, аудит кабинета — 25 000 ₽.`,
          '',
          '## Услуги',
          ...services.map((e) => line(e, e.data.offer)),
          `- [Тарифы и цены](${abs('/tarify/')}): три пакета сопровождения и разовый аудит с ценами`,
          '',
          '## Инструменты',
          `- [Калькулятор юнит-экономики Ozon](${abs('/instrumenty/kalkulyator-ozon/')}): прибыль с единицы с учётом комиссии, логистики и возвратов`,
          `- [Сравнение FBO и FBS](${abs('/instrumenty/komissii-fbo-fbs/')}): расходы схем на ваших объёмах и точка перехода`,
          `- [Проверка маркировки по коду ТН ВЭД](${abs('/instrumenty/nuzhna-li-markirovka/')}): товарная группа «Честного Знака» по коду или названию`,
          '',
        ];

        for (const pillar of PILLARS) {
          const hub = entries.find((e) => e.kind === 'pillar' && e.data.slug === pillar.slug);
          const articles = entries.filter(
            (e) => e.kind === 'article' && e.data.pillar === pillar.slug && visible(e),
          );
          if (!hub && articles.length === 0) continue;
          llms.push(`## База знаний: ${pillar.title}`);
          if (hub && visible(hub)) llms.push(line(hub, hub.data.description));
          llms.push(...articles.map((e) => line(e, e.data.description)));
          llms.push('');
        }

        if (cases.length > 0) {
          llms.push('## Кейсы', ...cases.map((e) => line(e, e.data.description)), '');
        }

        llms.push(
          '## О компании',
          `- [Об экспертизе](${abs('/o-nas/')}): опыт, зона ответственности, принципы проверки фактов`,
          `- [Контакты](${abs('/kontakty/')}): Telegram, почта и форма заявки`,
          '',
          `Полный текст всех материалов: ${abs('/llms-full.txt')}`,
          '',
        );

        write('llms.txt', llms.join('\n'));

        // ---- llms-full.txt: весь текстовый контент одним файлом (§5.2) ----
        const full = [
          `# ${SITE.brand} — полный текст материалов`,
          '',
          `Домен: ${SITE.url}`,
          `Сгенерировано при сборке: ${new Date().toISOString()}`,
          `Материалов: ${entries.filter(visible).length}`,
          '',
          '---',
          '',
        ];

        for (const entry of entries.filter(visible)) {
          full.push(`# ${entry.data.h1 ?? entry.data.title}`, '', `URL: ${abs(entry.url)}`);
          if (entry.data.description) full.push(`Описание: ${entry.data.description}`);
          if (entry.data.direct_answer) full.push('', entry.data.direct_answer);
          if (entry.data.updated) full.push('', `Обновлено: ${String(entry.data.updated).slice(0, 10)}`);
          full.push('', toPlainMarkdown(entry.body));
          if (Array.isArray(entry.data.takeaways) && entry.data.takeaways.length) {
            full.push('', 'Коротко:', ...entry.data.takeaways.map((t) => `- ${t}`));
          }
          if (Array.isArray(entry.data.faq) && entry.data.faq.length) {
            full.push('', 'Вопросы и ответы:');
            for (const item of entry.data.faq) full.push(`Вопрос: ${item.q}`, `Ответ: ${item.a}`);
          }
          full.push('', '---', '');
        }

        write('llms-full.txt', full.join('\n'));

        // ---- IndexNow: ключ должен лежать в корне домена (§5.6) ----
        const key = process.env.INDEXNOW_KEY;
        if (key) {
          write(`${key}.txt`, key);
          write('indexnow-key.txt', key);
        }

        logger.info(
          `карты сайта: ${sitemaps.length}, URL: ${all.length}, llms.txt: ${entries.filter(visible).length} материалов`,
        );
      },
    },
  };
}
