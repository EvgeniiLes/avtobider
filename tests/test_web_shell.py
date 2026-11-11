"""Оболочка сайта: каждый элемент, к которому обращается app.js, есть в index.html, а прежние версии доступны."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

WEB = Path(__file__).resolve().parent.parent / "app" / "web"


def _html_ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def test_every_element_script_looks_up_exists_in_page():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    script = "\n".join(p.read_text(encoding="utf-8") for p in sorted(WEB.glob("app*.js")))
    wanted = set(re.findall(r'\$\("([\w-]+)"\)', script)) | set(re.findall(r'getElementById\("([\w-]+)"\)', script))
    wanted |= {f"nav-{p}" for p in re.search(r"const PAGES = \[([^\]]+)\]", script).group(1).replace('"', "").replace(" ", "").split(",")}
    wanted |= {f"page-{p}" for p in re.search(r"const PAGES = \[([^\]]+)\]", script).group(1).replace('"', "").replace(" ", "").split(",")}
    missing = sorted(wanted - _html_ids(html))
    assert not missing, f"в index.html нет элементов, которые ищет app.js: {missing}"


def test_page_has_no_inline_styles_that_security_policy_blocks():
    assert 'style="' not in (WEB / "index.html").read_text(encoding="utf-8")


def test_fonts_and_example_images_are_served_from_our_server():
    with TestClient(app) as http:
        for path in ("/static/fonts/golos-cyrillic.woff2", "/static/fonts/golos-latin.woff2", "/static/img/b1.jpg", "/static/img/p2.jpg"):
            assert http.get(path).status_code == 200, path
        css = http.get("/static/style.css").text
        assert "fonts.googleapis" not in css and "/static/fonts/golos-cyrillic.woff2" in css


def test_the_site_is_served_from_one_place_and_old_versions_are_gone():
    with TestClient(app) as http:
        assert "/static/app.js" in http.get("/").text
        assert http.get("/old").status_code == 404 and http.get("/old2").status_code == 404


def test_guest_home_has_one_example_and_the_form():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for needle in ("Пример: небольшой сервис", 'id="auth-form"', 'id="tab-register"', 'id="tab-login"', 'id="yandex-box"'):
        assert needle in html, needle
