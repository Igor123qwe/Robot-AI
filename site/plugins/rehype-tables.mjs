import { visit } from 'unist-util-visit';

/**
 * Таблицы — основной носитель фактов (§5.3), и на мобильных они шире экрана.
 * Оборачиваем каждую в прокручиваемый контейнер с доступом с клавиатуры,
 * иначе горизонтальный скролл ломает вёрстку страницы и роняет CLS.
 */
export function rehypeTables() {
  return (tree) => {
    visit(tree, 'element', (node, index, parent) => {
      if (node.tagName !== 'table' || !parent || index === null) return;
      if (parent.type === 'element' && parent.properties?.className?.includes?.('table-scroll')) {
        return;
      }
      parent.children[index] = {
        type: 'element',
        tagName: 'div',
        properties: {
          className: ['table-scroll'],
          role: 'region',
          tabIndex: 0,
          'aria-label': 'Таблица, доступна прокрутка',
        },
        children: [node],
      };
    });
  };
}
