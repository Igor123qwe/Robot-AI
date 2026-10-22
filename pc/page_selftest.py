#!/usr/bin/env python3
"""Поведение страницы аватара (pc/avatar/index.html) — на заглушках PIXI и
Live2D, но в НАСТОЯЩЕМ Chromium.

Зачем отдельно от pc/selftest.py: там всё без браузера, а здесь нужен
Playwright и Chromium — то же, чем ПК снимает аватар для робота. Зато это
единственный способ проверить саму страницу: что эмоция робота доходит до
параметров модели, что рот открывается на речи, что тело качается под
музыку, — не глядя на экран и не имея модели. Библиотеки с CDN подменяются
заглушками, поэтому интернет не нужен.

Уже пойманное: сдвиги параметров копились кадр за кадром — форма рта за
секунду уезжала в 22 при пределе 1. Глазами такое видно как «модель
скривилась», а почему — нет.

    python pc/page_selftest.py

Браузер: системный Edge/Chrome/Chromium Playwright, либо путь в KUZYA_BROWSER.
"""
import json, sys, threading, time, urllib.request
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http.server import ThreadingHTTPServer
import kuzya_pc as k
from playwright.sync_api import sync_playwright

srv = ThreadingHTTPServer(("127.0.0.1", 0), k.Handler)
class FakeOllama:
    def models(self): return []
    def alive(self): return False
srv.cfg = k.Config("тест", whisper=k.Whisper("tiny"), ollama=FakeOllama())
srv.avatar = k.Аватар()
# Сценки (face/scenes.py) — случайные и складываются с эмоциями; для точных
# проверок ниже их выключаем, а для проверки самих сценок включаем обратно.
class _БезСценок:
    def кадр(self, *a, **kw): return {"имя": "", "текст": "", "параметры": {}}
настоящий_сценарист = srv.avatar.сценарист
srv.avatar.сценарист = _БезСценок()
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}"

def post(state):
    r = urllib.request.Request(url + "/avatar/state", data=json.dumps(state).encode(), method="POST")
    urllib.request.urlopen(r, timeout=5).read()

PIXI_STUB = """
window.__texts = [];
window.PIXI = {
  UPDATE_PRIORITY: {LOW: -25},
  Container: class { constructor(){ this.children=[]; this.position={x:0,y:0,set(x,y){this.x=x;this.y=y;}}; this.visible=true; }
    addChild(c){ this.children.push(c); } },
  Graphics: class { clear(){return this;} beginFill(){return this;} drawRoundedRect(){return this;} endFill(){return this;} },
  Text: class { constructor(t, st){ this.text=t; this.style=st; this.width=100; this.height=30;
    this.anchor={set(){}}; this.position={set(){}}; window.__texts.push(this); } },
  Application: class { constructor(o){ this.renderer={width:1280,height:800};
    this.view=document.createElement('canvas'); this.stage={addChild(){}};
    this.ticker={add(fn){ window.__tick=fn; }}; } },
  live2d: {}
};
"""
CUBISM_STUB = """
PIXI.live2d = { Live2DModel: { from: async (path) => {
  const params = {}; const listeners = {};
  const model = { width: 500, height: 900, scale:{set(){}}, anchor:{set(){}},
    position:{set(){}, y:0}, rotation:0,
    internalModel: { on(ev, fn){ listeners[ev]=fn; },
      motionManager: { definitions: { Idle: [{}], TapBody: [{}], Flick: [{}], Shake: [{}] } },
      settings: { expressions: [{Name: "normal"}, {Name: "smile"}, {Name: "sad"}] },
      coreModel: {
      getParameterValueById(id){ return params[id]||0; },
      setParameterValueById(id,v){ params[id]=v; } } },
    expression(n){ model.__expr=n; }, motion(g){ model.__motion=g; } };
  window.__model = model; window.__params = params; window.__listeners = listeners;
  return model; } } };
"""

итог = []
def check(что, есть, надо):
    итог.append((что, есть == надо, есть, надо))


