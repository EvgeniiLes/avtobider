"""Задаёт значения в .env: python3 apply_env.py файл_с_парами.txt [путь/к/.env]

Файл с парами: строки вида ИМЯ=значение. Старые строки с теми же именами заменяются,
битые строки без имени удаляются. Значения нигде не печатаются.
"""
import os
import re
import sys

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def read_pairs(path: str) -> dict[str, str]:
    pairs = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line.strip() or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not KEY_RE.match(key):
                sys.exit(f"Недопустимое имя настройки: {key!r}")
            pairs[key] = value.strip()
    return pairs


def apply(pairs: dict[str, str], env_path: str) -> list[str]:
    kept = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for raw in f.read().splitlines():
                key = raw.partition("=")[0].strip()
                if raw.startswith("=") or (key in pairs):
                    continue  # битая строка или заменяемая настройка
                kept.append(raw)
    # Склеенные строки вида ИМЯ=...nДРУГОЕ_ИМЯ=... (след прошлой ошибки): убираем целиком.
    kept = [line for line in kept if not any(line.partition("=")[2].count(f"{k}=") for k in pairs)]
    kept += [f"{k}={v}" for k, v in pairs.items()]
    tmp = env_path + ".tmp"
    old = os.stat(env_path) if os.path.exists(env_path) else None
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(kept) + "\n")
    os.chmod(tmp, (old.st_mode & 0o777) if old else 0o640)
    if old and hasattr(os, "chown"):
        os.chown(tmp, old.st_uid, old.st_gid)  # владелец остаётся прежним, иначе служба не прочитает файл
    os.replace(tmp, env_path)
    return sorted(pairs)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    names = apply(read_pairs(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "/opt/autobider/.env")
    print("Обновлены настройки: " + ", ".join(names))
