"""Ниша «Дизайн-студии и креативные агентства»: свои приёмы, свои слова, соседи по подаче."""
import re

from app.creative import refs

DESIGN_KIND = "IND Design Studio Дизайн-студия Делаем айдентику, полиграфию, баннеры и креативы для малого бизнеса"


def niches(found):
    return {refs.REFS[n][0] for n in found}


def test_design_studio_gets_its_own_references_and_every_file_exists():
    found = refs.suggest(DESIGN_KIND, limit=12, focus="IND Design Studio Дизайн-студия")
    assert niches(found) == {"design"} and found == [94, 95, 96, 97, 98, 99, 100]
    assert all(refs.load(n) for n in found)


def test_other_kinds_of_design_keep_their_own_niches():
    assert niches(refs.suggest("Студия дизайна ногтей Маникюр, дизайн ногтей", focus="Студия дизайна ногтей Маникюр")) == {"nails"}
    assert niches(refs.suggest("Студия интерьера Дизайн интерьера, ремонт под ключ", focus="Студия интерьера Дизайн интерьера")) <= {"furniture", "repair"}
    assert niches(refs.suggest("Разработка сайтов и маркетинг для бизнеса", focus="Разработка сайтов")) == {"it"}


def test_wide_search_takes_neighbours_after_own_references():
    wide = refs.suggest(DESIGN_KIND, limit=12, focus="IND Design Studio Дизайн-студия", wide=True)
    assert wide[:7] == [94, 95, 96, 97, 98, 99, 100] and len(wide) == 12 and niches(wide[7:]) <= {"it", "furniture", "edu", "apps"} | {refs.REFS[n][0] for n in refs.GENERIC_REFS}
    assert re.search(refs.NICHE_WORDS["design"], "айдентика")
