import { visit, EXIT } from 'unist-util-visit';
import { getEntityMap } from './entity-map.mjs';

/**
 * Автоматическая перелинковка по словарю сущностей (§7.2).
 *
 * Первое вхождение сущности в теле статьи превращается в ссылку, если
 * целевая страница существует и это не текущая страница. Лимиты: не более
 * 8 автоссылок на статью и не более одной на одну и ту же цель.
 */

const MAX_LINKS_PER_PAGE = 8;
const SKIP_PARENTS = new Set([
  'link',
  'linkReference',
  'heading',
  'code',
  'inlineCode',
  'html',
  'definition',
  'mdxJsxFlowElement',
  'mdxJsxTextElement',
  'mdxFlowExpression',
  'mdxTextExpression',
]);

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Границы слова с поддержкой кириллицы: \b в JS про латиницу не знает
const boundaried = (entity) =>
  new RegExp(`(?<![\\p{L}\\p{N}_])${escapeRe(entity)}(?![\\p{L}\\p{N}_])`, 'iu');

const currentUrl = (file) => {
  const fm = file?.data?.astro?.frontmatter;
  if (!fm?.slug) return null;
  const dir = String(file.path ?? '').replace(/\\/g, '/');
  if (dir.includes('/src/content/uslugi/')) return `/uslugi/${fm.slug}/`;
  if (dir.includes('/src/content/kejsy/')) return `/kejsy/${fm.slug}/`;
  return `/baza/${fm.slug}/`;
};

export function remarkAutolink() {
  return (tree, file) => {
    const entities = getEntityMap();
    if (entities.length === 0) return;

    const self = currentUrl(file);
    const usedTargets = new Set();
    const usedEntities = new Set();
    let linksAdded = 0;

    visit(tree, 'text', (node, index, parent) => {
      if (linksAdded >= MAX_LINKS_PER_PAGE) return EXIT;
      if (!parent || index === null || SKIP_PARENTS.has(parent.type)) return undefined;

      for (const item of entities) {
        if (linksAdded >= MAX_LINKS_PER_PAGE) break;
        if (usedTargets.has(item.url)) continue;
        if (usedEntities.has(item.entity.toLowerCase())) continue;
        if (self && item.url === self) continue;

        const match = boundaried(item.entity).exec(node.value);
        if (!match) continue;

        const before = node.value.slice(0, match.index);
        const hit = node.value.slice(match.index, match.index + match[0].length);
        const after = node.value.slice(match.index + match[0].length);

        const replacement = [];
        if (before) replacement.push({ type: 'text', value: before });
        replacement.push({
          type: 'link',
          url: item.url,
          data: { hProperties: { 'data-autolink': 'true' } },
          children: [{ type: 'text', value: hit }],
        });
        if (after) replacement.push({ type: 'text', value: after });

        parent.children.splice(index, 1, ...replacement);
        usedTargets.add(item.url);
        usedEntities.add(item.entity.toLowerCase());
        linksAdded += 1;

        // Хвост после ссылки обходится следующей итерацией visit
        return index + replacement.length - 1;
      }
      return undefined;
    });

    file.data.astro ??= {};
    file.data.astro.frontmatter ??= {};
    file.data.astro.frontmatter.autolinks = linksAdded;
  };
}
