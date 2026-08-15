/**
 * Единая точка правки реквизитов сайта.
 *
 * Плейсхолдеры из ТЗ (§0) заменяются здесь один раз — дальше значения
 * расходятся по шаблонам, Schema.org, robots.txt, llms.txt и sitemap.
 * Переменные окружения имеют приоритет: удобно для стенда и прод-сборки.
 */

const env = (key, fallback) => {
  const value = process.env[key];
  return value === undefined || value === '' ? fallback : value;
};

/** @type {string} — {DOMAIN}: только хост, без протокола и слеша */
export const DOMAIN = env('PUBLIC_DOMAIN', 'marketplace-support.ru');

export const SITE = {
  /** {BRAND} */
  brand: env('PUBLIC_BRAND', 'Маркетплейс Саппорт'),
  /** Юридически корректное название для Schema.org, если отличается от бренда */
  legalName: env('PUBLIC_LEGAL_NAME', 'ИП Иванов Иван Иванович'),
  domain: DOMAIN,
  url: `https://${DOMAIN}`,
  /** {INN} */
  inn: env('PUBLIC_INN', '000000000000'),
  /** {TG_PERSONAL} — личный Telegram для заявок */
  tgPersonal: env('PUBLIC_TG_PERSONAL', 'https://t.me/mp_support_expert'),
  /** {TG_BOT} — бот-лид-магнит */
  tgBot: env('PUBLIC_TG_BOT', 'https://t.me/mp_support_bot'),
  email: env('PUBLIC_EMAIL', 'hello@' + DOMAIN),
  phone: env('PUBLIC_PHONE', ''),
  city: env('PUBLIC_CITY', 'Москва'),
  /** Год начала практики — используется в описаниях и llms.txt */
  since: env('PUBLIC_SINCE', '2019'),
  /** Сколько SKU обработано — фактический показатель для E-E-A-T */
  skuProcessed: env('PUBLIC_SKU_PROCESSED', '12 000'),
  /** Эндпоинт приёма форм (см. server/forms.mjs) */
  formEndpoint: env('PUBLIC_FORM_ENDPOINT', '/api/lead'),
  /** Счётчик Яндекс.Метрики; пустая строка — счётчик не подключается */
  metrikaId: env('PUBLIC_METRIKA_ID', ''),
  /** GA4, опционально вторым счётчиком */
  ga4Id: env('PUBLIC_GA4_ID', ''),
  locale: 'ru_RU',
  lang: 'ru',
};

/** Автор-эксперт: один @id переиспользуется во всех статьях (§5.4) */
export const AUTHOR = {
  id: `${SITE.url}/o-nas/#person`,
  key: 'main',
  name: env('PUBLIC_AUTHOR_NAME', 'Иван Иванов'),
  jobTitle: 'Эксперт по маркетплейсам и ВЭД',
  photo: '/images/author.svg',
  short: 'Веду кабинеты продавцов на Ozon, Wildberries и Яндекс Маркете, отвечаю за маркировку и таможенную классификацию.',
  description:
    'Более 7 лет в закупках и ВЭД: классификация ТН ВЭД, импорт из Китая, таможенное оформление. С 2019 года веду кабинеты селлеров на российских маркетплейсах и сопровождаю регистрацию в «Честном Знаке».',
  knowsAbout: [
    'Честный Знак',
    'маркировка товаров',
    'ТН ВЭД',
    'таможенное оформление',
    'Ozon',
    'Wildberries',
    'Яндекс Маркет',
    'FBO',
    'FBS',
    'юнит-экономика маркетплейсов',
  ],
  sameAs: [SITE.tgPersonal].filter(Boolean),
};

/** Темы, по которым домен должен считаться релевантным источником (§5.4) */
export const KNOWS_ABOUT = [
  'Честный Знак',
  'маркировка товаров',
  'Data Matrix',
  'ТН ВЭД',
  'Ozon',
  'Wildberries',
  'Яндекс Маркет',
  'FBO',
  'FBS',
  'юнит-экономика маркетплейсов',
  'импорт из Китая',
  'таможенное оформление',
  'система «Меркурий»',
];

/** Pillar-разделы базы знаний. Порядок влияет на меню и llms.txt. */
export const PILLARS = [
  { slug: 'chestnyj-znak', title: 'Честный Знак', service: 'markirovka-chestnyj-znak' },
  { slug: 'tn-ved', title: 'ТН ВЭД и импорт', service: 'tn-ved-klassifikaciya' },
  { slug: 'ozon', title: 'Ozon', service: 'vedenie-ozon' },
  { slug: 'wildberries', title: 'Wildberries', service: 'vedenie-wildberries' },
  { slug: 'yandex-market', title: 'Яндекс Маркет', service: 'vedenie-yandex-market' },
  { slug: 'unit-ekonomika', title: 'Юнит-экономика', service: 'audit-kabineta' },
];

export const PILLAR_SLUGS = PILLARS.map((p) => p.slug);

/** Типы статей → шаблоны (§4.3) */
export const ARTICLE_TYPES = ['article', 'howto', 'reference', 'troubleshoot', 'comparison'];

export const NAV = [
  { href: '/uslugi/', label: 'Услуги' },
  { href: '/tarify/', label: 'Тарифы' },
  { href: '/baza/', label: 'База знаний' },
  { href: '/instrumenty/', label: 'Калькуляторы' },
  { href: '/kejsy/', label: 'Кейсы' },
  { href: '/o-nas/', label: 'Об экспертизе' },
];

export const abs = (path = '/') => new URL(path, SITE.url).href;
