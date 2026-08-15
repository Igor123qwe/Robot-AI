import { SITE, AUTHOR, KNOWS_ABOUT, abs } from '../../site.config.mjs';

/**
 * Конструкторы JSON-LD (§5.4). Все узлы связаны через @id: Organization —
 * корень графа, Person автора переиспользуется во всех статьях, страницы
 * ссылаются на оба. Это то, что читают и Яндекс, и AI-системы.
 */

export const ORG_ID = `${SITE.url}/#organization`;
export const SITE_ID = `${SITE.url}/#website`;
export const PERSON_ID = AUTHOR.id;

const iso = (value) => (value instanceof Date ? value.toISOString() : new Date(value).toISOString());

export const organization = () => ({
  '@type': 'ProfessionalService',
  '@id': ORG_ID,
  name: SITE.brand,
  legalName: SITE.legalName,
  url: `${SITE.url}/`,
  description: `Сопровождение продаж на российских маркетплейсах: ведение кабинетов Ozon, Wildberries и Яндекс Маркета, маркировка «Честный Знак», классификация ТН ВЭД.`,
  taxID: SITE.inn,
  areaServed: { '@type': 'Country', name: 'Россия' },
  knowsAbout: KNOWS_ABOUT,
  founder: { '@id': PERSON_ID },
  email: SITE.email,
  ...(SITE.phone ? { telephone: SITE.phone } : {}),
  address: { '@type': 'PostalAddress', addressCountry: 'RU', addressLocality: SITE.city },
  sameAs: [SITE.tgPersonal, SITE.tgBot].filter(Boolean),
  image: abs('/og/default.png'),
  logo: abs('/images/logo.svg'),
});

export const website = () => ({
  '@type': 'WebSite',
  '@id': SITE_ID,
  url: `${SITE.url}/`,
  name: SITE.brand,
  inLanguage: 'ru-RU',
  publisher: { '@id': ORG_ID },
});

export const person = () => ({
  '@type': 'Person',
  '@id': PERSON_ID,
  name: AUTHOR.name,
  jobTitle: AUTHOR.jobTitle,
  worksFor: { '@id': ORG_ID },
  knowsAbout: AUTHOR.knowsAbout,
  description: AUTHOR.description,
  url: abs('/o-nas/'),
  sameAs: AUTHOR.sameAs,
});

export const breadcrumbs = (items) => ({
  '@type': 'BreadcrumbList',
  '@id': `${abs(items.at(-1)?.href ?? '/')}#breadcrumbs`,
  itemListElement: items.map((item, i) => ({
    '@type': 'ListItem',
    position: i + 1,
    name: item.label,
    item: abs(item.href),
  })),
});

/** Article / TechArticle для статьи базы знаний (§5.4) */
export const article = ({ data, url, dateModified, wordCount = 0, type = 'article' }) => ({
  '@type': type === 'reference' || type === 'howto' ? 'TechArticle' : 'Article',
  '@id': `${abs(url)}#article`,
  headline: data.h1 ?? data.title,
  name: data.title,
  description: data.description,
  abstract: data.direct_answer,
  inLanguage: 'ru-RU',
  url: abs(url),
  mainEntityOfPage: { '@type': 'WebPage', '@id': abs(url) },
  datePublished: iso(data.published),
  dateModified: iso(dateModified ?? data.updated),
  author: { '@id': PERSON_ID },
  publisher: { '@id': ORG_ID },
  isPartOf: { '@id': SITE_ID },
  ...(wordCount ? { wordCount } : {}),
  ...(data.entities?.length ? { about: data.entities.map((n) => ({ '@type': 'Thing', name: n })) } : {}),
  ...(data.tags?.length ? { keywords: data.tags.join(', ') } : {}),
  image: abs(ogPath(url)),
});

export const howTo = ({ name, description, steps, url }) => ({
  '@type': 'HowTo',
  '@id': `${abs(url)}#howto`,
  name,
  description,
  inLanguage: 'ru-RU',
  step: steps.map((step, i) => ({
    '@type': 'HowToStep',
    position: i + 1,
    name: step.name,
    text: step.text,
    ...(step.url ? { url: abs(step.url) } : { url: `${abs(url)}#step-${i + 1}` }),
  })),
});

