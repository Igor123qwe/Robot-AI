// @ts-check
import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';
import remarkDirective from 'remark-directive';

import { SITE } from './site.config.mjs';
import { remarkCallouts } from './plugins/remark-callouts.mjs';
import { remarkAutolink } from './plugins/remark-autolink.mjs';
import { rehypeTables } from './plugins/rehype-tables.mjs';
import { rehypeInlineCta } from './plugins/rehype-inline-cta.mjs';
import { seoArtifacts } from './integrations/seo-artifacts.mjs';
import { ogImages } from './integrations/og-images.mjs';
import { buildChecks } from './integrations/build-checks.mjs';

export default defineConfig({
  site: SITE.url,
  // §3.2: завершающий слеш обязателен
  trailingSlash: 'always',
  build: {
    format: 'directory',
    // §8.1: критический CSS инлайном — стилей мало, отдельный запрос не нужен
    inlineStylesheets: 'always',
  },
  compressHTML: true,
  markdown: {
    remarkPlugins: [remarkDirective, remarkCallouts, remarkAutolink],
    rehypePlugins: [rehypeTables, rehypeInlineCta],
    shikiConfig: { theme: 'github-light', wrap: true },
  },
  integrations: [
    mdx(),
    // Порядок важен: og-images пишет картинки, seo-artifacts собирает карты
    // сайта и llms.txt, build-checks проверяет уже готовый dist.
    ogImages(),
    seoArtifacts(),
    buildChecks(),
  ],
  vite: {
    build: {
      // §8.1: бюджет JS на статью < 40 КБ — мелкие чанки не дробим
      assetsInlineLimit: 2048,
    },
  },
});
