#!/usr/bin/env python3
"""Собрать минимальную, но НАСТОЯЩУЮ модель VRM 1.0 — для самопроверок.

Зачем это здесь, а не готовым файлом в репозитории: чужие модели VRM (даже
бесплатные) весят десятки мегабайт и идут со своими лицензиями, а проверять
надо не их, а наш код. Эта модель — скелет из обязательных костей VRM,
светящийся зелёный кубик вместо тела и пустые выражения со стандартными
именами. Её достаточно, чтобы страница 3d.html прошла весь настоящий путь:
загрузка glb, разбор расширения VRMC_vrm, поиск костей, выражения, кадры в
WebGL — то есть всё, что заглушкой не проверишь.

    python pc/vrm_проба.py путь.vrm
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

# Кости, без которых VRM 1.0 недействительна (обязательные по спецификации).
ОБЯЗАТЕЛЬНЫЕ_КОСТИ = [
    "hips", "spine", "head",
    "leftUpperArm", "leftLowerArm", "leftHand",
    "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot",
    "rightUpperLeg", "rightLowerLeg", "rightFoot",
]
# Имена выражений — стандартные для VRM: их же ищет 3d.html.
ВЫРАЖЕНИЯ = ["happy", "angry", "sad", "relaxed", "surprised", "neutral",
             "blink", "aa", "ih", "ou", "ee", "oh"]

# Где какая кость стоит: не анатомия, а лишь бы взаимное расположение было
# осмысленным (голова выше таза), иначе камера смотрит не туда.
ГДЕ = {
    "hips": (0.0, 1.0, 0.0), "spine": (0.0, 0.2, 0.0), "head": (0.0, 0.35, 0.0),
    "leftUpperArm": (0.18, 0.25, 0.0), "leftLowerArm": (0.0, -0.25, 0.0),
    "leftHand": (0.0, -0.22, 0.0),
    "rightUpperArm": (-0.18, 0.25, 0.0), "rightLowerArm": (0.0, -0.25, 0.0),
    "rightHand": (0.0, -0.22, 0.0),
    "leftUpperLeg": (0.09, -0.05, 0.0), "leftLowerLeg": (0.0, -0.4, 0.0),
    "leftFoot": (0.0, -0.4, 0.0),
    "rightUpperLeg": (-0.09, -0.05, 0.0), "rightLowerLeg": (0.0, -0.4, 0.0),
    "rightFoot": (0.0, -0.4, 0.0),
}
# Кто чей ребёнок: цепочка рук и ног, всё висит на тазе.
РОДИТЕЛЬ = {
    "spine": "hips", "head": "spine",
    "leftUpperArm": "spine", "leftLowerArm": "leftUpperArm", "leftHand": "leftLowerArm",
    "rightUpperArm": "spine", "rightLowerArm": "rightUpperArm", "rightHand": "rightLowerArm",
    "leftUpperLeg": "hips", "leftLowerLeg": "leftUpperLeg", "leftFoot": "leftLowerLeg",
    "rightUpperLeg": "hips", "rightLowerLeg": "rightUpperLeg", "rightFoot": "rightLowerLeg",
}


def _куб() -> tuple[bytes, list, list]:
    """Вершины и треугольники кубика 0.3×0.3×0.3. Отдаёт (двоичное, мин, макс)."""
    р = 0.15
    углы = [(-р, -р, -р), (р, -р, -р), (р, р, -р), (-р, р, -р),
            (-р, -р, р), (р, -р, р), (р, р, р), (-р, р, р)]
    грани = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 4, 5), (0, 5, 1),
             (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    точки = b"".join(struct.pack("<3f", *у) for у in углы)
    номера = b"".join(struct.pack("<3H", *г) for г in грани)
    # Выравнивание по четыре байта — требование формата glb.
    while len(точки) % 4:
        точки += b"\0"
    return точки + номера, [-р, -р, -р], [р, р, р]


def собрать() -> bytes:
    двоичное, мин, макс = _куб()
    смещение_номеров = len(двоичное) - 12 * 3 * 2

    узлы = []
    номер_по_кости = {}
    for кость in ОБЯЗАТЕЛЬНЫЕ_КОСТИ:
        номер_по_кости[кость] = len(узлы)
        узлы.append({"name": кость, "translation": list(ГДЕ[кость])})
    # Дети — после того, как у всех есть номера.
    for кость, родитель in РОДИТЕЛЬ.items():
        узлы[номер_по_кости[родитель]].setdefault("children", []).append(номер_по_кости[кость])
    # Кубик — телом на груди, чтобы его было видно в кадре.
    узлы.append({"name": "тело", "mesh": 0, "translation": [0.0, 0.15, 0.0]})
    узлы[номер_по_кости["spine"]].setdefault("children", []).append(len(узлы) - 1)

    gltf = {
        "asset": {"version": "2.0", "generator": "robot-ai vrm_проба"},
        "extensionsUsed": ["VRMC_vrm"],
        "scene": 0,
        "scenes": [{"nodes": [номер_по_кости["hips"]]}],
        "nodes": узлы,
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1,
                                    "material": 0}]}],
        # Светится само: так кубик виден в кадре независимо от света и от
        # того, посчитались ли нормали, — проверке нужны пиксели, а не красота.
        "materials": [{"name": "зелёный",
                       "pbrMetallicRoughness": {"baseColorFactor": [0.2, 0.85, 0.4, 1.0]},
                       "emissiveFactor": [0.2, 0.85, 0.4]}],
        "buffers": [{"byteLength": len(двоичное)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": смещение_номеров, "target": 34962},
            {"buffer": 0, "byteOffset": смещение_номеров,
             "byteLength": len(двоичное) - смещение_номеров, "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3",
             "min": мин, "max": макс},
            {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
        "extensions": {"VRMC_vrm": {
            "specVersion": "1.0",
            "meta": {"name": "Проба", "version": "1", "authors": ["robot-ai"],
                     "licenseUrl": "https://vrm.dev/licenses/1.0/",
                     "avatarPermission": "everyone", "commercialUsage": "personalNonProfit",
                     "creditNotation": "unnecessary", "allowRedistribution": True,
                     "modification": "allowModification"},
            "humanoid": {"humanBones": {к: {"node": н} for к, н in номер_по_кости.items()}},
            "expressions": {"preset": {и: {"isBinary": False} for и in ВЫРАЖЕНИЯ}},
            "lookAt": {"type": "bone"},
            "firstPerson": {"meshAnnotations": []},
        }},
    }
    json_кусок = json.dumps(gltf, ensure_ascii=False).encode("utf-8")
    while len(json_кусок) % 4:
        json_кусок += b" "
    длина = 12 + 8 + len(json_кусок) + 8 + len(двоичное)
    return (struct.pack("<III", 0x46546C67, 2, длина)
            + struct.pack("<II", len(json_кусок), 0x4E4F534A) + json_кусок
            + struct.pack("<II", len(двоичное), 0x004E4942) + двоичное)


def main() -> int:
    куда = Path(sys.argv[1] if len(sys.argv) > 1 else "проба.vrm")
    куда.parent.mkdir(parents=True, exist_ok=True)
    куда.write_bytes(собрать())
    print(f"{куда} — {куда.stat().st_size} байт")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
