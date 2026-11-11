"""Защита от тихой поломки интерфейса: скрипты сайта (app-*.js и app.js) делят одну область видимости, и две функции с одним именем
значат, что работает только последняя. А файл, не подключённый в index.html, просто не загрузится."""
import re
from collections import Counter
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "app" / "web"


def page_scripts() -> list[Path]:
    """Скрипты в том порядке, в котором их грузит страница."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return [WEB / name for name in re.findall(r'<script src="/static/(app[\w-]*\.js)"', html)]


def test_no_two_top_level_functions_share_a_name():
    names = [n for path in page_scripts() for n in re.findall(r"^(?:async )?function (\w+)", path.read_text(encoding="utf-8"), re.M)]
    assert [n for n, count in Counter(names).items() if count > 1] == []


def test_every_script_file_is_loaded_by_the_page_and_app_js_starts_last():
    loaded = page_scripts()
    assert set(loaded) == set(WEB.glob("app*.js")) and loaded[-1].name == "app.js"
    assert re.search(r"^start\(\);", loaded[-1].read_text(encoding="utf-8"), re.M)
