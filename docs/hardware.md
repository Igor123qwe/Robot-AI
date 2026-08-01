# Железо

## Собрано и работает

| Узел | Что | Статус |
|---|---|---|
| Шасси | WHEELTEC WD550, 4 меканум-колеса 75 мм, моторы MG513 с энкодерами 500 линий, маятниковая подвеска | едет |
| Нижний контроллер | плата WHEELTEC C30D на STM32F407VET6 (драйверы моторов, OLED, BT-модуль). Тумблер `Motor OFF/ON` | работает |
| Компьют | D-Robotics RDK X5: 8×A55, 10 TOPS BPU, 8 ГБ, Wi-Fi 6, 4×USB3.0, 2×MIPI CSI, DSI, HDMI, CAN FD | работает |
| Батарея | WHEELTEC E351S, 3S li-ion, 10.8 В ном / 12.6 В макс, 5100 мА·ч. Зарядник DC5525 12.6 В / 1.9 А | ~11.2 В |
| Питание RDK X5 | **от платы C30D по USB-C** (выход 5 В / 5 А). Правый Type-C, рядом с клеммой батареи | работает |

### Питание — важное

- Отдельный DC-DC 12→5 В **не нужен**: C30D питает RDK X5 сама.
- 19 В ноутбучные блоки **не подключать**.
- 12 В напрямую в RDK X5 **не подавать** — только 5 В.

## Заказано, ещё не приехало

- Камера DECXIN AR0234 130°, global shutter, 2 МП 1080p/90fps, USB
- ToF DFRobot 8×8 Matrix (VL53L7CX, RP2040, 60° FOV, 3.5 м, USB-C/I2C/UART)
- ReSpeaker Lite (XMOS XU-316, 2 мика, аппаратный AEC, усилитель 5 Вт)
  + динамик SOTAMIA 3" 4 Ω 20 Вт
- Виброразвязка M3 (демпферы FPV)

## Осталось купить

### Экран — единственный крупный незакрытый пункт

RDK X5 поддерживает **только** панели Waveshare DSI из списка совместимости:
2.8 / 3.4(C) / 4.3 / 7(C) 1024×600 / 7.9 / 8(C) 1280×800 / 10.1(C).

- подключение кабелем DSI-Cable-12cm
- настройка через `srpi-config` (**не** `config.txt`)
- HDMI и DSI одновременно не работают

Критерии при выборе карточки на AliExpress:
- бренд **в поле** = Waveshare (в заголовке бренд ничего не значит)
- IPS, не TN
- модель ровно «7inch DSI LCD (C)» или «8inch DSI LCD (C)»
- лучше в Waveshare Official Store
- 8"(C) = 194×119 мм, питание 5 В / 3 А **отдельно**

### Прочее

- кнопка питания / e-stop
- паяльник, стяжки
- ещё один USB-A→C
- корпус YAHBOOM для RDK X5 с вентилятором и кронштейном камеры (1679 ₽) —
  выбран, но **проверить, влезет ли между этажами**

Картридер и DC-DC из списка вычеркнуты — не понадобились.

## Система на роботе

- Образ: фирменная сборка WHEELTEC поверх RDK OS 3.2.3 (Ubuntu 22.04.5, ядро 6.1.83 aarch64)
- Пользователь `wheeltec`
- ROS 2 Humble + TogetheROS (`/opt/tros/humble`)
- **Не перепрошивать на сток** — в образе готовые драйверы шасси и пакеты WHEELTEC

Окружение из `~/.bashrc`:

```bash
source /opt/ros/humble/setup.bash
source /opt/tros/humble/setup.bash
source /home/wheeltec/wheeltec_ros2/install/setup.bash
source /home/wheeltec/wheeltec_stepper_arm/install/setup.bash
```

## Готовые пакеты в `~/wheeltec_ros2/src`

Писать с нуля не надо, многое уже есть:

- `turn_on_wheeltec_robot` — главный узел шасси
- `wheeltec_robot_keyboard` — управление с клавиатуры
- `wheeltec_mic`, `wheeltec_mic_aiui` — микрофонный массив
- `tts_make_ros2` — синтез речи
- `largemodel`, `ollama_ros_chat-ros2` — интеграция с LLM
- `simple_follower_ros2` — следование (лазер / визуальное / линия / ArUco)
- `wheeltec_bodyreader` — скелет, поза, следование за человеком
- `wheeltec_robot_kcf` — трекинг объектов
- `usb_cam-ros2`, `web_video_server-ros2` — камера и стриминг
- `wheeltec_robot_nav2`, `wheeltec_robot_slam`, `wheeltec_robot_rtab` — навигация и SLAM
- `auto_recharge_ros2` — автовозврат на зарядку

Полный список команд запуска (на китайском):
`~/wheeltec_ros2/src/ROS2-V5.0(humble)常用指令.txt`

## Полезные топики

| Топик | Тип | Что |
|---|---|---|
| `/cmd_vel` | `geometry_msgs/msg/Twist` | команда скорости (linear.x, linear.y, angular.z) |
| `/PowerVoltage` | `std_msgs/msg/Float32` | напряжение батареи, В |

## Геометрия шасси (для кинематики меканума)

- половина колёсной базы ≈ 0.0975 м
- половина колеи ≈ 0.0850 м

Используется в `web/pult.html` для индикаторов колёс. Если окажутся неточными —
править константы `HALF_WHEELBASE` / `HALF_TRACK` там же.
