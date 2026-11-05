from app.creative import refs


def test_yoga_studio_gets_neutral_references_not_pets():
    found = refs.suggest("Студия йоги «Лотос» Йога-студия хатха виньяса", limit=12, focus="Студия йоги «Лотос» Йога-студия", wide=True)
    assert found and all(refs.REFS[n][0] != "pets" for n in found)
    unknown = refs.suggest("ООО Рога и копыта", limit=12, wide=True)
    assert unknown == list(refs.GENERIC_REFS) and all(refs.REFS[n][0] != "pets" for n in unknown)   # универсальные приёмы не про животных
    assert all(refs.REFS[n][0] == "pets" for n in refs.suggest("Груминг-салон для собак", limit=6, focus="Груминг-салон"))   # а у груминга свои