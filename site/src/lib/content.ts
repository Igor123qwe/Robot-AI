import { getCollection, type CollectionEntry } from 'astro:content';
import { PILLARS } from '../../site.config.mjs';

/** Выборки по коллекциям и автоматические связи между страницами (§6.1, §6.3). */

export type Article = CollectionEntry<'baza'>;
export type Pillar = CollectionEntry<'pillars'>;
export type Service = CollectionEntry<'uslugi'>;
export type Case = CollectionEntry<'kejsy'>;

export const allArticles = async (): Promise<Article[]> => {
  const items = await getCollection('baza');
  return items.sort((a, b) => +b.data.published - +a.data.published);
};

export const allPillars = async (): Promise<Pillar[]> => {
  const items = await getCollection('pillars');
  const order = new Map(PILLARS.map((p, i) => [p.slug, i]));
  return items.sort(
    (a, b) => (order.get(a.data.slug) ?? 99) - (order.get(b.data.slug) ?? 99),
  );
};

export const allServices = async (): Promise<Service[]> => {
  const items = await getCollection('uslugi');
  return items.sort((a, b) => a.data.order - b.data.order);
};

export const allCases = async (): Promise<Case[]> => {
  const items = await getCollection('kejsy');
  return items.sort((a, b) => +b.data.published - +a.data.published);
};

export const articlesByPillar = async (pillar: string): Promise<Article[]> => {
  const items = await allArticles();
  return items.filter((a) => a.data.pillar === pillar);
};

export const serviceBySlug = async (slug?: string): Promise<Service | undefined> => {
  if (!slug) return undefined;
  const items = await allServices();
  return items.find((s) => s.data.slug === slug);
};

export const pillarBySlug = async (slug?: string): Promise<Pillar | undefined> => {
  if (!slug) return undefined;
  const items = await allPillars();
  return items.find((p) => p.data.slug === slug);
};

/**
 * «Читайте также»: 3–4 ссылки внутри кластера, подбор по тегам и сущностям.
 * Свой pillar весит больше тега — соседи по кластеру важнее случайного
 * совпадения метки.
 */
export const relatedArticles = async (current: Article, limit = 4): Promise<Article[]> => {
  const items = await allArticles();
  const tags = new Set(current.data.tags);
  const entities = new Set(current.data.entities.map((e) => e.toLowerCase()));

  return items
    .filter((a) => a.id !== current.id && !a.data.noindex)
    .map((a) => {
      let score = a.data.pillar === current.data.pillar ? 3 : 0;
      for (const tag of a.data.tags) if (tags.has(tag)) score += 2;
      for (const entity of a.data.entities) if (entities.has(entity.toLowerCase())) score += 1;
      return { entry: a, score };
    })
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score || +b.entry.data.published - +a.entry.data.published)
    .slice(0, limit)
    .map((x) => x.entry);
};

/** Метка pillar для карточек и OG-картинок */
export const pillarTitle = (slug?: string): string =>
  PILLARS.find((p) => p.slug === slug)?.title ?? 'База знаний';

/** Услуга, связанная с pillar по умолчанию — фолбэк, если в статье не задана */
export const pillarService = (slug?: string): string | undefined =>
  PILLARS.find((p) => p.slug === slug)?.service;

export const TYPE_LABELS: Record<string, string> = {
  article: 'Разбор',
  howto: 'Инструкция',
  reference: 'Справочник',
  troubleshoot: 'Решение ошибки',
  comparison: 'Сравнение',
};
