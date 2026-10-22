@echo off
rem Запуск мозга робота на этом ПК. Двойной щелчок — и всё.
rem
rem Окно должно остаться открытым: пока оно живо, робот думает здесь.
rem Закрыть — робот замолчит, и лица на его экране не будет: он работает
rem только в паре с этим ПК. Для лица нужен Playwright с Chrome или Edge.

chcp 65001 >nul
cd /d "%~dp0"

rem Модели тяжёлые, а системный диск обычно мал. Если переменная не задана —
rem не трогаем: значит человек уже решил, где им лежать.
if "%OLLAMA_MODELS%"=="" echo -- OLLAMA_MODELS не задана, модели лягут на системный диск

rem Что запускать. Поменять можно прямо здесь.
rem Нерассуждающая сборка qwen3: гибрид qwen3:4b сначала думает вслух —
rem секунды молчания на каждой фразе, а при пределе ответа робота (384
rem токена) размышления съедают весь ответ. Нет её на диске — ниже скачаем.
set MODEL=qwen3:4b-instruct-2507-q4_K_M
rem GigaAM, а не whisper: она обучена на русском, и на этой видеокарте
rem секунда звука разбирается за десятую долю секунды. Whisper остаётся
rem запасным — set STT=whisper, и тогда важен размер ниже.
set STT=gigaam
set WHISPER=small
rem Ксения — единственный женский голос Silero, который не сваливается в
rem механическое чтение на длинных фразах.
set VOICE=xenia
set PORT=4000

echo == мозг Кузи ==
echo модель        %MODEL%
echo распознавание %STT%
echo голос         %VOICE%
echo порт          %PORT%
echo.
echo Адрес для робота — строка IPv4 отсюда:
ipconfig | findstr /c:"IPv4"
echo.

rem Модели нет — скачиваем один раз, около 2.5 ГБ. Без неё мост отвечал бы
rem роботу ошибкой на каждую фразу, и робот молчал бы. Ollama не запущена
rem или не установлена — молча идём дальше: kuzya_pc.py скажет об этом сам.
ollama list >nul 2>nul
if not errorlevel 1 (
  ollama list | findstr /i /c:"%MODEL%" >nul
  if errorlevel 1 (
    echo -- модели %MODEL% нет, скачиваю один раз: ollama pull %MODEL%
    ollama pull %MODEL%
    echo.
  )
)

rem Ollama отвечает быстрее и держит окно больше, если ей сказать три вещи:
rem считать внимание быстрым способом, хранить кэш вдвое компактнее (q8_0) и
rem держать один разговор за раз (на 6 ГБ несколько кэшей не помещаются).
rem Ollama из трея берёт их из переменных пользователя, а не из этого окна:
rem set здесь до неё не дойдёт. Поэтому подсказка: один раз setx, потом
rem выйти из Ollama в трее и запустить снова. Видит их и kuzya_pc.py — тогда
rem он сам просит окно побольше, 10240 вместо 8192.
set OLLAMA_HINT=
if not "%OLLAMA_FLASH_ATTENTION%"=="1" set OLLAMA_HINT=1
if not "%OLLAMA_KV_CACHE_TYPE%"=="q8_0" set OLLAMA_HINT=1
if not "%OLLAMA_NUM_PARALLEL%"=="1" set OLLAMA_HINT=1
if defined OLLAMA_HINT (
  echo -- Ollama без быстрого внимания и сжатого кэша. Один раз выполнить:
  echo      setx OLLAMA_FLASH_ATTENTION 1
  echo      setx OLLAMA_KV_CACHE_TYPE q8_0
  echo      setx OLLAMA_NUM_PARALLEL 1
  echo    затем выйти из Ollama в трее, запустить её снова и открыть start.cmd заново.
  echo.
)

rem Своё окружение, если оно есть. Колесо torch с поддержкой видеокарты —
rem это шесть гигабайт на диске, и на системном они находятся не всегда:
rem установка обрывается на «No space left on device», причём УЖЕ УДАЛИВ
rem старую сборку. Робот остаётся без распознавания вовсе.
rem
rem Поэтому окружение принято держать на большом диске рядом с моделями.
rem Путь можно задать своим: set ROBOT_VENV=D:\что-угодно
if "%ROBOT_VENV%"=="" set ROBOT_VENV=F:\robot-venv
set PY=python
if exist "%ROBOT_VENV%\Scripts\python.exe" (
  set PY="%ROBOT_VENV%\Scripts\python.exe"
  echo окружение     %ROBOT_VENV%
) else (
  echo окружение     системное ^(своего нет^)
)

%PY% kuzya_pc.py --model %MODEL% --stt %STT% --whisper %WHISPER% --voice %VOICE% --port %PORT%

rem Сюда попадаем, только если сервер упал. Без паузы окно закроется, и
rem причину прочитать будет негде.
echo.
echo Сервер остановился. Строки выше объясняют почему.
pause
