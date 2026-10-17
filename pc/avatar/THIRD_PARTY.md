# Чужой код в pc/avatar и откуда он

Всё ниже — под лицензией MIT. Она разрешает брать, менять и раздавать код
при одном условии: строка об авторских правах и текст разрешения (он
приведён в конце) идут вместе с кодом. Этот файл и есть то место.

## Код, лежащий здесь как есть

**three.js r169** — `vendor/three.module.min.js`, `vendor/loaders/`,
`vendor/utils/`, `vendor/misc/GPUComputationRenderer.js`,
`vendor/postprocessing/Pass.js` (два последних — для шара, `orb.html`).
Взяты с тега r169 (https://github.com/mrdoob/three.js/tree/r169) без правок.

    Copyright © 2010-2024 three.js authors

**@pixiv/three-vrm** — `vendor/three-vrm.module.min.js` (3D-персонаж,
`3d.html`); его заголовок с лицензией — в самом файле.

## Код, переработанный в orb.html

**shipnotes-components** (https://github.com/aqualang89/shipnotes-components),
`components/voice-orb/voice-orb.js`, `components/signal-orb/signal-orb.js`,
`components/iris/iris.js`. Из них в шаре: точка с ядром и ореолом
(фрагментный шейдер), волны речи наружу и слушания внутрь, три пояса
думающего шара с бегущими головами, искры, сдвиг сетки Фибоначчи против
муара (`seedsFor`), размер точки по их числу, облегчение по медленным
кадрам, дешёвое свечение (размытие в четверть экрана, тон-маппинг
1 − e^(−1.45c)).

    MIT License
    Copyright (c) 2026 Ship Notes

## Идеи, без кода

Ничего не скопировано — взят приём, код свой. Лицензии этого не требуют,
но откуда идея — честнее записать.

- **dotmorph** (https://github.com/shniii/dotmorph, MIT, © 2026 Shnyar) —
  перетекание фигур через закрученное облако, у каждой точки своя задержка.
- **Holocore UI** (https://github.com/AntonioMolinaSantos/holocore-ui, MIT,
  © 2026 Antonio Molina) — смесь состояний весами с экспоненциальным
  подходом, дальняя сторона шара приглушена («линза»).
- **VoiceOrbs** (https://github.com/amunozdev/voiceorbs, MIT,
  © 2026 Alexis Munoz) — электроны-кометы атома, лог-спираль галактики.
- **jarvis** (https://github.com/hnturk25/jarvis, MIT) — хореография
  приветствия: сжаться и дрогнуть, разлететься, собраться.
- **The-Spirit** (https://github.com/edankwan/The-Spirit, MIT,
  © 2015 Edan Kwan) — точки, которых течение уносит с поверхности и которые
  возвращаются (у нас вместо curl-шума — поток ABC, он дешевле).
- Поток Арнольда — Бельтрами — Чилдресса (ABC) и сфера Фибоначчи — общая
  математика.

## Текст разрешения MIT

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
