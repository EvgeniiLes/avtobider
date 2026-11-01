"""Защита от тихой поломки интерфейса: две функции с одним именем в app.js, и работает только последняя."""
import re
from collections import Counter
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "app" / "web" / "app.js"


def test_no_two_top_level_functions_share_a_name():
    names = re.findall(r"^(?:async )?function (\w+)", SCRIPT.read_text(encoding="utf-8"), re.M)
    assert [n for n, count in Counter(names).items() if count > 1] == []
