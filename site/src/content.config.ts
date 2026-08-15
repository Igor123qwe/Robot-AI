import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';
import { ARTICLE_TYPES, PILLAR_SLUGS } from '../site.config.mjs';

/**
 * Схема frontmatter (§7.1) и часть правил валидации сборки (§7.3).
 * Всё, что можно проверить по одному файлу, проверяется здесь — сборка
 * падает с указанием файла и поля. Правила, которым нужен весь корпус
 * (дубли slug/title, битые ссылки), живут в scripts/validate-content.mjs.
 */

const words = (value: string) => value.trim().split(/\s+/).filter(Boolean).length;

const directAnswer = z
  .string()
  .refine((v) => words(v) >= 30, {
    message: 'direct_answer короче 30 слов — блок прямого ответа не самодостаточен (§5.3)',
  })
  .refine((v) => words(v) <= 90, {
    message: 'direct_answer длиннее 90 слов — LLM цитируют только компактный фрагмент (§5.3)',
  });

const title = z
  .string()
  .min(10, { message: 'title короче 10 символов' })
  .max(65, { message: 'title длиннее 65 символов (§7.3)' });

const description = z
  .string()
  .min(120, { message: 'description короче 120 символов (§7.3)' })
  .max(170, { message: 'description длиннее 170 символов (§7.3)' });

const slug = z
  .string()
  .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/, {
    message: 'slug: только строчная латиница, цифры и дефис (§3.2)',
  });

const faqItem = z.object({
  q: z.string().min(5),
  a: z.string().min(10),
});

const changelogItem = z.object({
  date: z.coerce.date(),
  text: z.string().min(5),
});

/** Собственные данные (§5.3) — размечаются как Dataset */
const ownData = z
  .object({
    title: z.string(),
    description: z.string().optional(),
    measured: z.string().optional(),
    rows: z.array(z.object({ label: z.string(), value: z.string() })).optional(),
  })
  .optional();

const baza = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/baza' }),
  schema: z.object({
    title,
    h1: z.string().min(5).optional(),
    description,
    slug,
    pillar: z.enum(PILLAR_SLUGS as [string, ...string[]]),
    type: z.enum(ARTICLE_TYPES as [string, ...string[]]),
    direct_answer: directAnswer,
    related_service: z.string().optional(),
    tags: z.array(z.string()).default([]),
    entities: z.array(z.string()).default([]),
    faq: z.array(faqItem).default([]),
    /** Блок «Коротко» в конце статьи (§6.3): 3–5 буллетов */
    takeaways: z
      .array(z.string())
      .min(3, { message: 'takeaways: нужно 3–5 буллетов для блока «Коротко» (§6.3)' })
      .max(5, { message: 'takeaways: не больше 5 буллетов (§6.3)' }),
    published: z.coerce.date(),
    updated: z.coerce.date(),
    author: z.string().default('main'),
    has_own_data: z.boolean().default(false),
    own_data: ownData,
    /** Датированный список правок для регуляторных тем (§5.5) */
    changelog: z.array(changelogItem).default([]),
    /** Шаги для шаблона howto — идут в Schema HowTo */
    steps: z
      .array(z.object({ name: z.string(), text: z.string(), url: z.string().optional() }))
      .default([]),
    /** Источники данных: выводятся в подвале статьи */
    sources: z.array(z.object({ title: z.string(), url: z.string().url().optional() })).default([]),
    /** Демонстрационный текст: рендерится с предупреждением и noindex */
    demo: z.boolean().default(false),
    noindex: z.boolean().default(false),
    image: z.string().optional(),
  }),
});

const pillars = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/pillars' }),
  schema: z.object({
    title,
    h1: z.string().min(5).optional(),
    description,
    slug: z.enum(PILLAR_SLUGS as [string, ...string[]]),
    direct_answer: directAnswer,
    related_service: z.string().optional(),
    entities: z.array(z.string()).default([]),
    tags: z.array(z.string()).default([]),
    faq: z.array(faqItem).default([]),
    order: z.number().default(100),
    published: z.coerce.date(),
    updated: z.coerce.date(),
    author: z.string().default('main'),
    demo: z.boolean().default(false),
    noindex: z.boolean().default(false),
  }),
});

const uslugi = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/uslugi' }),
  schema: z.object({
    title,
    h1: z.string().min(5).optional(),
    /** Подзаголовок-оффер под H1 (§6.2) */
    offer: z.string().min(20),
    description,
    slug,
    direct_answer: directAnswer,
    order: z.number().default(100),
    price_from: z.number().int().positive(),
    price_unit: z.string().default('месяц'),
    /** Кому подходит — 3–4 сегмента (§6.2) */
    segments: z
      .array(z.object({ title: z.string(), text: z.string() }))
      .min(3, { message: 'segments: нужно 3–4 сегмента «кому подходит» (§6.2)' })
      .max(4),
    /** Что входит — таблица работ с периодичностью (§6.2) */
    works: z
      .array(z.object({ work: z.string(), period: z.string(), result: z.string().optional() }))
      .min(4),
    /** Как проходит работа — 4–5 шагов, идут в Schema HowTo (§6.2) */
    steps: z
      .array(z.object({ name: z.string(), text: z.string() }))
      .min(4, { message: 'steps: нужно 4–5 шагов «как проходит работа» (§6.2)' })
      .max(5),
    /** 6–8 вопросов в Schema FAQPage (§6.2) */
    faq: z
      .array(faqItem)
      .min(6, { message: 'faq: на странице услуги нужно 6–8 вопросов (§6.2)' })
      .max(8),
    pillar: z.enum(PILLAR_SLUGS as [string, ...string[]]).optional(),
    entities: z.array(z.string()).default([]),
    tags: z.array(z.string()).default([]),
    published: z.coerce.date(),
    updated: z.coerce.date(),
    demo: z.boolean().default(false),
    noindex: z.boolean().default(false),
  }),
});

const kejsy = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/kejsy' }),
  schema: z.object({
    title,
    h1: z.string().min(5).optional(),
    description,
    slug,
    direct_answer: directAnswer,
    niche: z.string(),
    marketplace: z.array(z.string()).default([]),
    period: z.string(),
    related_service: z.string().optional(),
    pillar: z.enum(PILLAR_SLUGS as [string, ...string[]]).optional(),
    /** Метрики результата — выводятся плиткой и в own-data */
    results: z.array(z.object({ label: z.string(), value: z.string() })).min(2),
    review: z
      .object({ author: z.string(), role: z.string().optional(), text: z.string() })
      .optional(),
    entities: z.array(z.string()).default([]),
    tags: z.array(z.string()).default([]),
    takeaways: z.array(z.string()).min(3).max(5),
    published: z.coerce.date(),
    updated: z.coerce.date(),
    author: z.string().default('main'),
    demo: z.boolean().default(false),
    noindex: z.boolean().default(false),
  }),
});

export const collections = { baza, pillars, uslugi, kejsy };
