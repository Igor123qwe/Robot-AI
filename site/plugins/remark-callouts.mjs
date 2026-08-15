import { visit } from 'unist-util-visit';

/**
 * Блоки-врезки в теле статьи через directive-синтаксис — чтобы автор писал
 * обычный Markdown и не импортировал компоненты в каждый MDX:
 *
 *   :::warning[Заголовок]
 *   Текст врезки.
 *   :::
 *
 * Типы: warning, example, own-data, note, actual (§5.3, §6.3).
 */

const TYPES = {
  warning: { className: 'callout callout--warning', label: 'Важно' },
  note: { className: 'callout callout--note', label: 'На заметку' },
  example: { className: 'callout callout--example', label: 'Пример' },
  'own-data': { className: 'callout callout--own-data', label: 'Наши данные' },
  actual: { className: 'callout callout--actual', label: 'Актуально' },
};

export function remarkCallouts() {
  return (tree) => {
    visit(tree, (node) => {
      if (node.type !== 'containerDirective' && node.type !== 'leafDirective') return;
      const preset = TYPES[node.name];
      if (!preset) return;

      const label = extractLabel(node, preset.label);

      const data = node.data ?? (node.data = {});
      data.hName = 'aside';
      data.hProperties = {
        className: preset.className,
        ...(node.name === 'own-data' ? { 'data-own-data': 'true' } : {}),
      };

      node.children.unshift({
        type: 'paragraph',
        data: { hProperties: { className: 'callout__title' } },
        children: [{ type: 'text', value: label }],
      });
    });
  };
}

/** `:::warning[Свой заголовок]` → «Свой заголовок», иначе заголовок по умолчанию */
function extractLabel(node, fallback) {
  const first = node.children?.[0];
  if (first?.data?.directiveLabel) {
    node.children.shift();
    const text = first.children?.map((c) => c.value ?? '').join('').trim();
    if (text) return text;
  }
  return node.attributes?.title?.trim() || fallback;
}
