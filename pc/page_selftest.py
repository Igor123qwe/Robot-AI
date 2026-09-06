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

with sync_playwright() as p:
    свой = os.environ.get("KUZYA_BROWSER", "").strip()
    b = None
    if свой:
        b = p.chromium.launch(executable_path=свой, headless=True)
    else:
        for канал in ("msedge", "chrome", None):
            try:
                b = p.chromium.launch(channel=канал, headless=True)
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

    # 6. Человек слева → голова и глаза ВЛЕВО, к нему. Пеленг влево
    # положительный, а у Cubism «вправо по экрану» — это ПЛЮС ParamAngleX
    # (так же ставит взгляд focus() самой библиотеки). Значит человеку слева
    # соответствует ОТРИЦАТЕЛЬНЫЙ ParamAngleX. Раньше здесь ждали «> 10» —
    # и проверка закрепляла ошибку: модель отворачивалась от человека, а
    # снаружи это выглядело как «персонаж за мной не следит».
    post({"эмоция": "спокоен", "человек": True, "взгляд": 0.5, "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("человек СЛЕВА → голова повёрнута к нему, влево (ParamAngleX < -10)",
          парам("ParamAngleX") < -10, True)
    check("…и глаза туда же, а не в другую сторону", парам("ParamEyeBallX") < -0.3, True)
    # И зеркально: человек справа (пеленг отрицательный) — вправо.
    post({"эмоция": "спокоен", "человек": True, "взгляд": -0.5, "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("человек СПРАВА → голова и глаза вправо",
          (парам("ParamAngleX") > 10, парам("ParamEyeBallX") > 0.3), (True, True))


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
    for _ in range(15):
        page2.evaluate("window.__listeners.afterMotionUpdate()")
        page2.wait_for_timeout(50)
        post({"эмоция": "спокоен", "говорит": "", "музыка": {"играет": True, "название": "x"}})
    body_angle_x = парам2("PARAM_BODY_ANGLE_X")
    check("старый формат: корпус качается в танце (PARAM_BODY_ANGLE_X)", abs(body_angle_x) > 0.5, True)
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
    # Гасится там, где о взгляде уже позаботились. Числа точные: если
    # блуждание всё-таки прибавится, к каждому прибавится ещё 6 градусов.
    со_сценкой = взгляд_при(1.0, 1.0, сцена={"к": 0.5})
    в_контакте = взгляд_при(1.0, 1.0, наклон=6.0)
    в_танце = взгляд_при(1.0, 1.0, метка="танцует",
                         музыка={"играет": True, "название": "x"})
    check("рассеянный взгляд молчит, когда взглядом заняты сценка, контакт глаз или танец",
          (round(со_сценкой[0], 1), round(в_контакте[0], 1), round(abs(в_танце[0]), 1)),
          (12.5, -30.0, 0.0))       # −30: наклон +6 — это человек СЛЕВА, см. выше
    check("рассеянный взгляд: ошибок страницы нет", not ошибки4, True)
    page4.close()
    b.close()

srv.shutdown()
плохо = [и for и in итог if not и[1]]
for что, ок, есть, надо in итог:
    print(("✓ " if ок else "✗ ") + что + ("" if ок else f"  получено {есть!r}, ожидалось {надо!r}"))
print("ошибки страницы:", ошибки or "нет")
sys.exit(1 if плохо else 0)