def _пиксели_png(данные: bytes):
    """(ширина, высота, каналов, строки) из PNG — без чужих библиотек.

    Нужно ровно для одного: посмотреть, что в снимке 3D-страницы и правда
    есть модель. Pillow ради этого в зависимости не тянем — снимок маленький,
    а распаковка PNG укладывается в тридцать строк.
    """
    import struct
    import zlib
    поз, ш, в, цвет, idat = 8, 0, 0, 6, b""
    while поз < len(данные):
        длина = struct.unpack(">I", данные[поз:поз + 4])[0]
        тип, тело = данные[поз + 4:поз + 8], данные[поз + 8:поз + 8 + длина]
        if тип == b"IHDR":
            ш, в, _, цвет = struct.unpack(">IIBB", тело[:10])
        elif тип == b"IDAT":
            idat += тело
        поз += 12 + длина
    сырое = zlib.decompress(idat)
    каналов = {0: 1, 2: 3, 4: 2, 6: 4}[цвет]
    шаг = ш * каналов
    строки, прошлая, i = [], bytearray(шаг), 0
    for _ in range(в):
        фильтр = сырое[i]
        строка = bytearray(сырое[i + 1:i + 1 + шаг])
        i += 1 + шаг
        for x in range(шаг):
            a = строка[x - каналов] if x >= каналов else 0
            b_ = прошлая[x]
            c = прошлая[x - каналов] if x >= каналов else 0
            if фильтр == 1:
                строка[x] = (строка[x] + a) & 255
            elif фильтр == 2:
                строка[x] = (строка[x] + b_) & 255
            elif фильтр == 3:
                строка[x] = (строка[x] + (a + b_) // 2) & 255
            elif фильтр == 4:
                п = a + b_ - c
                па, пб, пc = abs(п - a), abs(п - b_), abs(п - c)
                строка[x] = (строка[x] + (a if па <= пб and па <= пc
                                          else (b_ if пб <= пc else c))) & 255
        строки.append(bytes(строка))
        прошлая = строка
    return ш, в, каналов, строки

with sync_playwright() as p:
    свой = os.environ.get("KUZYA_BROWSER", "").strip()
    b = None
    # Те же ключи, что у Съёмки на ПК: без них браузер без видеокарты прячет
    # WebGL, и шар (как и 3D) молча не рисуется.
    ключи = ["--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]
    if свой:
        b = p.chromium.launch(executable_path=свой, headless=True, args=ключи)
    else:
        for канал in ("msedge", "chrome", None):
            try:
                b = p.chromium.launch(channel=канал, headless=True, args=ключи)
                break
            except Exception:                   # noqa: BLE001
                continue
    if b is None:
        print("браузер не открылся: playwright install chromium — или путь в KUZYA_BROWSER")
        srv.shutdown()
        sys.exit(2)
    page = b.new_page(viewport={"width": 1280, "height": 800})
    page.route("**/live2dcubismcore.min.js", lambda r: r.fulfill(body="", content_type="text/javascript"))
    page.route("**/pixi.min.js", lambda r: r.fulfill(body=PIXI_STUB, content_type="text/javascript"))
    page.route("**/cubism4.min.js", lambda r: r.fulfill(body=CUBISM_STUB, content_type="text/javascript"))
    # config.json — настоящий, плюс параметр правой руки: у модели-заглушки
    # он «есть» (заглушка принимает любой id), и по нему видно, что руки
    # сценок доезжают до параметра из config.json.
    настройки = json.loads((Path(__file__).resolve().parent / "avatar" / "config.json").read_text(encoding="utf-8"))
    настройки["параметры"]["рука_п"] = "ParamArmRA"
    настройки["параметры"]["рука_размах"] = 10
    # Основной прогон — на модели нового формата (Cubism 4): по имени файла
    # страница выбирает, какие библиотеки грузить, и заглушки ниже — именно
    # для них. Старый формат (Shizuku, .model.json) проверяется отдельно.
    настройки["модель"] = "samples/mao/Mao.model3.json"
    настройки["жесты_в_покое"] = 0            # жесты в покое — отдельной страницей ниже
    page.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки, ensure_ascii=False),
                                                     content_type="application/json"))
    ошибки = []
    page.on("pageerror", lambda e: ошибки.append(str(e)))
    page.on("console", lambda m: ошибки.append(m.text) if m.type == "error" else None)
    запросы = []
    page.on("request", lambda r: запросы.append(r.url))
    page.on("requestfailed", lambda r: запросы.append("FAILED " + r.url + " " + str(r.failure)))
    page.goto(url + "/avatar/", wait_until="load")
    try:
        page.wait_for_function("!!(window.__listeners && window.__listeners.afterMotionUpdate)", timeout=10000)
    except Exception as e:
        print("не дождался:", str(e).splitlines()[0])
        print("беда:", page.evaluate("document.getElementById('беда').textContent"))
        print("ошибки:", ошибки)
        print("PIXI:", page.evaluate("typeof window.PIXI"), "live2d:", page.evaluate("typeof (window.PIXI||{}).live2d"))
        print("model:", page.evaluate("typeof window.__model"), "listeners:", page.evaluate("JSON.stringify(Object.keys(window.__listeners||{}))"))
        print("запросы:", *запросы[:12], sep="\n  ")
        b.close(); srv.shutdown(); sys.exit(2)

    def кадры(n=25, шаг=30):
        for _ in range(n):
            page.evaluate("window.__listeners.afterMotionUpdate()")
            page.wait_for_timeout(шаг)

    def парам(id):
        return page.evaluate(f"window.__params[{json.dumps(id)}] || 0")

    # 1. Радость: рот-улыбка через стандартный параметр, без настройки выражений.
    post({"эмоция": "рад", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("рад → ParamMouthForm ≈ 1", round(парам("ParamMouthForm"), 1), 1.0)
    check("рад → улыбка глаз", парам("ParamEyeLSmile") > 0.9, True)
    check("беда пуста (страница не жалуется)", page.evaluate("document.getElementById('беда').textContent"), "")

    # 2. Огорчён: брови вниз, а улыбка глаз ушла в ноль.
    post({"эмоция": "огорчён", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("огорчён → брови вниз", парам("ParamBrowLY") < -0.6, True)
    check("огорчён → улыбка глаз снята", парам("ParamEyeLSmile") < 0.05, True)

    # 3. Говорит: рот из позы (синус Питомца) идёт в ParamMouthOpenY, кивки.
    post({"эмоция": "спокоен", "говорит": "привет", "музыка": {"играет": False}})
    page.wait_for_timeout(300)
    открытия = []
    for _ in range(20):
        page.evaluate("window.__listeners.afterMotionUpdate()")
        открытия.append(парам("ParamMouthOpenY"))
        page.wait_for_timeout(50)
        post({"эмоция": "спокоен", "говорит": "привет", "музыка": {"играет": False}})
    check("говорит → рот открывается (максимум > 0.5)", max(открытия) > 0.5, True)
    check("говорит → рот и закрывается (минимум < 0.3)", min(открытия) < 0.3, True)

    # 4. Музыка → танец: покачивание всего тела и наклон корпуса.
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": True, "название": "x"}})
    page.wait_for_timeout(300)
    повороты, наклоны = [], []
    for _ in range(20):
        page.evaluate("window.__listeners.afterMotionUpdate()")
        повороты.append(page.evaluate("window.__model.rotation"))
        наклоны.append(парам("ParamBodyAngleX"))
        page.wait_for_timeout(50)
    check("танец → тело качается (поворот ≠ 0)", max(abs(x) for x in повороты) > 0.02, True)
    check("танец → корпус наклоняется (ParamBodyAngleX)", max(abs(x) for x in наклоны) > 0.5, True)
    check("метка танца дошла", page.evaluate("window.__model.__motion || ''"), "")  # танец в config пуст — motion не зовётся
    check("карточка музыки слева — персонаж отошёл вправо (как домовёнок на роботе)",
          page.evaluate("window.__model.position.x") > 1280 * 0.58, True)

    # 5. Сон: глаза закрыты, рот закрыт.
    post({"эмоция": "сплю"})
    page.wait_for_timeout(300); кадры(45)      # шаг обратно к середине плавный — даём ему секунду с лишним
    check("сплю → глаза закрыты", (парам("ParamEyeLOpen"), парам("ParamEyeROpen")), (0, 0))
    check("сплю → рот закрыт", парам("ParamMouthOpenY"), 0)
    check("карточки нет — персонаж вернулся к середине",
          abs(page.evaluate("window.__model.position.x") - 640) < 40, True)

    # 6а. Сценка: человек появился — событие, у всех его сценок есть движение
    # и почти у всех реплика; проверяем, что сценка доехала до модели.
    srv.avatar.сценарист = настоящий_сценарист
    post({"эмоция": "спокоен", "человек": False, "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры(3)
    post({"эмоция": "спокоен", "человек": True, "взгляд": 0.0, "музыка": {"играет": False}})
    page.wait_for_timeout(300)
    сдвиги = set(); пузыри = set()
    for _ in range(15):
        page.evaluate("window.__listeners.afterMotionUpdate()")
        сдвиги.add(round(парам("ParamAngleY") + парам("ParamAngleZ") + парам("ParamBodyAngleZ"), 1))
        пузыри.add(page.evaluate("(window.__texts[0] || {}).text || ''"))
        page.wait_for_timeout(50)
        post({"эмоция": "спокоен", "человек": True, "взгляд": 0.0, "музыка": {"играет": False}})
    check("человек появился → сценка двигает голову/тело", len(сдвиги) > 3, True)
    check("…а пузырь над головой есть в PIXI (текст или пусто — смотря какая сценка выпала)",
          page.evaluate("window.__texts.length") >= 1, True)
    srv.avatar.сценарист = _БезСценок()

    # 6. Человек слева от робота → голова и глаза к нему, то есть ВПРАВО по
    # экрану: экран смотрит вперёд, и левый бок робота у того, кто стоит
    # перед ним, — по правую руку (orb_rules.ХОД, экран_знак). У Cubism
    # «вправо по экрану» — ПЛЮС ParamAngleX (так же ставит взгляд focus()
    # самой библиотеки). Раньше здесь ждали минус — «слева от робота» путали
    # со «слева на экране», и проверка закрепляла взгляд от человека.
    post({"эмоция": "спокоен", "человек": True, "взгляд": 0.5, "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("человек слева от робота → голова к нему, вправо по экрану (ParamAngleX > 10)",
          парам("ParamAngleX") > 10, True)
    check("…и глаза туда же, а не в другую сторону", парам("ParamEyeBallX") > 0.3, True)
    # И зеркально: человек справа от робота (пеленг отрицательный) — влево.
    post({"эмоция": "спокоен", "человек": True, "взгляд": -0.5, "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("человек справа от робота → голова и глаза влево по экрану",
          (парам("ParamAngleX") < -10, парам("ParamEyeBallX") < -0.3), (True, True))


    # 7. Сценка открывает рот («зевнул», «свистит», «смеётся»). Раньше рот
    # сценки складывался как сдвиг, а потом затирался абсолютным ртом позы —
    # и на Live2D ни одна из этих сценок рта не открывала.
    class _Зевок:
        def кадр(self, *a, **kw):
            return {"имя": "зевнул", "текст": "", "параметры": {"ParamMouthOpenY": 1.0}}
    srv.avatar.сценарист = _Зевок()
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("сценка «зевнул» открывает рот на модели (ParamMouthOpenY ≈ 1)",
          round(парам("ParamMouthOpenY"), 1), 1.0)
    check("…и не копится сверх единицы", парам("ParamMouthOpenY") <= 1.0, True)

    # 8. Сдвиг сценки по параметру, которого нет ни в одной эмоции
    # (ParamBodyAngleZ), после сценки уходит в ноль, а не остаётся запечённым
    # до следующей сценки, которая тронет тот же параметр.
    class _Наклон:
        def кадр(self, *a, **kw):
            return {"имя": "потянулся", "текст": "", "параметры": {"ParamBodyAngleZ": 15.0}}
    srv.avatar.сценарист = _Наклон()
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("сценка наклоняет корпус (ParamBodyAngleZ > 10)", парам("ParamBodyAngleZ") > 10, True)
    srv.avatar.сценарист = _БезСценок()
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры(40)
    check("сценка кончилась — наклон корпуса ушёл в ноль, а не остался запечённым",
          abs(парам("ParamBodyAngleZ")) < 1.0, True)

    # 11. Живая беда: сценка прислала мусор вместо числа (битый config.json,
    # опечатка в сценке). Раньше это давало NaN, а NaN через сглаживание
    # (NaN*k+NaN=NaN) тянется вечно на ВСЕХ параметрах разом — и с виду это
    # неотличимо от «модель просто дышит и ничего не делает»: рот не
    # открывается, жесты не играют, а на экране никакой ошибки не видно,
    # потому что она никогда не проходит через показать(). Проверяем, что
    # мусор не портит параметр (остаётся числом) и не мешает соседям.
    class _Порча:
        def кадр(self, *a, **kw):
            return {"имя": "порченая", "текст": "", "параметры": {"ParamAngleZ": "мусор"}}
    srv.avatar.сценарист = _Порча()
    post({"эмоция": "спокоен", "говорит": "Привет, как дела", "музыка": {"играет": False}})
    page.wait_for_timeout(300)
    for _ in range(15):
        page.evaluate("window.__listeners.afterMotionUpdate()")
        page.wait_for_timeout(30)
    check("мусорная строка вместо числа не превращается в NaN",
          page.evaluate("Number.isFinite(window.__params['ParamAngleZ'])"), True)
    check("…и соседний параметр (рот, речь) при этом продолжает работать",
          парам("ParamMouthOpenY") >= 0.0, True)
    check("ошибки страницы (console.error/pageerror) при этом нет",
          not ошибки, True)

    # Убрали порчу — оверлей не остался в залипшем состоянии навсегда.
    srv.avatar.сценарист = _БезСценок()
    post({"эмоция": "рад", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("после порченой сценки обычная эмоция снова красит модель",
          парам("ParamMouthForm") > 0.5, True)

    # Та же порча, но именно в рту сценки: рот считается отдельной веткой
    # (абсолютно, без сглаживания), и её защита от мусора отдельная от
    # общего цикла выше — обе должны быть на месте, а не только одна.
    class _ПорчаРта:
        def кадр(self, *a, **kw):
            return {"имя": "порченая", "текст": "", "параметры": {"ParamMouthOpenY": "мусор"}}
    srv.avatar.сценарист = _ПорчаРта()
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("мусор в ParamMouthOpenY сценки не превращает рот в NaN",
          page.evaluate("Number.isFinite(window.__params['ParamMouthOpenY'])"), True)
    srv.avatar.сценарист = _БезСценок()

    # Провода диагностики на ПК: без них поломка в кадре не видна нигде.
    исходник_pc = (Path(__file__).resolve().parent / "kuzya_pc.py").read_text(encoding="utf-8")
    check("kuzya_pc.py слушает pageerror и console.error страницы аватара",
          ('страница.on("pageerror"' in исходник_pc, 'страница.on("console"' in исходник_pc),
          (True, True))

    # 10. Готовые поведения модели: группы motion и выражения подобраны по
    # именам, движение играется раз на старте сценки с таким поводом,
    # выражение — по эмоции, без единой строчки в config.json.
    готовые = page.evaluate("window.__готовые")
    check("группы движений модели найдены и разложены по поводам (Tap → встреча, Flick → движение, Shake → тревога)",
          (готовые["движения"]["человек_пришёл"], готовые["движения"]["движение"], готовые["движения"]["тревога"]),
          ("TapBody", "Flick", "Shake"))
    check("выражения модели разложены по эмоциям (smile → рад, sad → огорчён)",
          (готовые["выраженияПоЭмоции"]["рад"], готовые["выраженияПоЭмоции"]["огорчён"]), ("smile", "sad"))
    class _Встреча:
        def кадр(self, *a, **kw):
            return {"имя": "помахал рукой", "когда": "человек_пришёл", "текст": "Привет!", "параметры": {}}
    srv.avatar.сценарист = _Встреча()
    page.evaluate("window.__model.__motion = ''; window.__сыграно = []")
    for _ in range(4):
        post({"эмоция": "рад", "говорит": "", "музыка": {"играет": False}})
        page.wait_for_timeout(150)
    check("сценка встречи → у модели сыграно её движение TapBody, один раз, а не на каждый опрос",
          (page.evaluate("window.__model.__motion"), page.evaluate("window.__сыграно.length")), ("TapBody", 1))
    check("эмоция «рад» → выражение smile самой модели", page.evaluate("window.__model.__expr"), "smile")
    srv.avatar.сценарист = _БезСценок()

    # 9. Руки сценки («помахал рукой»: рука_п 60 = до упора) — в параметр
    # руки из config.json, в его размахе; левой в config нет — ничего не
    # ставится и страница не падает.
    class _Машет:
        def кадр(self, *a, **kw):
            return {"имя": "помахал рукой", "текст": "Привет!", "параметры": {"рука_п": 60.0, "рука_л": 60.0}}
    srv.avatar.сценарист = _Машет()
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры(40)
    check("рука сценки → параметр руки из config.json, в его размахе (≈10)",
          round(парам("ParamArmRA")), 10)
    check("левой руки в config нет — её параметр не выдуман",
          page.evaluate("Object.keys(window.__params).filter(k => k === 'рука_л' || k === 'undefined').length"), 0)
    srv.avatar.сценарист = _БезСценок()
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры(40)
    check("сценка кончилась — рука опущена", abs(парам("ParamArmRA")) < 1.0, True)

    # 12. Старый формат (Cubism 2, Shizuku из samples/): по имени файла
    # страница грузит другое ядро и другой мост, параметры зовёт старыми
    # именами (ParamMouthOpenY → PARAM_MOUTH_OPEN_Y) через setParamFloat,
    # корпус — и стандартным PARAM_BODY_ANGLE_X, и PARAM_BODY_X, как у
    # Shizuku. Заглушка ядра — только старый API: setParameterValueById у
    # неё нет, и если страница позовёт его — параметр не встанет.
    CUBISM2_STUB = """
    PIXI.live2d = { Live2DModel: { from: async (path) => {
      const params = {}; const listeners = {};
      const model = { width: 500, height: 900, scale:{set(){}}, anchor:{set(){}},
        position:{set(){}, y:0}, rotation:0,
        internalModel: { on(ev, fn){ listeners[ev]=fn; },
          motionManager: { definitions: { idle: [{}], tap_body: [{}], flick_head: [{}], shake: [{}] } },
          settings: { expressions: [{name: "f01", file: "exp/f01.exp.json"}] },
          coreModel: {
          getParamFloat(id){ return params[id]||0; },
          setParamFloat(id,v){ params[id]=v; } } },
        expression(n){ model.__expr=n; }, motion(g){ model.__motion=g; model.__motions=(model.__motions||0)+1; } };
      window.__model = model; window.__params = params; window.__listeners = listeners;
      return model; } } };
    """
    page2 = b.new_page(viewport={"width": 1280, "height": 800})
    запросы2 = []
    page2.on("request", lambda r: запросы2.append(r.url))
    page2.route("**/live2d.min.js", lambda r: r.fulfill(body="", content_type="text/javascript"))
    page2.route("**/pixi.min.js", lambda r: r.fulfill(body=PIXI_STUB, content_type="text/javascript"))
    page2.route("**/cubism2.min.js", lambda r: r.fulfill(body=CUBISM2_STUB, content_type="text/javascript"))
    настройки2 = dict(настройки)
    настройки2["модель"] = "samples/shizuku/shizuku.model.json"
    настройки2["жесты_в_покое"] = [0.2, 0.3]
    page2.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки2, ensure_ascii=False),
                                                      content_type="application/json"))
    ошибки2 = []
    page2.on("pageerror", lambda e: ошибки2.append(str(e)))
    page2.on("console", lambda m: ошибки2.append(m.text) if m.type == "error" else None)
    page2.goto(url + "/avatar/", wait_until="load")
    page2.wait_for_function("!!(window.__listeners && window.__listeners.afterMotionUpdate)", timeout=10000)
    check("старый формат: по имени .model.json страница взяла ядро Cubism 2, а не Cubism 4",
          (any("live2d.min.js" in з for з in запросы2), any("cubism2.min.js" in з for з in запросы2),
           any("cubismcore" in з or "cubism4" in з for з in запросы2)), (True, True, False))

    def парам2(id):
        return page2.evaluate(f"window.__params[{json.dumps(id)}] || 0")
    def кадры2(n=25, шаг=30):
        for _ in range(n):
            page2.evaluate("window.__listeners.afterMotionUpdate()")
            page2.wait_for_timeout(шаг)
    post({"эмоция": "рад", "говорит": "привет", "музыка": {"играет": True, "название": "x"}})
    page2.wait_for_timeout(300)
    рты = []
    for _ in range(20):
        page2.evaluate("window.__listeners.afterMotionUpdate()")
        рты.append(парам2("PARAM_MOUTH_OPEN_Y"))
        page2.wait_for_timeout(50)
        post({"эмоция": "рад", "говорит": "привет", "музыка": {"играет": True, "название": "x"}})
    check("старый формат: рот речи → PARAM_MOUTH_OPEN_Y (старое имя, setParamFloat)", max(рты) > 0.5, True)
    check("старый формат: улыбка → PARAM_MOUTH_FORM ≈ 1", round(парам2("PARAM_MOUTH_FORM"), 1), 1.0)
    # Без «говорит»: та ветка перебивает танец (см. «тело: говорит перебивает
    # взгляд и танец» в scripts/mutations.py) — с ним корпус качаться не будет.
    post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": True, "название": "x"}})
    page2.wait_for_timeout(300)
    # Размах — по всем кадрам, а не по последнему: качание синусом, и один
    # снимок на загруженной машине попадал в его ноль.
    размах = 0.0
    for _ in range(15):
        page2.evaluate("window.__listeners.afterMotionUpdate()")
        размах = max(размах, abs(парам2("PARAM_BODY_ANGLE_X")))
        page2.wait_for_timeout(50)
        post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": True, "название": "x"}})
    body_angle_x = парам2("PARAM_BODY_ANGLE_X")
    check("старый формат: корпус качается в танце (PARAM_BODY_ANGLE_X)", размах > 0.5, True)
    check("старый формат: псевдоним PARAM_BODY_X держит то же значение (Shizuku зовёт корпус так)",
          парам2("PARAM_BODY_X"), body_angle_x)
    check("старый формат: новых имён (ParamMouthOpenY) в ядро не ушло",
          page2.evaluate("Object.keys(window.__params).filter(k => /^Param[A-Z]/.test(k)).length"), 0)
    check("старый формат: группы движений разложены по поводам (tap_body → встреча, flick_head, shake)",
          page2.evaluate("[window.__готовые.движения.человек_пришёл, window.__готовые.движения.движение, "
                         "window.__готовые.движения.тревога]"), ["tap_body", "flick_head", "shake"])

    # 13. Жесты в покое: раз в срок (тут 0.2–0.3 с) — своё движение «тап»,
    # но не поверх речи, сна, танца или сценки.
    page2.evaluate("window.__жестов_покоя = 0; window.__model.__motion = ''")
    for _ in range(12):
        post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}})
        page2.wait_for_timeout(100)
    check("жесты в покое: за секунду покоя сыграно хотя бы одно движение «тап»",
          (page2.evaluate("window.__жестов_покоя") >= 1, page2.evaluate("window.__model.__motion")),
          (True, "tap_body"))
    # Переключаем на «говорит» и даём странице время реально это узнать
    # (её собственный опрос идёт раз в 100 мс, независимо от post() ниже) —
    # иначе счётчик сбросился бы раньше, чем страница увидела перемену, и
    # жест из уже устаревшего «в покое» состояния засчитался бы как «во
    # время речи», хотя речь для страницы ещё не наступила.
    post({"эмоция": "спокоен", "говорит": "Привет, как дела", "музыка": {"играет": False}})
    page2.wait_for_timeout(250)
    page2.evaluate("window.__жестов_покоя = 0")
    for _ in range(15):
        post({"эмоция": "спокоен", "говорит": "Привет, как дела", "музыка": {"играет": False}})
        page2.wait_for_timeout(100)
    check("жесты в покое: во время речи — ни одного", page2.evaluate("window.__жестов_покоя"), 0)
    check("старый формат: ошибок страницы нет", not ошибки2, True)
    page2.close()

    # 14. Mao (официальный образец, Cubism 4): параметра ParamMouthOpenY у
    # модели нет, рот открывает ParamA — так записано в её группе LipSync.
    # Страница должна взять его сама, без правки config.json.
    CUBISM_MAO_STUB = CUBISM_STUB.replace(
        "settings: { expressions: [{Name: \"normal\"}, {Name: \"smile\"}, {Name: \"sad\"}] },",
        "settings: { expressions: [{Name: \"exp_01\"}], groups: [{Target: \"Parameter\", Name: \"LipSync\", Ids: [\"ParamA\"]}] },")
    CUBISM_MAO_STUB = CUBISM_MAO_STUB.replace(
        "coreModel: {",
        "coreModel: { _model: { parameters: { ids: [\"ParamA\", \"ParamAngleX\", \"ParamEyeLSmile\"] } },")
    page3 = b.new_page(viewport={"width": 1280, "height": 800})
    page3.route("**/live2dcubismcore.min.js", lambda r: r.fulfill(body="", content_type="text/javascript"))
    page3.route("**/pixi.min.js", lambda r: r.fulfill(body=PIXI_STUB, content_type="text/javascript"))
    page3.route("**/cubism4.min.js", lambda r: r.fulfill(body=CUBISM_MAO_STUB, content_type="text/javascript"))
    page3.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки, ensure_ascii=False),
                                                      content_type="application/json"))
    ошибки3 = []
    page3.on("pageerror", lambda e: ошибки3.append(str(e)))
    page3.goto(url + "/avatar/", wait_until="load")
    page3.wait_for_function("!!(window.__listeners && window.__listeners.afterMotionUpdate)", timeout=10000)
    check("Mao: рот взят из группы LipSync модели (ParamA), а не ParamMouthOpenY из config",
          page3.evaluate("window.__ядро.рот"), "ParamA")
    рты3 = []
    for _ in range(20):
        post({"эмоция": "спокоен", "говорит": "привет", "музыка": {"играет": False}})
        page3.wait_for_timeout(50)
        page3.evaluate("window.__listeners.afterMotionUpdate()")
        рты3.append(page3.evaluate("window.__params['ParamA'] || 0"))
    check("Mao: речь открывает ParamA", max(рты3) > 0.5, True)
    check("Mao: ошибок страницы нет", not ошибки3, True)
    page3.close()

    # 15. Модель, у которой группы движений названы НЕ ТАК. Библиотека заводит
    # покой сама, но только из группы с точным именем «Idle»; у готовых
    # моделей она сплошь и рядом другая, а у части официальных образцов вовсе
    # без имени. Тогда покоя нет вообще — модель стоит столбом, и на экране
    # это неотличимо от «наш код ничего не играет». Живая жалоба: «анимация
    # около нуля» при полностью рабочем конвейере.
    ЧУЖИЕ_ГРУППЫ = CUBISM_STUB.replace(
        "motionManager: { definitions: { Idle: [{}], TapBody: [{}], Flick: [{}], Shake: [{}] } },",
        "motionManager: { groups: { idle: 'Idle' }, definitions: { '': [{}], 'mark_m01': [{}] } },")
    page5 = b.new_page(viewport={"width": 1280, "height": 800})
    page5.route("**/live2dcubismcore.min.js", lambda r: r.fulfill(body="", content_type="text/javascript"))
    page5.route("**/pixi.min.js", lambda r: r.fulfill(body=PIXI_STUB, content_type="text/javascript"))
    page5.route("**/cubism4.min.js", lambda r: r.fulfill(body=ЧУЖИЕ_ГРУППЫ, content_type="text/javascript"))
    page5.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки, ensure_ascii=False),
                                                      content_type="application/json"))
    жалобы5 = []
    page5.on("console", lambda m: жалобы5.append((m.type, m.text)))
    page5.goto(url + "/avatar/", wait_until="load")
    page5.wait_for_function("!!(window.__listeners && window.__listeners.afterMotionUpdate)", timeout=10000)
    check("группы названы не так — покой всё равно заведён (из той, что есть)",
          page5.evaluate("window.__model.internalModel.motionManager.groups.idle"), "")
    check("…и жест «тап» тоже нашёлся: любое авторское движение лучше столба",
          page5.evaluate("window.__готовые.движения.человек_пришёл"), "mark_m01")
    check("…и об этом сказано предупреждением — иначе это молча и невидимо",
          any(т == "warning" and "нет группы покоя" in текст for т, текст in жалобы5), True)
    check("kuzya_pc пересылает в журнал и предупреждения страницы, не только ошибки",
          'm.type in ("error", "warning")' in исходник_pc, True)
    page5.close()

    # 16. У модели вовсе нет движений — это не «наш код молчит», а «модель
    # такая»; сказать об этом надо ошибкой, её видно в журнале ПК.
    БЕЗ_ДВИЖЕНИЙ = CUBISM_STUB.replace(
        "motionManager: { definitions: { Idle: [{}], TapBody: [{}], Flick: [{}], Shake: [{}] } },",
        "motionManager: { groups: { idle: 'Idle' }, definitions: {} },")
    page6 = b.new_page(viewport={"width": 1280, "height": 800})
    page6.route("**/live2dcubismcore.min.js", lambda r: r.fulfill(body="", content_type="text/javascript"))
    page6.route("**/pixi.min.js", lambda r: r.fulfill(body=PIXI_STUB, content_type="text/javascript"))
    page6.route("**/cubism4.min.js", lambda r: r.fulfill(body=БЕЗ_ДВИЖЕНИЙ, content_type="text/javascript"))
    page6.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки, ensure_ascii=False),
                                                      content_type="application/json"))
    жалобы6 = []
    page6.on("console", lambda m: жалобы6.append((m.type, m.text)))
    page6.goto(url + "/avatar/", wait_until="load")
    page6.wait_for_function("!!(window.__listeners && window.__listeners.afterMotionUpdate)", timeout=10000)
    check("нет ни одной группы движений — сказано ошибкой, с советом взять другую модель",
          any(т == "error" and "нет ни одной группы" in текст and "shizuku" in текст
              for т, текст in жалобы6), True)
    page6.close()

    # 17. ТРЁХМЕРНЫЙ ПЕРСОНАЖ (3d.html). Здесь заглушек нет вовсе: настоящие
    # библиотеки из vendor/, настоящая модель VRM (собирается pc/vrm_проба.py:
    # скелет из обязательных костей, светящийся кубик вместо тела, выражения
    # со стандартными именами) и настоящий WebGL. Проверять 3D заглушками
    # бессмысленно: весь смысл в том, доезжает ли картинка до кадра, который
    # ПК отдаёт роботу, — а это видно только по пикселям снимка.
    import vrm_проба
    модель3d = Path(__file__).resolve().parent / "avatar" / "model3d" / "проба.vrm"
    модель3d.parent.mkdir(parents=True, exist_ok=True)
    модель3d.write_bytes(vrm_проба.собрать())
    настройки3d = dict(настройки)
    настройки3d["движок"] = "3d"
    настройки3d["модель3d"] = "model3d/проба.vrm"
    page7 = b.new_page(viewport={"width": 320, "height": 240})
    ошибки7 = []
    page7.on("pageerror", lambda e: ошибки7.append("JS: " + str(e)))
    page7.on("console", lambda m: ошибки7.append(m.text) if m.type == "error" else None)
    page7.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки3d, ensure_ascii=False),
                                                      content_type="application/json"))
    page7.goto(url + "/avatar/3d.html", wait_until="load")
    try:
        page7.wait_for_function("window.__готовые3d !== undefined", timeout=20000)
        готовые3d = page7.evaluate("window.__готовые3d")
    except Exception:                                # noqa: BLE001
        готовые3d = {"беда": page7.evaluate("document.getElementById('беда').textContent")[:200]}
    check("3D: настоящая модель VRM загрузилась, кости и выражения найдены",
          (sorted(готовые3d.get("кости", []))[:2],
           "happy" in готовые3d.get("выражения", []),
           "aa" in готовые3d.get("выражения", [])),
          (["head", "leftUpperArm"], True, True))
    page7.wait_for_timeout(1200)
    check("3D: кадры считаются — страница живая, а не «скрипт загрузился»",
          page7.evaluate("window.__кадров3d") > 3, True)
    # Пиксели СНИМКА, а не холста: холст WebGL без preserveDrawingBuffer
    # читается пустым, и проверка по нему проходила бы даже при чёрном
    # экране. ПК снимает страницу ровно так же, как здесь.
    ш, в, к, строки = _пиксели_png(page7.screenshot())
    свои = sum(1 for стр in строки for x in range(0, ш * к, к)
               if стр[x + 1] > 60 and стр[x + 1] > стр[x] + 15 and стр[x + 1] > стр[x + 2] + 15)
    check("3D: модель ВИДНА в снятом кадре — том самом, что уедет роботу",
          свои > 200, True)
    # Куда 3D-персонаж смотрит, когда человек в кадре. Пеленг положителен,
    # когда человек СЛЕВА, а мир three.js растёт вправо по экрану: и голова,
    # и глаза обязаны уйти в минус — и обязательно в ОДНУ сторону. Раньше
    # знак у головы был противоположен знаку глаз, а к человеку голова не
    # поворачивалась вовсе: глаза косили на собеседника, лицо смотрело мимо.
    # Позу считает НАСТОЯЩИЙ face/character.py, как на живом ПК: проверяется
    # вся цепочка «пеленг → поза → кости», а не переписанная формула.
    import character as ch                 # face/ в пути кладёт kuzya_pc
    питомец7 = ch.Питомец(1280, 720, тело_ширина=0.36, шаг_длина=0.12)

    # Маршрут ставится ОДИН раз и отдаёт изменяемый словарь: у обработчика с
    # двумя параметрами Playwright занимает второй под Request, и значение
    # по умолчанию в него не передать.
    состояние7 = {"эмоция": "спокоен", "человек": True, "взгляд": 0.0,
                  "музыка": {"играет": False}, "поза": {}}
    page7.route("**/state", lambda r: r.fulfill(
        body=json.dumps(состояние7), content_type="application/json"))

    def взгляд3d(пеленг):
        состояние7["взгляд"] = пеленг
        состояние7["поза"] = питомец7.кадр(
            {"эмоция": "спокоен", "человек": True, "взгляд": пеленг,
             "музыка": {"играет": False}}, 5.0)
        page7.wait_for_timeout(700)
        return page7.evaluate("window.__взгляд3d")
    слева = взгляд3d(0.6)
    справа = взгляд3d(-0.6)
    check("3D: человек слева от робота — и голова, и глаза уходят вправо по экрану, к нему",
          (слева["голова"] > 1.0, слева["глаза"] > 0.05), (True, True))
    check("3D: человек справа от робота — обе величины меняют знак вместе",
          (справа["голова"] < -1.0, справа["глаза"] < -0.05), (True, True))
    check("3D: голова и глаза не спорят друг с другом",
          (слева["голова"] < 0) == (слева["глаза"] < 0), True)
    check("3D: ошибок страницы нет", not ошибки7, True)
    page7.close()
    модель3d.unlink(missing_ok=True)

    # 15. Рассеянный взгляд: без человека, сценки и танца — голова и глаза
    # не должны стоять чучелом в одной точке между редкими сценками. Живая
    # жалоба, из-за которой это появилось: «двигается просто вверх-вниз-
    # влево-вправо, нет ощущения живого персонажа» — а на Live2D между
    # сценками голова и глаза вообще не шевелились. Своя страница: «поза»
    # на общем сервере всегда считает настоящий, живой Питомец (тот же
    # character.py, что и на роботе) — он сам бродит по экрану даже когда
    # сценарист выключен, и «идёт» время от времени даёт наклон ±4° сам по
    # себе, независимо от рассеянного взгляда; здесь ответ /state —
    # заглушка, наклон гарантированно 0. Само блуждание — суммы синусов с
    # периодами ~14-20 с; окно короче периода могло бы попасть на его
    # плоскую вершину и увидеть только малую часть размаха — не баг приёма,
    # а неудачное время проверки. Поэтому время здесь — свои, управляемые
    # часы (window.__виртуальное_время, подставлены до навигации), а не
    # реальные секунды: страница живёт с рождения только на них, ничего
    # общего с остальными страницами этого файла не делит.
    page4 = b.new_page(viewport={"width": 1280, "height": 800})
    page4.add_init_script("""
        window.__виртуальное_время = 1000;
        performance.now = () => window.__виртуальное_время;
    """)
    page4.route("**/live2dcubismcore.min.js", lambda r: r.fulfill(body="", content_type="text/javascript"))
    page4.route("**/pixi.min.js", lambda r: r.fulfill(body=PIXI_STUB, content_type="text/javascript"))
    page4.route("**/cubism4.min.js", lambda r: r.fulfill(body=CUBISM_STUB, content_type="text/javascript"))
    настройки4 = dict(настройки); настройки4["модель"] = "samples/mao/Mao.model3.json"
    page4.route("**/config.json", lambda r: r.fulfill(body=json.dumps(настройки4, ensure_ascii=False),
                                                      content_type="application/json"))
    состояние4 = {"эмоция": "спокоен", "говорит": "", "музыка": {"играет": False}, "сцена": {},
                  "поза": {"метка": "стоит", "x": 640, "наклон": 0, "подскок": 0, "рот": 0,
                           "нога": 0, "рассеян_x": 0.0, "рассеян_y": 0.0}}
    page4.route("**/state", lambda r: r.fulfill(body=json.dumps(состояние4, ensure_ascii=False),
                                                content_type="application/json"))
    ошибки4 = []
    page4.on("pageerror", lambda e: ошибки4.append(str(e)))
    page4.goto(url + "/avatar/", wait_until="load")
    page4.wait_for_function("!!(window.__listeners && window.__listeners.afterMotionUpdate)", timeout=10000)

    def взгляд_при(рас_x, рас_y, **ещё):
        """Поставить блуждание (и что ещё нужно) и дать сглаживанию сойтись."""
        состояние4["поза"]["рассеян_x"] = рас_x
        состояние4["поза"]["рассеян_y"] = рас_y
        состояние4["поза"]["метка"] = ещё.pop("метка", "стоит")
        состояние4["поза"]["наклон"] = ещё.pop("наклон", 0)
        состояние4["сцена"] = ещё.pop("сцена", {})
        состояние4.update(ещё)
        page4.wait_for_timeout(200)              # страница успевает опросить /state
        for _ in range(25):
            page4.evaluate("window.__виртуальное_время += 300")
            page4.evaluate("window.__listeners.afterMotionUpdate()")
        return (page4.evaluate("window.__params['ParamAngleX'] || 0"),
                page4.evaluate("window.__params['ParamEyeBallY'] || 0"))

    вправо = взгляд_при(1.0, 1.0)
    влево = взгляд_при(-1.0, -1.0)
    ровно = взгляд_при(0.0, 0.0)
    check("рассеянный взгляд: доли из общей логики доезжают до параметров модели",
          (round(вправо[0]), round(вправо[1], 2), round(влево[0]), round(влево[1], 2),
           round(ровно[0]), round(ровно[1], 2)),
          (6, 0.25, -6, -0.25, 0, 0.0))
    # Гасится там, где о взгляде уже позаботились по-настоящему: сценка
    # ведёт взгляд сама, танец крутит всё тело. Числа точные: если
    # блуждание всё-таки прибавится, к каждому прибавится ещё 6 градусов.
    со_сценкой = взгляд_при(1.0, 1.0, сцена={"к": 0.5})
    в_танце = взгляд_при(1.0, 1.0, метка="танцует",
                         музыка={"играет": True, "название": "x"})
    check("рассеянный взгляд молчит, когда взглядом заняты сценка или танец",
          (round(со_сценкой[0], 1), round(abs(в_танце[0]), 1)), (12.5, 0.0))
    # А НА КОНТАКТЕ ГЛАЗ не молчит: там отвод взгляда, и приглушённые числа
    # приходят уже из общей логики (character.py, ВЗГЛЯД_ОТВОД). Здесь
    # проверяем, что страница их не выбрасывает: +30 от пеленга (человек
    # слева от робота — вправо по экрану) плюс блуждание. Замереть наглухо
    # на контакте глаз и значит выглядеть куклой.
    в_контакте = взгляд_при(1.0, 1.0, наклон=6.0)
    в_контакте_ровно = взгляд_при(0.0, 0.0, наклон=6.0)
    check("на контакте глаз взгляд не замирает: блуждание доезжает и туда",
          (round(в_контакте_ровно[0], 1), round(в_контакте[0], 1)), (30.0, 36.0))
    # ДЫХАНИЕ В ГРУДИ. У домовёнка на роботе груди нет, и дыхание там видно
    # подскоком всего тела; у модели Live2D грудь есть, и для неё заведён
    # ParamBreath. Фазу вдоха считает ОДНА И ТА ЖЕ формула (character.py,
    # `_дыхание`): два персонажа обязаны дышать в такт, потому что дышит один
    # и тот же. Здесь проверяем, что фаза доезжает до параметра модели.
    def грудь_при(фаза):
        состояние4["поза"]["дыхание"] = фаза
        состояние4["поза"]["рассеян_x"] = состояние4["поза"]["рассеян_y"] = 0.0
        состояние4["поза"]["метка"] = "стоит"
        состояние4["сцена"] = {}
        page4.wait_for_timeout(200)
        for _ in range(25):
            page4.evaluate("window.__виртуальное_время += 300")
            page4.evaluate("window.__listeners.afterMotionUpdate()")
        return page4.evaluate("window.__params['ParamBreath'] || 0")
    вдох, выдох = грудь_при(1.0), грудь_при(0.0)
    check("фаза вдоха доезжает до груди модели (ParamBreath)",
          (round(вдох, 2), round(выдох, 2)), (1.0, 0.0))
    check("рассеянный взгляд: ошибок страницы нет", not ошибки4, True)
    page4.close()

    # 18. ШАР ИЗ ТОЧЕК (orb.html) — лицо по умолчанию. Заглушек нет: настоящие
    # three.js и WebGL, настоящий сервер и общие правила (face/orb_rules.py).
    # Время — только через __шаг (?тест=1), поэтому кадры воспроизводимы;
    # пиксели — со СНИМКА страницы, то есть ровно то, что уедет роботу.
    # Экран маленький (640×400): шар — в долях высоты, и разбирать PNG
    # четверти размера вчетверо быстрее.
    import orb_rules
    W8, H8 = 640, 400
    page8 = b.new_page(viewport={"width": W8, "height": H8})
    ошибки8 = []
    page8.on("pageerror", lambda e: ошибки8.append("JS: " + str(e)))
    page8.on("console", lambda m: ошибки8.append(m.text) if m.type == "error" else None)
    page8.goto(url + "/avatar/orb.html?тест=1&зерно=3&качество=3", wait_until="load")
    page8.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    шар8 = page8.evaluate("window.__шар")
    check("шар: страница собралась — пружины на видеокарте, правила пришли с ПК",
          (шар8["gpgpu"], шар8["правила"], шар8["точек"] >= 4096), (True, True, True))

    def шаг8(состояние, секунд):
        page8.evaluate("с => __задать(с)", состояние)
        return page8.evaluate(f"__шаг({секунд})")

    def снимок8():
        ш, _, к, строки = _пиксели_png(page8.screenshot())
        return lambda x, y: tuple(строки[int(y)][int(x) * к: int(x) * к + 3])

    def светлых(px, отбор, шаг=3, порог=200) -> int:
        return sum(1 for y in range(0, H8, шаг) for x in range(0, W8, шаг)
                   if отбор(x, y) and sum(px(x, y)) > порог)

    покой8 = {"эмоция": "спокоен", "шар": {"режим": "покой"}}
    инфо = шаг8(покой8, 2.0)
    px = снимок8()
    (cx, cy), R = инфо["центр"], инфо["радиус"]
    внутри = светлых(px, lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 < R * R)
    далеко = светлых(px, lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 > (1.6 * R) ** 2)
    check("шар: виден в снятом кадре — светлые точки в круге шара, вокруг темно",
          (внутри > 150, далеко), (True, 0))
    check("шар: на месте шара робота — центр 0.5 W × 0.43 H, радиус 0.30 H",
          (abs(cx - W8 / 2) < 0.02 * W8, abs(cy - 0.43 * H8) < 0.03 * H8, round(R / H8, 2)),
          (True, True, 0.30))
    check("шар: фон тот же, что у лица робота (#0b0f14)",
          all(abs(a - b_) <= 3 for a, b_ in zip(px(3, 3), (0x0B, 0x0F, 0x14))), True)

    инфо = шаг8({"эмоция": "слушаю", "шар": {"режим": "слух"}}, 2.5)
    check("шар: режим и цвет — из общих правил (orb_rules.json), те же, что у робота",
          (инфо["режим"], инфо["цвет_а"]), ("слух", list(orb_rules.ПАЛИТРЫ["слух"][0])))
    инфо = шаг8({"эмоция": "думаю", "шар": {"режим": "дума"}}, 2.5)
    check("…и для думающего тоже: смена режима доехала", (инфо["режим"], инфо["цвет_а"]),
          ("дума", list(orb_rules.ПАЛИТРЫ["дума"][0])))

    любит = {"эмоция": "рад", "шар": {"режим": "радость"},
             "сцена": {"имя": "обнял", "когда": "услышал_люблю", "текст": ""}}
    инфо = шаг8(любит, 1.6)
    check("шар: повод сценки «услышал_люблю» — сердце (жест из общей таблицы)",
          (orb_rules.СОБЫТИЯ["услышал_люблю"], "сердце" in инфо["эффекты"]), ("сердце", True))
    # Сердце целиком, а не половиной: pow() от отрицательного в GLSL — NaN, и
    # левая половина сердца пропадала (sin³ при sin < 0).
    px = снимок8()
    (cx, cy), R = инфо["центр"], инфо["радиус"]
    слева = светлых(px, lambda x, y: cx - 1.2 * R < x < cx - 0.1 * R and abs(y - cy) < 1.2 * R, шаг=2)
    справа = светлых(px, lambda x, y: cx + 0.1 * R < x < cx + 1.2 * R and abs(y - cy) < 1.2 * R, шаг=2)
    check("шар: сердце симметрично — обе половины на месте",
          (слева > 40, справа > 40, 0.6 < слева / max(1, справа) < 1.6), (True, True, True))
    инфо = шаг8(любит, 3.0)
    check("…и тот же повод не повторяется, пока сценка та же",
          "сердце" in инфо["эффекты"], False)

    касание = lambda н: {**покой8, "касание_шара": {"номер": н, "x": 0.5, "y": 0.35, "куда": "голова"}}
    первое = шаг8(касание(5), 0.1)["касание"]
    тот_же = шаг8(касание(5), 2.0)["касание"]
    новое = шаг8(касание(6), 0.1)["касание"]
    check("шар: касание с робота — волна по шару; тот же номер не повторяется, новый — да",
          (первое, тот_же, новое), (True, False, True))
    шаг8(покой8, 2.0)
    мимо = {**покой8, "касание_шара": {"номер": 9, "x": 0.95, "y": 0.9, "куда": "мимо"}}
    check("шар: «мимо» (постучали по стеклу рядом) — шар не вздрагивает, как и у робота",
          шаг8(мимо, 0.1)["касание"], False)

    инфо = шаг8({**покой8, "погода": {"t": 9, "код": 0}}, 2.0)
    check("шар: карточка слева — шар меньше и правее её, как у робота",
          (инфо["радиус"] < 0.9 * 0.30 * H8, инфо["центр"][0] - 1.05 * инфо["радиус"] > 0.38 * W8),
          (True, True))

    инфо = шаг8({**покой8, "сцена": {"имя": "ку", "текст": "Ку-ку"}}, 0.5)
    рамка = page8.evaluate("""(() => { const п = document.getElementById('пузырь');
        const r = п.getBoundingClientRect();
        return [r.left, r.top, r.width, r.height, п.textContent, п.style.opacity]; })()""")
    (cx, cy), R = инфо["центр"], инфо["радиус"]
    check("шар: реплика сценки — плашкой справа от шара, не на нём",
          (рамка[4], рамка[5], рамка[0] >= cx + R), ("Ку-ку", "1", True))
    длинно = "Сегодня облачно, плюс двенадцать, ветер слабый, без осадков"
    шаг8({**покой8, "сцена": {"имя": "ку", "текст": длинно}}, 0.5)
    рамка2 = page8.evaluate("""(() => { const r = document.getElementById('пузырь').getBoundingClientRect();
        return [r.left, r.right, r.height]; })()""")
    check("шар: длинная реплика переносится справа от шара, а не ложится на него",
          (рамка2[0] >= cx + R, рамка2[1] <= W8, рамка2[2] > 1.5 * рамка[3]), (True, True, True))

    инфо = шаг8({"эмоция": "сплю", "шар": {"режим": "сон"}}, 8.0)
    px = снимок8()
    (cx, cy), R = инфо["центр"], инфо["радиус"]
    середина = светлых(px, lambda x, y: abs(x - cx) < 0.9 * R and abs(y - cy) < 0.5 * R, шаг=2)
    бока = светлых(px, lambda x, y: abs(abs(x - cx) - 2.1 * R) < 0.2 * R and abs(y - cy) < 0.3 * R, шаг=2)
    check("шар: во сне — кольцо вокруг часов: по бокам светло, в середине (часы робота) пусто",
          (бока > 20, середина), (True, 0))
    check("…и во сне реплик нет", page8.evaluate("document.getElementById('пузырь').style.opacity"), "0")

    фигуры = list(orb_rules.ФИГУРЫ) + ["сатурн", "цветок", "узел", "атом", "бесконечность",
                                       "вихрь", "кристалл", "волна"]
    собрались = []
    шаг8(покой8, 2.0)
    for имя in фигуры:
        page8.evaluate("и => __сюжет('фигура', {фигура: и})", имя)
        собрались.append(page8.evaluate("__шаг(1.6)")["фигура"])
        page8.evaluate("__шаг(6.5)")
    check("шар: каждая фигура собирается (общие и свои у ПК)", собрались, фигуры)
    for сюжет in ("рой", "светлячки", "созвездие", "волна цвета", "перелив", "пульс",
                  "волчок", "вдох", "оглядывается", "за окном"):
        page8.evaluate("и => __сюжет(и)", сюжет)
        page8.evaluate("__шаг(1.0)")
    инфо = page8.evaluate("__шаг(14.0)")
    px = снимок8()
    (cx, cy), R = инфо["центр"], инфо["радиус"]
    check("шар: после всех сюжетов шар снова шар — точки на месте, ничего не улетело в NaN",
          (светлых(px, lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 < R * R) > 150,
           светлых(px, lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 > (1.6 * R) ** 2)), (True, 0))
    check("шар: ошибок страницы нет", ошибки8, [])
    page8.close()

    # Без float-текстур (?gpgpu=0): пружин нет, всё считается в шейдере точек —
    # шар обязан рисоваться и так, иначе на слабом ПК роботу уйдёт пустота.
    page9 = b.new_page(viewport={"width": W8, "height": H8})
    ошибки9 = []
    page9.on("pageerror", lambda e: ошибки9.append("JS: " + str(e)))
    page9.on("console", lambda m: ошибки9.append(m.text) if m.type == "error" else None)
    page9.goto(url + "/avatar/orb.html?тест=1&зерно=3&качество=3&gpgpu=0", wait_until="load")
    page9.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    page9.evaluate("__событие('сбор')")
    инфо = page9.evaluate("__шаг(2.0)")
    ш_, _, к_, строки_ = _пиксели_png(page9.screenshot())
    (cx, cy), R = инфо["центр"], инфо["радиус"]
    видно9 = sum(1 for y in range(0, H8, 3) for x in range(0, W8, 3)
                 if (x - cx) ** 2 + (y - cy) ** 2 < R * R and sum(строки_[y][x * к_: x * к_ + 3]) > 200)
    check("шар без пружин (?gpgpu=0): рисуется, разлёт не ломает", (инфо["gpgpu"], видно9 > 150, ошибки9),
          (False, True, []))
    page9.close()

    # Живая страница (без ?тест): состояние робота → сервер ПК → опрос
    # страницы → режим шара. Вся цепочка, как на ПК.
    page10 = b.new_page(viewport={"width": W8, "height": H8})
    page10.goto(url + "/avatar/orb.html?качество=3", wait_until="load")
    page10.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    post({"эмоция": "слушаю", "человек": True})
    try:
        page10.wait_for_function("window.__шар.режим === 'слух'", timeout=5000)
        живой = page10.evaluate("window.__шар.режим")
    except Exception:                                # noqa: BLE001
        живой = page10.evaluate("window.__шар.режим")
    check("шар: робот сказал «слушаю» — живая страница через сервер ПК перешла в «слух»",
          живой, "слух")
    # Робот замолчал на пять секунд (после неудачного POST'а он так и
    # делает) — это запинка, а не сон: шар держит последнее живое состояние,
    # а не проваливается в кольцо сна, чтобы потом взорваться сбором.
    page10.wait_for_timeout(int((k.Аватар.СТАРЕЕТ + 1.5) * 1000))
    check("шар: робот замолчал на несколько секунд — шар не засыпает (запинка, не сон)",
          (page10.evaluate("window.__шар.режим"),
           "сбор" in page10.evaluate("window.__шар.эффекты")), ("слух", False))
    page10.close()

    # Правила не пришли — шар всё равно рисуется, а беда сказана словами.
    page11 = b.new_page(viewport={"width": W8, "height": H8})
    page11.route("**/orb_rules.json", lambda r: r.fulfill(status=500, body="нет"))
    page11.goto(url + "/avatar/orb.html?тест=1&качество=3", wait_until="load")
    page11.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    check("шар: правила не пришли — шар рисуется, а на странице сказано, чего нет",
          (page11.evaluate("window.__шар.правила"),
           "Правила шара" in page11.evaluate("document.getElementById('беда').textContent")),
          (False, True))
    page11.close()

    # Со свечением (качество 1 — так страница встаёт на ПК с видеокартой).
    # Проверки выше идут без него, и сведение свечения не собиралось ни разу:
    # кириллица в имени переменной шейдера роняла программу, и на настоящей
    # видеокарте шар был пустым экраном. Здесь — что шейдеры собрались и шар
    # в кадре есть. А с «робот=1» (так открывает Съёмка) беда не видна.
    page12 = b.new_page(viewport={"width": W8, "height": H8})
    шейдеры12 = []
    page12.on("console", lambda m: шейдеры12.append(m.text)
              if ("Shader Error" in m.text or "VALIDATE_STATUS false" in m.text
                  or "program not valid" in m.text) else None)
    page12.route("**/orb_rules.json", lambda r: r.fulfill(status=500, body="нет"))
    page12.goto(url + "/avatar/orb.html?тест=1&зерно=3&качество=1&робот=1", wait_until="load")
    page12.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    page12.evaluate("с => __задать(с)", {"эмоция": "спокоен", "шар": {"режим": "покой"}})
    инфо12 = page12.evaluate("__шаг(2.0)")
    ш12, _, к12, строки12 = _пиксели_png(page12.screenshot())
    px12 = lambda x, y: tuple(строки12[int(y)][int(x) * к12: int(x) * к12 + 3])  # noqa: E731
    (cx12, cy12), R12 = инфо12["центр"], инфо12["радиус"]
    светлых12 = sum(1 for y in range(0, H8, 3) for x in range(0, W8, 3)
                    if (x - cx12) ** 2 + (y - cy12) ** 2 < R12 * R12 and sum(px12(x, y)) > 200)
    check("шар: со свечением (качество 1) шейдеры собрались и шар в кадре виден",
          (page12.evaluate("window.__шар.свечение"), шейдеры12[:2], светлых12 > 150),
          (True, [], True))
    # Разлёт при сборе (приветствие, пробуждение, музыка) не ложится на угол
    # часов и даты робота: он рисует их поверх кадра, и точки под цифрами —
    # мусор. Раньше точки долетали до краёв экрана.
    page12.evaluate("([в, г]) => __событие(в, г)", ["сбор", None])
    page12.evaluate("__шаг(0.75)")
    ш12, _, к12, строки12 = _пиксели_png(page12.screenshot())
    угол12 = sum(1 for y in range(0, int(0.15 * H8), 2) for x in range(int(0.67 * W8), W8, 2)
                 if sum(строки12[y][x * к12: x * к12 + 3]) > 150)
    check("шар: разлёт при сборе не залетает в угол часов и даты робота", угол12, 0)
    # Проснулся — кольцо само собирается в шар, лицом к нам: без разлёта
    # (он рвал кольцо в облако до часов) и без белого столба ребром кольца.
    page12.evaluate("с => __задать(с)", {"эмоция": "сплю", "шар": {"режим": "сон"}})
    page12.evaluate("__шаг(6.0)")
    page12.evaluate("с => __задать(с)", {"эмоция": "спокоен", "шар": {"режим": "покой"},
                                         "сцена": {"имя": "проснулся", "когда": "проснулся"}})
    инфо_просн = page12.evaluate("__шаг(0.4)")
    ш12, _, к12, строки12 = _пиксели_png(page12.screenshot())
    угол_просн = sum(1 for y in range(0, int(0.15 * H8), 2) for x in range(int(0.67 * W8), W8, 2)
                     if sum(строки12[y][x * к12: x * к12 + 3]) > 150)
    светлые = [sum(строки12[y][x * к12: x * к12 + 3]) for y in range(0, H8, 2)
               for x in range(0, W8, 2) if sum(строки12[y][x * к12: x * к12 + 3]) > 150]
    белых = sum(1 for в in светлые if в > 720) / max(1, len(светлые))
    check("шар: проснулся — без разлёта, кольцо собирается в шар, угол часов чист, столба нет",
          ("сбор" in инфо_просн["эффекты"], угол_просн, белых < 0.05), (False, 0, True))
    check("шар: кадры роботу (робот=1) — беда записана, но на экране её нет",
          ("Правила шара" in page12.evaluate("document.getElementById('беда').textContent"),
           page12.evaluate("getComputedStyle(document.getElementById('беда')).display")),
          (True, "none"))
    page12.close()

    # ЕЗДА: шар едет вместе с роботом. Состояния — в точности как их шлёт
    # робот (ход, итог_хода, делает; voice/robot_voice/face.py), а шар.ход —
    # от настоящего orb_rules.ход_шара: проверяется страница, а не копия
    # правил. Страница свежая — для неё это первое состояние после загрузки.
    import math
    def ход_робота(**поля):
        return {"вперёд": 0.0, "вбок": 0.0, "поворот": 0.0, "факт": None, "упор": 0.0,
                "старт": 4, "возраст": 0.03, "связь": True, **поля}
    def едет(**поля):
        с = {"эмоция": "спокоен", "ход": ход_робота(**поля)}
        с["шар"] = {"режим": "покой", "ход": orb_rules.ход_шара(с)}
        return с
    page13 = b.new_page(viewport={"width": W8, "height": H8})
    ошибки13 = []
    page13.on("pageerror", lambda e: ошибки13.append("JS: " + str(e)))
    page13.on("console", lambda m: ошибки13.append(m.text) if m.type == "error" else None)
    page13.goto(url + "/avatar/orb.html?тест=1&зерно=3&качество=3", wait_until="load")
    page13.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    def шаг13(состояние, секунд):
        page13.evaluate("с => __задать(с)", состояние)
        return page13.evaluate(f"__шаг({секунд})")
    def центроид13():
        _, _, к, строки = _пиксели_png(page13.screenshot())
        вес = сх = 0.0
        for y in range(0, H8, 2):
            for x in range(0, W8, 2):
                я = sum(строки[y][x * к: x * к + 3])
                if я > 150:
                    вес += я; сх += я * x
        return сх / max(1.0, вес), вес

    # Стоит, но чуть давит (упор выше порога): сюжеты «от скуки» молчат и не
    # мешают мерить — фигура или волчок посреди замера сдвинули бы всё.
    тихо = едет(упор=0.15)
    # Итог езды — по номеру; первый же увиденный номер — только отметка.
    стоит = {**тихо, "итог_хода": {"номер": 7, "как": "доехал", "причина": "", "сторона": None},
             "делает": {"номер": 3, "что": "drive", "итог": "отказ"}}
    def с_итогом(номер, как, сторона=None):
        return {**стоит, "итог_хода": {"номер": номер, "как": как, "причина": "", "сторона": сторона}}
    первое = шаг13(стоит, 0.6)["эффекты"]
    check("шар: первое состояние после загрузки — только отметка: старый итог, старт и отказ не играются",
          [в for в in ("тормоз", "кивок", "трогаюсь", "тряска") if в in первое], [])
    кнопка = [шаг13(с_итогом(8, "кнопка"), 0.1)["эффекты"], шаг13(с_итогом(8, "кнопка"), 0.6)["эффекты"]]
    check("шар: стоп-кнопка — вздрог, и ни кивка прибытия, ни тормоза — ни сразу, ни следом",
          ("вздрог" in кнопка[0], [в for э in кнопка for в in э if в in ("кивок", "тормоз")]),
          (True, []))
    шаг13(с_итогом(8, "кнопка"), 1.5)
    сразу = шаг13(с_итогом(9, "доехал"), 0.1)["эффекты"]
    следом = шаг13(с_итогом(9, "доехал"), 0.4)["эффекты"]
    check("шар: доехал — сначала тормоз, кивок — следом, а не разом",
          ("тормоз" in сразу, "кивок" in сразу, "кивок" in следом), (True, False, True))
    тронулся = шаг13({**с_итогом(9, "доехал"), **едет(старт=5, упор=0.15)}, 0.1)["эффекты"]
    шаг13({**с_итогом(9, "доехал"), **едет(старт=5, упор=0.15),
           "делает": {"номер": 4, "что": "drive", "итог": "идёт"}}, 0.1)
    отказ = шаг13({**с_итогом(9, "доехал"), **едет(старт=5, упор=0.15),
                   "делает": {"номер": 4, "что": "drive", "итог": "отказ"}}, 0.1)
    check("шар: тронулся (новый ход.старт) — «трогаюсь»; отказ ехать — мотает «головой»",
          ("трогаюсь" in тронулся, "тряска" in отказ["эффекты"]), (True, True))
    стоит = {**стоит, **едет(старт=5, упор=0.15), "делает": {"номер": 4, "что": "drive", "итог": "отказ"}}
    до = шаг13(с_итогом(9, "доехал"), 2.0)["центр"][0]
    после = шаг13(с_итогом(10, "сторож", 1), 0.15)
    # Слева у робота — справа на экране для того, кто стоит перед ним (см.
    # orb_rules.ХОД, экран_знак): шар отшатывается влево по экрану.
    check("шар: стоп у препятствия слева у робота — отпрянул влево по экрану, вмятина с той стороны (тычок)",
          ("отпрянул" in после["эффекты"], "тычок" in после["эффекты"],
           до - после["центр"][0] > 0.04 * после["радиус"]), (True, True, True))
    шаг13(тихо, 2.0)

    # Знаки: поворот влево — спин и взгляд в одну сторону, крен в обратную, и
    # так же, как у правил (страница знак не теряет и не переворачивает).
    цель = orb_rules.ход_шара({"ход": ход_робота(поворот=1.0)})
    х = шаг13(едет(поворот=1.0), 1.0)["ход"]
    check("шар: поворот — спин и взгляд в одну сторону, крен в обратную, знаки — как у правил",
          (х["спин"] * х["взгляд"][0] > 0, х["крен"] * х["спин"] < 0,
           (х["спин"] < 0) == (цель["спин"] < 0), abs(х["спин"]) > 0.8 * abs(цель["спин"])),
          (True, True, True, True))
    # И сами знаки, а не только согласие между ними: согласованно зеркальный
    # шар проходил проверку выше. Поворот влево — нос робота уходит к правой
    # руке того, кто перед ним: спин и зрачок — вправо по экрану, верх — туда же.
    check("шар: поворот влево — спин и зрачок вправо по экрану, к носу робота, крен в поворот",
          (х["спин"] > 0, х["взгляд"][0] > 0, х["крен"] < 0), (True, True, True))
    # Человек слева от робота — у правого края экрана: зрачок к нему.
    человек = {"эмоция": "спокоен", "человек": True}
    # Поза — как её считает ПК (character.py: поза.взгляд — доля пеленга).
    к_левому = шаг13({**человек, "взгляд": 0.5, "поза": {"взгляд": 0.5}}, 1.5)["взор"][0]
    к_правому = шаг13({**человек, "взгляд": -0.5, "поза": {"взгляд": -0.5}}, 1.5)["взор"][0]
    check("шар: человек слева от робота — зрачок вправо по экрану, к нему; справа — влево",
          (к_левому > 0.1, к_правому < -0.1), (True, True))
    шаг13(тихо, 2.5)
    c0, вес0 = центроид13()
    шаг13(едет(вбок=0.2), 1.5)
    c1, вес1 = центроид13()
    сдвиг = (c1 - c0) / W8
    check("шар: едет боком — шар на снимке съезжает в ту же сторону, что и взгляд (≥ 0.03 W)",
          (abs(сдвиг) >= 0.03, (сдвиг < 0) == (orb_rules.ход_шара({"ход": ход_робота(вбок=0.2)})["сдвиг"][0] < 0),
           вес1 > 0.5 * вес0), (True, True, True))
    шаг13(тихо, 2.5)

    # Едет — сюжетов «от скуки» нет: идущий уходит, новые не начинаются.
    page13.evaluate("и => __сюжет(и)", "рой")
    шёл = шаг13(едет(), 1.0)["сюжет"]
    сюжеты = []
    for _ in range(40):                                # двадцать секунд в повороте
        инфо = шаг13(едет(поворот=1.0), 0.5)
        сюжеты.append(инфо["сюжет"] or инфо["фигура"])
    check("шар: едет — сюжет уходит за полсекунды, и двадцать секунд ни роя, ни фигур",
          (шёл, sorted(set(сюжеты))), ("рой", [""]))
    после = [шаг13(едет(), 0.5)["сюжет"] for _ in range(60)]
    check("…а встал — сюжеты снова идут", any(после), True)

    # 18. Дикие числа: 10 рад/с, −5 м/с — через правила; мусор прямо в шар.ход
    # — мимо них. Ни NaN в отчёте, ни пустого кадра.
    инфо = шаг13(едет(поворот=10.0, вперёд=-5.0), 1.0)
    page13.evaluate("""() => __задать({эмоция: "спокоен", ход: {старт: NaN},
        итог_хода: {номер: Infinity, как: "доехал"},
        шар: {режим: "покой", ход: {сила: NaN, крен: Infinity, спин: -Infinity, взгляд: [NaN, "x"],
              сдвиг: "мусор", радиус: null, зрачок: {}, течение: NaN, дрожь: 1e308,
              куда: [NaN, Infinity, 0], сюжеты_нельзя: "да"}}})""")
    мусор = page13.evaluate("__шаг(1.0)")
    def конечны(и):
        х = и["ход"]
        return all(isinstance(ч, (int, float)) and math.isfinite(ч)
                   for ч in [и["радиус"], *и["центр"], х["сила"], х["крен"], х["спин"], х["радиус_к"],
                             х["дрожь"], *х["взгляд"], *х["сдвиг"]])
    _, _, к13, строки13 = _пиксели_png(page13.screenshot())
    (cx, cy), R = мусор["центр"], мусор["радиус"]
    видно13 = sum(1 for y in range(0, H8, 3) for x in range(0, W8, 3)
                  if (x - cx) ** 2 + (y - cy) ** 2 < R * R and sum(строки13[y][x * к13: x * к13 + 3]) > 200)
    check("шар: дикие числа и мусор в ходе — без NaN, шар на месте",
          (конечны(инфо), конечны(мусор), видно13 > 150), (True, True, True))
    # Запинка связи (состояние «устарело»): ход из него шар не тянет.
    устал = шаг13({**едет(поворот=1.0), "устарело": 2.0}, 1.0)["ход"]["сила"]
    check("шар: состояние устарело — ход из него не тянет шар", устал < 0.01, True)
    check("шар: езда — ошибок страницы нет", ошибки13, [])
    page13.close()

    # 17. Живая цепочка: робот → сервер ПК → опрос страницы → сглаживание.
    # Нарастает за доли секунды; робот замолчал — ПК состарил ход (живёт
    # 0.4 с), и шар осел (спад 0.35 с: до 0.05 — ещё три постоянные), а режим
    # держится — это не сон.
    page14 = b.new_page(viewport={"width": W8, "height": H8})
    page14.goto(url + "/avatar/orb.html?качество=3", wait_until="load")
    page14.wait_for_function("window.__шар && window.__шар.готов", timeout=20000)
    post({"эмоция": "слушаю", "человек": True})
    try:
        page14.wait_for_function("window.__шар.режим === 'слух'", timeout=5000)
    except Exception:                                # noqa: BLE001
        pass
    едет_живьём = {"эмоция": "слушаю", "человек": True, "ход": ход_робота(поворот=1.0, возраст=0.02)}
    # Время — страничное (__шар.t), а не настенное: шаг кадра ограничен 0.1 с,
    # и на медленных программных кадрах занятой машины настенные часы уходят
    # вперёд страницы — проверка падала бы от загрузки, а не от задержки.
    нарастание = page14.evaluate("""async (тело) => {
        const t0 = performance.now(), с0 = window.__шар.t;
        await fetch("state", { method: "POST", body: JSON.stringify(тело) });
        while (performance.now() - t0 < 4000) {
          if (window.__шар.ход.сила > 0.3) return (window.__шар.t - с0) * 1000;
          await new Promise((r) => requestAnimationFrame(r));
        }
        return -1; }""", едет_живьём)
    for _ in range(10):                              # робот шлёт десять раз в секунду
        post(едет_живьём)
        time.sleep(0.1)
    спад, режим14 = page14.evaluate("""async () => {
        const t0 = performance.now(), с0 = window.__шар.t;
        while (performance.now() - t0 < 8000) {
          if (window.__шар.ход.сила < 0.05) return [(window.__шар.t - с0) * 1000, window.__шар.режим];
          await new Promise((r) => requestAnimationFrame(r));
        }
        return [-1, window.__шар.режим]; }""")
    check("шар: живая цепочка — ход доезжает и нарастает быстрее 250 мс от посылки робота",
          0 <= нарастание < 250, True)
    check("…робот замолчал — шар осел за 0.4 + 0.05 + 3·0.35 с с запасом, а режим держится",
          (0 <= спад < 1800, режим14), (True, "слух"))
    page14.close()
    b.close()

srv.shutdown()
плохо = [и for и in итог if not и[1]]
for что, ок, есть, надо in итог:
    print(("✓ " if ок else "✗ ") + что + ("" if ок else f"  получено {есть!r}, ожидалось {надо!r}"))
print("ошибки страницы:", ошибки or "нет")
sys.exit(1 if плохо else 0)