export const faqPage = (faq, url) => ({
  '@type': 'FAQPage',
  '@id': `${abs(url)}#faq`,
  mainEntity: faq.map((item) => ({
    '@type': 'Question',
    name: item.q,
    acceptedAnswer: { '@type': 'Answer', text: item.a },
  })),
});

/** Справочник: собственные и сводные данные размечаем как Dataset (§5.3) */
export const dataset = ({ name, description, url, keywords, dateModified, measured }) => ({
  '@type': 'Dataset',
  '@id': `${abs(url)}#dataset`,
  name,
  description,
  url: abs(url),
  inLanguage: 'ru-RU',
  license: 'https://creativecommons.org/licenses/by/4.0/',
  creator: { '@id': ORG_ID },
  ...(dateModified ? { dateModified: iso(dateModified) } : {}),
  ...(measured ? { measurementTechnique: measured } : {}),
  ...(keywords?.length ? { keywords } : {}),
});

export const service = ({ data, url }) => ({
  '@type': 'Service',
  '@id': `${abs(url)}#service`,
  name: data.h1 ?? data.title,
  serviceType: data.title,
  description: data.description,
  url: abs(url),
  provider: { '@id': ORG_ID },
  areaServed: { '@type': 'Country', name: 'Россия' },
  ...(data.entities?.length ? { category: data.entities.join(', ') } : {}),
  offers: {
    '@type': 'Offer',
    url: abs(url),
    availability: 'https://schema.org/InStock',
    priceSpecification: {
      '@type': 'PriceSpecification',
      price: data.price_from,
      priceCurrency: 'RUB',
      valueAddedTaxIncluded: true,
      description: `от ${data.price_from.toLocaleString('ru-RU')} ₽ / ${data.price_unit}`,
    },
  },
});

/** Тарифы: OfferCatalog с числовыми ценами — без них страница не попадёт в ответы (§6.4) */
export const offerCatalog = (packages, url) => ({
  '@type': 'OfferCatalog',
  '@id': `${abs(url)}#catalog`,
  name: `Тарифы на сопровождение маркетплейсов — ${SITE.brand}`,
  url: abs(url),
  provider: { '@id': ORG_ID },
  itemListElement: packages.map((pkg, i) => ({
    '@type': 'Offer',
    position: i + 1,
    name: pkg.name,
    description: pkg.summary,
    price: pkg.price,
    priceCurrency: 'RUB',
    url: `${abs(url)}#${pkg.id}`,
    availability: 'https://schema.org/InStock',
    priceSpecification: {
      '@type': 'UnitPriceSpecification',
      price: pkg.price,
      priceCurrency: 'RUB',
      unitText: pkg.unit ?? 'месяц',
      valueAddedTaxIncluded: true,
    },
    itemOffered: {
      '@type': 'Service',
      name: pkg.name,
      provider: { '@id': ORG_ID },
      areaServed: { '@type': 'Country', name: 'Россия' },
    },
  })),
});

export const softwareApplication = ({ name, description, url }) => ({
  '@type': ['SoftwareApplication', 'WebApplication'],
  '@id': `${abs(url)}#app`,
  name,
  description,
  url: abs(url),
  applicationCategory: 'BusinessApplication',
  operatingSystem: 'Any',
  browserRequirements: 'Работает в любом современном браузере, расчёт идёт на устройстве',
  inLanguage: 'ru-RU',
  author: { '@id': ORG_ID },
  offers: { '@type': 'Offer', price: 0, priceCurrency: 'RUB' },
});

export const review = ({ author, text, url }) => ({
  '@type': 'Review',
  '@id': `${abs(url)}#review`,
  author: { '@type': 'Person', name: author },
  reviewBody: text,
  itemReviewed: { '@id': ORG_ID },
});

/** Путь к автогенерируемой OG-картинке страницы (§8.3) */
export const ogPath = (url) => {
  const clean = url.replace(/^\/+|\/+$/g, '');
  return clean ? `/og/${clean.replace(/\//g, '-')}.png` : '/og/default.png';
};

/** Собирает финальный граф: @context один раз, узлы — в @graph */
export const graph = (nodes) => ({
  '@context': 'https://schema.org',
  '@graph': nodes.filter(Boolean),
});
