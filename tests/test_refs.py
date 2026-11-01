from app.creative import refs

NNS_KIND = "NotNeuroSlop Агентство по внедрению ИИ и автоматизации бизнеса"
NNS_TEXT = (NNS_KIND + " Находим рутину, собираем ИИ-агентов и автоматизации. ИИ-политика и обучение команды. Интеграции через n8n, Make и API. "
            "Отвечают клиентам в Telegram, считают стоимость, передают в CRM.")


def niches(found):
    return {refs.REFS[n][0] for n in found}


def test_an_ai_agency_gets_it_references_even_though_its_description_mentions_training():
    found = refs.suggest(NNS_TEXT, limit=12, focus=NNS_KIND)
    assert niches(found) == {"it"} and len(found) >= 5                  # раньше побеждало слово «обучение» и шли школьные референсы
    assert niches(refs.suggest(NNS_TEXT, limit=12)) == {"it"}           # и без названия отдельно: слов про ИИ и автоматизацию в тексте больше, чем про обучение


def test_the_kind_of_business_outweighs_passing_mentions_in_the_description():
    text = "Барбершоп Усы Мужские стрижки, оформление бороды. Для мастеров проводим курсы и обучение, есть школа барберов."
    assert niches(refs.suggest(text, focus="Барбершоп Усы")) == {"hair"}
    assert niches(refs.suggest("Лотос Маникюрный салон Маникюр и педикюр в центре города", focus="Лотос Маникюрный салон")) == {"nails"}


def test_unknown_business_gets_the_universal_references_and_ties_keep_the_old_order():
    assert refs.suggest("Лавка древностей Продаём антиквариат") == list(refs.GENERIC_REFS)
    assert niches(refs.suggest("груминг и маникюр")) == {"pets"}         # поровну: первая в списке ниша, как раньше
