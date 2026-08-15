const RU_DATE = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
const RU_LONG = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });

export const formatDate = (value) => RU_DATE.format(value instanceof Date ? value : new Date(value));
export const formatLong = (value) => RU_LONG.format(value instanceof Date ? value : new Date(value));
export const isoDate = (value) =>
  (value instanceof Date ? value : new Date(value)).toISOString().slice(0, 10);

export const plural = (n, forms) => {
  const abs = Math.abs(n) % 100;
  const tail = abs % 10;
  if (abs > 10 && abs < 20) return forms[2];
  if (tail > 1 && tail < 5) return forms[1];
  if (tail === 1) return forms[0];
  return forms[2];
};

export const money = (value) => `${Number(value).toLocaleString('ru-RU')} ₽`;

/** ~1500 знаков в минуту — средний темп чтения технического текста на русском */
export const readingTime = (body = '') => {
  const chars = body.replace(/\s+/g, ' ').trim().length;
  const minutes = Math.max(1, Math.round(chars / 1500));
  return `${minutes} ${plural(minutes, ['минута', 'минуты', 'минут'])}`;
};

export const countWords = (body = '') => body.trim().split(/\s+/).filter(Boolean).length;

/** Длина текста без frontmatter, разметки и служебных блоков — для валидации §7.3 */
export const contentLength = (body = '') =>
  body
    .replace(/^---[\s\S]*?---/, '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/[#>*_`|\-]/g, '')
    .replace(/\s+/g, ' ')
    .trim().length;

export const stripMd = (body = '') =>
  body
    .replace(/^---[\s\S]*?---/, '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^:::.*$/gm, '')
    .replace(/[*_`>]/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
