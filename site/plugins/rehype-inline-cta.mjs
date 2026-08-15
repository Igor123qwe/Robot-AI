import { CTA } from '../src/data/cta.mjs';

/**
 * Инлайн-CTA внутри тела статьи (§6.3): мягкий — после второго раздела H2,
 * прямой — в конце текста. Вставляются на этапе сборки, поэтому попадают в
 * серверный HTML и видны без JS.
 *
 * Работает только для статей базы знаний: на pillar-страницах и в услугах
 * свои конверсионные блоки.
 */

const el = (tagName, properties, children = []) => ({
  type: 'element',
  tagName,
  properties,
  children,
});

const text = (value) => ({ type: 'text', value });

const block = (kind, place) => {
  const cta = CTA[kind];
  return el('aside', { className: cta.className.split(' '), 'data-cta': kind }, [
    el('p', { className: ['cta__title'] }, [text(cta.title)]),
    el('p', {}, [text(cta.text)]),
    el('div', { className: ['cta__actions'] }, [
      el(
        'a',
        {
          className: cta.buttonClass.split(' '),
          href: cta.href,
          'data-goal': 'cta_click',
          'data-goal-place': place,
          ...(cta.href.startsWith('http') ? { rel: 'noopener' } : {}),
        },
        [text(cta.label)],
      ),
    ]),
  ]);
};

export function rehypeInlineCta() {
  return (tree, file) => {
    const path = String(file?.path ?? '').replace(/\\/g, '/');
    if (!path.includes('/src/content/baza/')) return;

    const children = tree.children ?? [];
    const h2Positions = [];
    children.forEach((node, i) => {
      if (node.type === 'element' && node.tagName === 'h2') h2Positions.push(i);
    });
    if (h2Positions.length === 0) return;

    // Прямой CTA всегда в конце тела статьи
    children.push(block('direct', 'article-end'));

    // Мягкий CTA — там, где заканчивается второй раздел, то есть перед третьим H2.
    // Если разделов меньше трёх, второй блок только мешал бы первому.
    if (h2Positions.length >= 3) {
      children.splice(h2Positions[2], 0, block('soft', 'article-mid'));
    }
  };
}
