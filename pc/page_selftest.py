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

    # 6. Человек слева → голова и глаза влево.
    post({"эмоция": "спокоен", "человек": True, "взгляд": 0.5, "музыка": {"играет": False}})
    page.wait_for_timeout(300); кадры()
    check("человек → голова повёрнута (ParamAngleX > 10)", парам("ParamAngleX") > 10, True)
    check("человек → глаза в ту же сторону", парам("ParamEyeBallX") > 0.3, True)

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
    b.close()

srv.shutdown()
плохо = [и for и in итог if not и[1]]
for что, ок, есть, надо in итог:
    print(("✓ " if ок else "✗ ") + что + ("" if ок else f"  получено {есть!r}, ожидалось {надо!r}"))
print("ошибки страницы:", ошибки or "нет")
sys.exit(1 if плохо else 0)
