"""Собирает макеты нового интерфейса (два направления и схема разделов) в самодостаточные html-файлы.
Запуск: python docs/mockups/build.py. Картинки вшиваются прямо в файл, шрифты берутся из Google Fonts только для макета."""
import base64
from pathlib import Path

HERE = Path(__file__).parent
IMG = {n: "data:image/jpeg;base64," + base64.b64encode((HERE / "img" / f"{n}.jpg").read_bytes()).decode() for n in
       ("b1", "b2", "b3", "b4", "b5", "p1", "p2")}

ADS = [  # (картинка, заголовок, статус, класс статуса, показы, клики, ctr, заявки)
    ("b1", "Груминг собак и кошек. Записаться за минуту", "Идёт", "ok", "4 120", "96", "2,3 %", "4"),
    ("b2", "Груминг без седативных: бережно и спокойно", "Идёт", "ok", "3 010", "61", "2,0 %", "3"),
    ("b4", "Грумеры со стажем от 5 лет в Москве", "Идёт", "ok", "2 380", "41", "1,7 %", "2"),
    ("b5", "Груминг собак и кошек, Москва", "Идёт", "ok", "1 766", "13", "0,7 %", "0"),
    ("b3", "Не знаете, какая стрижка нужна питомцу?", "Предлагаем остановить", "warn", "1 204", "3", "0,2 %", "0"),
]


def bars() -> str:
    vals = [9, 12, 11, 14, 18, 16, 10, 13, 17, 19, 22, 21, 18, 14]
    w, h, top = 560, 150, 24
    out = [f'<svg class="chart" viewBox="0 0 {w + 34} {h + 26}" role="img" aria-label="Клики по дням за 14 дней">']
    for t in (0, 10, 20):
        y = h - t / top * h + 4
        out.append(f'<line class="grid" x1="34" x2="{w + 34}" y1="{y:.1f}" y2="{y:.1f}"/><text class="ax" x="28" y="{y + 4:.1f}" text-anchor="end">{t}</text>')
    step = w / len(vals)
    for i, v in enumerate(vals):
        bh = v / top * h
        out.append(f'<rect class="bar" x="{34 + i * step + 5:.1f}" y="{h - bh + 4:.1f}" width="{step - 10:.1f}" height="{bh:.1f}" rx="1.5"/>')
        if i in (0, 6, 13):
            out.append(f'<text class="ax" x="{34 + i * step + step / 2:.1f}" y="{h + 22}" text-anchor="middle">{17 + i} окт.</text>')
    out.append("</svg>")
    return "".join(out)


def ad_rows() -> str:
    return "".join(
        f'<tr><td class="thumb"><img src="{IMG[i]}" alt=""></td><td class="ttl">{t}</td><td><span class="st {c}">{s}</span></td>'
        f'<td class="n">{a}</td><td class="n">{b}</td><td class="n">{d}</td><td class="n">{e}</td></tr>'
        for i, t, s, c, a, b, d, e in ADS)


SCREENS = """
<section class="screen" data-screen="overview">
  <header class="page-head"><h1>Обзор</h1><p class="meta">Груминг «Хвост и Лапы» · последние 14 дней</p></header>
  <div class="stats">
    <div class="stat"><span class="k">Показы</span><span class="v">12 480</span><span class="d">было 9 110</span></div>
    <div class="stat"><span class="k">Клики</span><span class="v">214</span><span class="d">было 171</span></div>
    <div class="stat"><span class="k">Цена клика</span><span class="v">18 ₽</span><span class="d">было 22 ₽</span></div>
    <div class="stat"><span class="k">Заявки</span><span class="v">9</span><span class="d">было 6</span></div>
  </div>
  <div class="pair">
    <div class="panel"><h2>Клики по дням</h2>@@BARS@@</div>
    <div class="panel"><h2>Бюджет недели</h2>
      <p class="big">3 240 <span>из 5 000 ₽</span></p>
      <div class="bullet" role="img" aria-label="Потрачено 65 процентов, по плану на этот день 71"><i style="width:65%"></i><b style="left:71%"></b></div>
      <p class="note">Тратим чуть медленнее плана: остаток лучше не копить, а отдать удачным объявлениям.</p>
    </div>
  </div>
  <div class="panel"><h2>Ждёт вашего решения <span class="count">2</span></h2>
    <ul class="queue">
      <li><div><b>Остановить «Не знаете, какая стрижка нужна питомцу?»</b><span>9 дней, 1 204 показа и 3 клика. Освободим бюджет для остальных.</span></div>
        <div class="actions"><button class="btn">Остановить</button><button class="btn ghost">Оставить</button></div></li>
      <li><div><b>Пять новых идей для проверки</b><span>Стратег предложил подачи по вашим фото и удачным приёмам из ниши.</span></div>
        <div class="actions"><button class="btn ghost">Посмотреть идеи</button></div></li>
    </ul>
  </div>
  <div class="panel flush"><h2 class="pad">Объявления</h2>
    <table class="tbl"><thead><tr><th></th><th>Объявление</th><th>Статус</th><th class="n">Показы</th><th class="n">Клики</th><th class="n">CTR</th><th class="n">Заявки</th></tr></thead>
    <tbody>@@ROWS@@</tbody></table></div>
</section>

<section class="screen" data-screen="ads" hidden>
  <header class="page-head"><h1>Объявления</h1><p class="meta">5 объявлений, баннеры готовы у всех</p></header>
  <ol class="steps"><li class="done"><b>Идеи</b><span>5 предложено, 3 в рекламе</span></li><li class="cur"><b>Баннеры</b><span>готовы 5 из 5</span></li><li><b>Запуск</b><span>проверка и отправка</span></li></ol>
  <div class="editor">
    <ul class="adlist">
      <li class="sel"><img src="@@b1@@" alt=""><span>Груминг собак и кошек. Записаться за минуту<em>Готов</em></span></li>
      <li><img src="@@b2@@" alt=""><span>Груминг без седативных: бережно и спокойно<em>Готов</em></span></li>
      <li><img src="@@b4@@" alt=""><span>Грумеры со стажем от 5 лет в Москве<em>Готов</em></span></li>
      <li><img src="@@b5@@" alt=""><span>Груминг собак и кошек, Москва<em>Готов</em></span></li>
      <li><img src="@@b3@@" alt=""><span>Не знаете, какая стрижка нужна питомцу?<em>Готов</em></span></li>
    </ul>
    <div class="edit-main">
      <div class="preview"><img src="@@b1@@" alt="Баннер"><p class="vers">Версии <img src="@@b1@@" class="cur" alt=""><img src="@@b3@@" alt=""><img src="@@b5@@" alt=""></p></div>
      <div class="fields">
        <label>Заголовок на баннере<textarea rows="2">Груминг собак и кошек. Записаться за минуту</textarea><small>43 из 56</small></label>
        <label>Текст объявления<textarea rows="3">Работаем 7 дней в неделю с 9 до 21. Любые породы. Профессиональная косметика</textarea><small>76 из 81</small></label>
        <div class="group"><span class="gl">Фото</span><div class="photos"><img class="sel" src="@@p1@@" alt=""><img src="@@p2@@" alt=""></div></div>
        <div class="group"><span class="gl">Подача</span>
          <div class="chips"><button>Светлое</button><button class="sel">Яркое</button><button>Тёмное</button><button>Пастельное</button><button>Фото во весь баннер</button></div>
          <p class="gl2">По мотивам удачных объявлений ниши</p>
          <div class="chips"><button>Питомцы из-за белой плашки</button><button>Лапа в руке, огромный заголовок</button><button>Кот у миски, цена</button></div></div>
        <div class="row"><button class="btn">Нарисовать заново · $0,07</button><button class="btn ghost">Другой текст</button><a class="link" href="#">Убрать из рекламы</a></div>
        <p class="note">Текст на баннере проверяется автоматически: заголовок, кнопка и цена должны совпасть слово в слово.</p>
      </div>
    </div>
  </div>
</section>

<section class="screen" data-screen="results" hidden>
  <header class="page-head"><h1>Результаты</h1><p class="meta">17–30 октября · сравнение с предыдущими 14 днями</p></header>
  <div class="panel flush"><h2 class="pad">Что работает лучше</h2>
    <p class="hint pad">Сравниваем только когда хватает данных: меньше 500 показов результат не показываем.</p>
    <ul class="rank">
      <li><span class="nm">Скорость записи</span><span class="vl">2,3 %</span><i style="width:100%"></i></li>
      <li><span class="nm">Без стресса для питомца</span><span class="vl">2,0 %</span><i style="width:87%"></i></li>
      <li><span class="nm">Опыт мастеров</span><span class="vl">1,7 %</span><i style="width:74%"></i></li>
      <li><span class="nm">Район и адрес</span><span class="vl">0,7 %</span><i style="width:30%"></i></li>
      <li class="few"><span class="nm">Выбор стрижки</span><span class="vl">мало данных</span><i style="width:0"></i></li>
    </ul></div>
  <div class="pair">
    <div class="panel"><h2>По типу фото</h2><table class="tbl compact"><thead><tr><th>Фото</th><th class="n">Показы</th><th class="n">CTR</th></tr></thead>
      <tbody><tr><td>Работа мастера</td><td class="n">7 130</td><td class="n">2,2 %</td></tr><tr><td>Интерьер</td><td class="n">3 970</td><td class="n">1,2 %</td></tr><tr><td>Команда</td><td class="n">1 380</td><td class="n">0,9 %</td></tr></tbody></table></div>
    <div class="panel"><h2>История изменений</h2><ul class="log">
      <li><time>29 окт., 21:08</time>Баннер к «Груминг без седативных» заменён</li>
      <li><time>29 окт., 20:00</time>Отправлено на проверку 5 объявлений</li>
      <li><time>28 окт., 10:14</time>Подключён кабинет Яндекс Директа</li></ul></div>
  </div>
</section>

<section class="screen" data-screen="media" hidden>
  <header class="page-head"><h1>Материалы</h1><p class="meta">Из них сервис собирает баннеры и тексты</p></header>
  <div class="panel"><h2>Фото бизнеса <span class="count">13</span></h2>
    <div class="gallery"><figure><img src="@@p1@@" alt=""><figcaption>Работа мастера</figcaption></figure><figure><img src="@@p2@@" alt=""><figcaption>Интерьер</figcaption></figure>
    <button class="drop">Добавить фото</button></div></div>
  <div class="pair">
    <div class="panel"><h2>Логотип</h2><p class="empty">Пока не загружен. Если он есть, добавьте файл: сервис сам решит, где его показать, и по желанию уберёт фон.</p><button class="btn ghost">Загрузить логотип</button></div>
    <div class="panel"><h2>Скриншот сайта или приложения</h2><p class="empty">Нужен, если реклама ведёт на сайт или в приложение. Для салона можно пропустить.</p><button class="btn ghost">Загрузить скриншот</button></div>
  </div>
  <div class="panel"><h2>История баннеров <span class="count">12</span></h2>
    <div class="gallery wide"><img src="@@b1@@" alt=""><img src="@@b2@@" alt=""><img src="@@b3@@" alt=""><img src="@@b4@@" alt=""><img src="@@b5@@" alt=""></div>
    <p class="note">Любой прежний баннер можно вернуть на объявление бесплатно.</p></div>
</section>
"""

NAV_SCRIPT = """<script>
(function(){var links=document.querySelectorAll('[data-go]'),screens=document.querySelectorAll('.screen');
function show(id){screens.forEach(function(s){s.hidden=s.dataset.screen!==id});links.forEach(function(l){l.classList.toggle('on',l.dataset.go===id)});window.scrollTo(0,0)}
links.forEach(function(l){l.addEventListener('click',function(e){e.preventDefault();show(l.dataset.go);history.replaceState(null,'','#'+l.dataset.go)})});
show((location.hash||'#overview').slice(1)||'overview')})();
</script>"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={fam}&display=swap">')

# ---------- Направление A: рабочая панель с боковым меню ----------
CSS_A = """
:root{--bg:#f2f3f5;--surface:#fff;--ink:#14171c;--muted:#5d6672;--line:#dfe2e7;--line-2:#eceef1;--accent:#1f4de0;--accent-ink:#fff;--accent-soft:#e9eefe;
--ok:#14703f;--ok-soft:#e4f3ea;--warn:#965a08;--warn-soft:#fbf0dc;--r:6px;--font:"Golos Text",system-ui,sans-serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font);font-variant-numeric:tabular-nums}
.shell{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}
.side{background:var(--surface);border-right:1px solid var(--line);padding:20px 14px;display:flex;flex-direction:column;gap:22px;position:sticky;top:0;height:100vh}
.brand{font-weight:600;font-size:17px;letter-spacing:-.01em;padding:0 8px}
.biz{border:1px solid var(--line);border-radius:var(--r);padding:8px 10px;display:flex;justify-content:space-between;align-items:center;font-weight:500}
.biz span{color:var(--muted);font-weight:400;font-size:13px}
.side nav{display:flex;flex-direction:column;gap:2px}
.side .grp{font-size:12px;color:var(--muted);padding:12px 10px 4px}
.side a{display:flex;justify-content:space-between;color:var(--ink);text-decoration:none;padding:7px 10px;border-radius:var(--r)}
.side a:hover{background:var(--bg)}.side a.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.side a em{font-style:normal;color:var(--muted);font-weight:400}
.limit{margin-top:auto;font-size:13px;color:var(--muted);padding:0 8px}.limit i{display:block;height:4px;background:var(--line);border-radius:2px;margin-top:6px}.limit i b{display:block;height:100%;width:23%;background:var(--ink);border-radius:2px}
main{padding:30px 40px 64px;max-width:1180px;width:100%}
.page-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin-bottom:22px;flex-wrap:wrap}
h1{font-size:24px;font-weight:600;letter-spacing:-.015em;margin:0}.meta{margin:0;color:var(--muted);font-size:14px}
h2{font-size:15px;font-weight:600;margin:0 0 14px;display:flex;gap:8px;align-items:center}
.count{font-weight:500;color:var(--muted);font-size:13px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;margin-bottom:16px}
.panel.flush{padding:18px 0 0;overflow:hidden}.pad{padding:0 20px}.panel .hint.pad{margin:-6px 0 12px;color:var(--muted);font-size:13px}
.pair{display:grid;grid-template-columns:1.6fr 1fr;gap:16px}.pair>.panel{margin-bottom:16px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);border-radius:var(--r);background:var(--surface);margin-bottom:16px}
.stat{padding:16px 20px;display:flex;flex-direction:column;gap:2px}.stat+.stat{border-left:1px solid var(--line)}
.k{color:var(--muted);font-size:13px}.v{font-size:30px;font-weight:600;letter-spacing:-.02em;line-height:1.2}.d{color:var(--muted);font-size:13px}
.chart{width:100%;height:auto}.chart .grid{stroke:var(--line-2)}.chart .ax{fill:var(--muted);font-size:11px;font-family:var(--font)}.chart .bar{fill:var(--ink)}
.big{font-size:28px;font-weight:600;margin:0 0 12px;letter-spacing:-.02em}.big span{font-size:14px;font-weight:400;color:var(--muted)}
.bullet{position:relative;height:10px;background:var(--line-2);border-radius:2px}.bullet i{position:absolute;inset:0 auto 0 0;background:var(--ink);border-radius:2px}.bullet b{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--accent)}
.note{color:var(--muted);font-size:13px;margin:12px 0 0}
.queue{list-style:none;margin:0;padding:0}.queue li{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:14px 0;border-top:1px solid var(--line-2)}.queue li:first-child{border-top:0;padding-top:0}
.queue b{display:block;font-weight:600}.queue span{color:var(--muted);font-size:14px}.actions{display:flex;gap:8px;flex-shrink:0}
.btn{font:inherit;font-weight:500;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);border-radius:5px;padding:8px 14px;cursor:pointer}
.btn:hover{filter:brightness(.94)}.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}.btn.ghost:hover{background:var(--bg)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tbl{width:100%;border-collapse:collapse}.tbl th{font-weight:500;color:var(--muted);font-size:13px;text-align:left;padding:8px 12px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--bg)}
.tbl td{padding:10px 12px;border-bottom:1px solid var(--line-2);vertical-align:middle}.tbl tr:last-child td{border-bottom:0}.n{text-align:right}
.tbl th:first-child,.tbl td:first-child{padding-left:20px}.tbl th:last-child,.tbl td:last-child{padding-right:20px}
.thumb{width:76px}.thumb img{width:56px;height:42px;object-fit:cover;border-radius:3px;display:block}.ttl{max-width:340px}
.st{font-size:13px;font-weight:500}.st.ok{color:var(--ok)}.st.warn{color:var(--warn)}
.tbl.compact th,.tbl.compact td{padding-left:0;padding-right:12px}.tbl.compact th{background:none;border-top:0}
.steps{display:flex;gap:0;list-style:none;margin:0 0 16px;padding:0;border:1px solid var(--line);border-radius:var(--r);background:var(--surface);overflow:hidden}
.steps li{flex:1;padding:12px 18px;display:flex;flex-direction:column}.steps li+li{border-left:1px solid var(--line)}.steps b{font-weight:600}.steps span{font-size:13px;color:var(--muted)}
.steps .cur{box-shadow:inset 0 -2px 0 var(--accent)}.steps .cur b{color:var(--accent)}
.editor{display:grid;grid-template-columns:288px 1fr;gap:16px}
.adlist{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:var(--r);align-self:start}
.adlist li{display:flex;gap:10px;padding:10px;border-top:1px solid var(--line-2);cursor:pointer}.adlist li:first-child{border-top:0}.adlist li.sel{background:var(--accent-soft)}
.adlist img{width:64px;height:48px;object-fit:cover;border-radius:3px;flex-shrink:0}.adlist span{font-size:13.5px;line-height:1.35;display:flex;flex-direction:column;gap:2px}.adlist em{font-style:normal;font-size:12px;color:var(--ok)}
.edit-main{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:20px;display:grid;grid-template-columns:minmax(280px,360px) 1fr;gap:24px;align-items:start}
.preview img{width:100%;border-radius:4px;display:block;border:1px solid var(--line)}.vers{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);margin:10px 0 0}
.vers img{width:56px;height:42px;object-fit:cover;border-radius:3px;border:1px solid var(--line);opacity:.8}.vers img.cur{opacity:1;outline:2px solid var(--ink);outline-offset:1px}
.fields{display:flex;flex-direction:column;gap:16px}label{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--muted)}
textarea{font:inherit;color:var(--ink);padding:9px 11px;border:1px solid var(--line);border-radius:5px;background:var(--surface);resize:vertical;width:100%}textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}small{color:var(--muted);font-size:12px}
.group{display:flex;flex-direction:column;gap:8px}.gl{font-size:13px;color:var(--muted)}.gl2{margin:6px 0 0;font-size:13px;color:var(--muted)}
.photos{display:flex;gap:8px}.photos img{width:72px;height:54px;object-fit:cover;border-radius:3px;border:1px solid var(--line);opacity:.75}.photos img.sel{opacity:1;outline:2px solid var(--accent);outline-offset:1px}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chips button{font:inherit;font-size:13px;background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:5px 10px;cursor:pointer;color:var(--ink)}
.chips button.sel{border-color:var(--ink);background:var(--bg);font-weight:600}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.link{color:var(--muted);font-size:14px}
.rank{list-style:none;margin:0;padding:0}.rank li{position:relative;display:flex;justify-content:space-between;padding:12px 20px;border-top:1px solid var(--line-2)}
.rank i{position:absolute;left:0;bottom:0;height:3px;background:var(--ink)}.rank .few{color:var(--muted)}.rank .vl{font-weight:600}.rank .few .vl{font-weight:400}
.log{list-style:none;margin:0;padding:0;font-size:14px}.log li{padding:9px 0;border-top:1px solid var(--line-2)}.log li:first-child{border-top:0;padding-top:0}.log time{display:block;font-size:12px;color:var(--muted)}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}.gallery figure{margin:0}.gallery img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:4px;display:block;border:1px solid var(--line)}
.gallery figcaption{font-size:12.5px;color:var(--muted);margin-top:4px}.gallery.wide{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
.drop{font:inherit;color:var(--muted);border:1px dashed var(--line);background:none;border-radius:4px;min-height:120px;cursor:pointer}.empty{margin:0 0 12px;color:var(--muted);font-size:14px}
@media(max-width:900px){.shell{grid-template-columns:minmax(0,1fr)}.side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;padding:12px;gap:8px 14px;border-right:0;border-bottom:1px solid var(--line)}
.side nav{flex-direction:row;flex-wrap:wrap}.side .grp,.limit{display:none}.biz{margin-left:auto}main{padding:20px 16px 48px}
.stats{grid-template-columns:1fr 1fr}.stat:nth-child(3){border-left:0}.stat:nth-child(n+3){border-top:1px solid var(--line)}.pair,.editor,.edit-main{grid-template-columns:1fr}
.queue li{flex-direction:column;align-items:flex-start}.tbl{display:block;overflow-x:auto}.steps{flex-direction:column}.steps li+li{border-left:0;border-top:1px solid var(--line)}}
"""

SHELL_A = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Макет A: рабочая панель</title>
@@FONTS@@<style>@@CSS@@</style></head><body><div class="shell">
<aside class="side"><div class="brand">Автобидер</div>
<div class="biz">Груминг «Хвост и Лапы»<span>сменить</span></div>
<nav><span class="grp">Реклама</span><a href="#" data-go="overview">Обзор</a><a href="#" data-go="ads">Объявления <em>5</em></a><a href="#" data-go="results">Результаты</a>
<span class="grp">Материалы</span><a href="#" data-go="media">Фото и баннеры <em>13</em></a>
<span class="grp">Настройки</span><a href="#">Бизнес</a><a href="#">Подключения</a></nav>
<div class="limit">Генерация: $1,17 из $5 в этом месяце<i><b></b></i></div></aside>
<main>@@SCREENS@@</main></div>@@SCRIPT@@</body></html>"""

# ---------- Направление B: издание с верхним меню, без рамок ----------
CSS_B = """
:root{--bg:#ffffff;--ink:#16181d;--muted:#656b75;--line:#d9dbe0;--line-2:#ececef;--accent:#0f5a46;--accent-ink:#fff;--accent-soft:#e6f1ec;--tint:#f6f6f4;
--ok:#0f5a46;--warn:#8a5200;--r:3px;--serif:"Literata",Georgia,serif;--font:"PT Sans",system-ui,sans-serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 var(--font);font-variant-numeric:lining-nums tabular-nums}
.mast{border-bottom:2px solid var(--ink);padding:0 32px}.mast-in{max-width:1040px;margin:0 auto;display:flex;align-items:baseline;gap:36px;padding:18px 0 0;flex-wrap:wrap}
.brand{font:600 22px var(--serif);letter-spacing:-.01em;margin-right:8px}
.mast nav{display:flex;gap:26px;flex:1;flex-wrap:wrap}.mast nav a{color:var(--muted);text-decoration:none;padding:6px 0 14px;border-bottom:3px solid transparent;margin-bottom:-2px;font-weight:700}
.mast nav a:hover{color:var(--ink)}.mast nav a.on{color:var(--ink);border-bottom-color:var(--accent)}
.who{font-size:14px;color:var(--muted);padding-bottom:14px}.who b{color:var(--ink)}
main{max-width:1040px;margin:0 auto;padding:40px 32px 80px}
.page-head{margin-bottom:30px}h1{font:600 38px/1.15 var(--serif);font-variant-numeric:lining-nums;letter-spacing:-.02em;margin:0 0 6px}.meta{margin:0;color:var(--muted)}
h2{font:600 20px var(--serif);margin:0 0 14px;display:flex;gap:10px;align-items:baseline;letter-spacing:-.01em}.count{font:400 14px var(--font);color:var(--muted)}
.panel{border-top:1px solid var(--ink);padding:18px 0 8px;margin-bottom:34px}.panel.flush{padding-bottom:0}.pad{padding:0}.panel .hint{margin:-6px 0 14px;color:var(--muted);font-size:15px}
.pair{display:grid;grid-template-columns:1.5fr 1fr;gap:44px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:28px;border-top:1px solid var(--ink);padding-top:18px;margin-bottom:34px}
.stat{display:flex;flex-direction:column}.k{color:var(--muted);font-size:14px}.v{font:600 44px/1.1 var(--serif);letter-spacing:-.03em;margin:4px 0}.d{color:var(--muted);font-size:14px}
.chart{width:100%;height:auto}.chart .grid{stroke:var(--line-2)}.chart .ax{fill:var(--muted);font-size:11px;font-family:var(--font)}.chart .bar{fill:var(--accent)}
.big{font:600 34px var(--serif);margin:0 0 12px;letter-spacing:-.02em}.big span{font:400 15px var(--font);color:var(--muted)}
.bullet{position:relative;height:8px;background:var(--line-2)}.bullet i{position:absolute;inset:0 auto 0 0;background:var(--accent)}.bullet b{position:absolute;top:-5px;bottom:-5px;width:2px;background:var(--ink)}
.note{color:var(--muted);font-size:14.5px;margin:12px 0 0}
.queue{list-style:none;margin:0;padding:0}.queue li{display:flex;justify-content:space-between;gap:24px;align-items:center;padding:16px 0;border-top:1px solid var(--line)}.queue li:first-child{border-top:0;padding-top:4px}
.queue b{display:block;font-size:17px}.queue span{color:var(--muted);font-size:15px}.actions{display:flex;gap:8px;flex-shrink:0}
.btn{font:700 15px var(--font);border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);border-radius:var(--r);padding:9px 16px;cursor:pointer}
.btn:hover{filter:brightness(1.12)}.btn.ghost{background:transparent;color:var(--ink);border-color:var(--ink)}.btn.ghost:hover{background:var(--tint)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.tbl{width:100%;border-collapse:collapse}.tbl th{font-weight:400;color:var(--muted);font-size:14px;text-align:left;padding:8px 12px 8px 0;border-bottom:1px solid var(--ink)}
.tbl td{padding:12px 12px 12px 0;border-bottom:1px solid var(--line-2);vertical-align:middle}.n{text-align:right}.tbl th.n,.tbl td.n{padding-right:0;padding-left:12px}
.thumb{width:78px}.thumb img{width:62px;height:46px;object-fit:cover;display:block}.ttl{max-width:340px}
.st{font-size:14px;font-weight:700}.st.ok{color:var(--ok)}.st.warn{color:var(--warn)}
.steps{display:flex;list-style:none;margin:0 0 30px;padding:0;counter-reset:s;border-top:1px solid var(--ink)}
.steps li{flex:1;padding:12px 24px 0 0;display:flex;flex-direction:column}.steps b{font:600 18px var(--serif)}.steps span{font-size:14px;color:var(--muted)}
.steps .cur{box-shadow:0 -3px 0 var(--accent);margin-top:-1px}.steps .done b{color:var(--muted)}
.editor{display:grid;grid-template-columns:250px 1fr;gap:36px}
.adlist{list-style:none;margin:0;padding:0}.adlist li{display:flex;gap:10px;padding:10px 0;border-top:1px solid var(--line-2);cursor:pointer}.adlist li.sel{box-shadow:inset 3px 0 0 var(--accent);padding-left:10px;background:var(--tint)}
.adlist img{width:60px;height:45px;object-fit:cover;flex-shrink:0}.adlist span{font-size:14.5px;line-height:1.3;display:flex;flex-direction:column;gap:2px}.adlist em{font-style:normal;font-size:13px;color:var(--ok)}
.edit-main{display:grid;grid-template-columns:minmax(260px,340px) 1fr;gap:32px;align-items:start}
.preview img{width:100%;display:block}.vers{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--muted);margin:10px 0 0}
.vers img{width:54px;height:40px;object-fit:cover;opacity:.7}.vers img.cur{opacity:1;outline:2px solid var(--accent);outline-offset:1px}
.fields{display:flex;flex-direction:column;gap:18px}label{display:flex;flex-direction:column;gap:4px;font-size:14px;color:var(--muted)}
textarea{font:inherit;color:var(--ink);padding:8px 0;border:0;border-bottom:1px solid var(--ink);background:transparent;border-radius:0;resize:vertical;width:100%}textarea:focus{outline:none;border-bottom:3px solid var(--accent);padding-bottom:6px}small{color:var(--muted);font-size:13px}
.group{display:flex;flex-direction:column;gap:8px}.gl{font-size:14px;color:var(--muted)}.gl2{margin:6px 0 0;font-size:14px;color:var(--muted)}
.photos{display:flex;gap:8px}.photos img{width:72px;height:54px;object-fit:cover;opacity:.7}.photos img.sel{opacity:1;outline:3px solid var(--accent);outline-offset:1px}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chips button{font:inherit;font-size:14px;background:transparent;border:1px solid var(--line);border-radius:var(--r);padding:4px 10px;cursor:pointer;color:var(--ink)}
.chips button.sel{border-color:var(--ink);background:var(--ink);color:#fff}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.link{color:var(--muted);font-size:15px}
.rank{list-style:none;margin:0;padding:0}.rank li{position:relative;display:flex;justify-content:space-between;padding:13px 0;border-top:1px solid var(--line-2)}
.rank i{position:absolute;left:0;bottom:-1px;height:4px;background:var(--accent)}.rank .few{color:var(--muted)}.rank .vl{font:600 18px var(--serif)}.rank .few .vl{font:400 15px var(--font)}
.log{list-style:none;margin:0;padding:0;font-size:15px}.log li{padding:10px 0;border-top:1px solid var(--line-2)}.log li:first-child{border-top:0;padding-top:0}.log time{display:block;font-size:13px;color:var(--muted)}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px}.gallery figure{margin:0}.gallery img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block}
.gallery figcaption{font-size:13.5px;color:var(--muted);margin-top:4px}.gallery.wide{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}
.drop{font:inherit;color:var(--muted);border:1px dashed var(--muted);background:none;min-height:120px;cursor:pointer}.empty{margin:0 0 12px;color:var(--muted)}
h1,h2,.v,.big,.rank .vl,.brand{font-variant-numeric:lining-nums tabular-nums}
@media(max-width:860px){.mast{padding:0 16px}.mast-in{gap:6px 20px}.who{display:none}main{padding:26px 16px 56px}h1{font-size:30px}
.stats{grid-template-columns:1fr 1fr}.pair,.editor,.edit-main{grid-template-columns:1fr;gap:24px}.queue li{flex-direction:column;align-items:flex-start}
.tbl{display:block;overflow-x:auto}.steps{flex-direction:column}}
"""

SHELL_B = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Макет B: издание</title>
@@FONTS@@<style>@@CSS@@</style></head><body>
<header class="mast"><div class="mast-in"><span class="brand">Автобидер</span>
<nav><a href="#" data-go="overview">Обзор</a><a href="#" data-go="ads">Объявления</a><a href="#" data-go="results">Результаты</a><a href="#" data-go="media">Материалы</a><a href="#">Настройки</a></nav>
<span class="who"><b>Груминг «Хвост и Лапы»</b> · сменить</span></div></header>
<main>@@SCREENS@@</main>@@SCRIPT@@</body></html>"""


def screens() -> str:
    s = SCREENS.replace("@@BARS@@", bars()).replace("@@ROWS@@", ad_rows())
    for n, uri in IMG.items():
        s = s.replace(f"@@{n}@@", uri)
    return s


def build(name: str, shell: str, css: str, fam: str) -> None:
    html = (shell.replace("@@FONTS@@", FONTS.format(fam=fam)).replace("@@CSS@@", css)
            .replace("@@SCREENS@@", screens()).replace("@@SCRIPT@@", NAV_SCRIPT))
    (HERE / name).write_text(html, encoding="utf-8")
    print(name, len(html) // 1024, "КБ")


build("direction-a.html", SHELL_A, CSS_A, "Golos+Text:wght@400;500;600")
build("direction-b.html", SHELL_B, CSS_B, "Literata:opsz,wght@7..72,400;600&family=PT+Sans:wght@400;700")


# ---------- Направление C: панель A в языке референса (крупный жирный шрифт, пастельные плитки, капсулы) ----------
def bars_c() -> str:
    """Столбики-капсулы на цветной плитке: последний день выделен белым."""
    vals = [9, 12, 11, 14, 18, 16, 10, 13, 17, 19, 22, 21, 18, 14]
    w, h = 330, 120
    step = w / len(vals)
    out = [f'<svg class="chart-c" viewBox="0 0 {w} {h + 22}" role="img" aria-label="Клики по дням за 14 дней">']
    for i, v in enumerate(vals):
        bh = v / 24 * h
        out.append(f'<rect class="{"hi" if i == 10 else ""}" x="{i * step + 2:.1f}" y="{h - bh:.1f}" width="{step - 4:.1f}" height="{bh:.1f}" rx="{(step - 4) / 2:.1f}"/>')
    for i, label in ((0, "17"), (4, "21"), (8, "25"), (13, "30")):
        out.append(f'<text x="{i * step + step / 2:.1f}" y="{h + 17}" text-anchor="middle">{label}</text>')
    out.append("</svg>")
    return "".join(out)


HOME_C = """
<section class="screen" data-screen="overview">
  <div class="hero">
    <div class="hero-l">
      <span class="badge"><i></i>Груминг «Хвост и Лапы» · 17–30 октября</span>
      <h1 class="hero-h">9 заявок за две недели <span class="bub">+3</span></h1>
      <p class="lead">Объявления показали 12 480 раз и привели 214 человек на сайт. Это на 25 % больше, чем в прошлые две недели.</p>
      <div class="hero-cta"><button class="btn">Посмотреть идеи</button><a class="link" href="#" data-go="results">Все результаты</a></div>
      <div class="chiprow">
        <button class="chip">Нарисовать баннер<span class="plus">+</span></button><button class="chip">Новые идеи<span class="plus">+</span></button>
        <button class="chip hot">Остановить слабое<span class="plus">+</span></button><button class="chip">Бюджет<span class="plus">+</span></button><button class="chip">Добавить фото<span class="plus">+</span></button>
      </div>
    </div>
    <div class="tiles">
      <div class="tile mint photo"><img src="@@p2@@" alt="Лучшее фото"><p><b>Лучшее фото</b>Работа мастера: 2,2 % кликов</p></div>
      <div class="tile lav chart"><div class="th"><span>Клики</span><em>+214</em></div>@@BARSC@@</div>
      <div class="tile butter budget"><div class="th"><span>Бюджет недели</span></div><p class="big">3 240 <small>из 5 000 ₽</small></p>
        <div class="cap" role="img" aria-label="Потрачено 65 процентов"><i style="width:65%"></i></div><p class="sm">Идём чуть медленнее плана</p></div>
      <div class="tile pink ideas"><div class="th"><span>Идеи стратега</span><b class="big2">5 новых</b></div>
        <div class="thumbs"><img src="@@b2@@" alt=""><img src="@@b4@@" alt=""><img src="@@b3@@" alt=""></div><button class="circ" aria-label="Открыть идеи">+</button></div>
    </div>
  </div>
  <div class="panel"><h2>Ждёт вашего решения <span class="count">1</span></h2>
    <ul class="queue"><li><div><b>Остановить «Не знаете, какая стрижка нужна питомцу?»</b><span>9 дней, 1 204 показа и 3 клика. Освободим бюджет для остальных.</span></div>
      <div class="actions"><button class="btn">Остановить</button><button class="btn ghost">Оставить</button></div></li></ul></div>
  <div class="panel flush"><h2 class="pad">Объявления</h2>
    <table class="tbl"><thead><tr><th></th><th>Объявление</th><th>Статус</th><th class="n">Показы</th><th class="n">Клики</th><th class="n">CTR</th><th class="n">Заявки</th></tr></thead>
    <tbody>@@ROWS@@</tbody></table></div>
</section>
"""

HOME_NEW = """
<div class="state-new">
  <div class="hero">
    <div class="hero-l">
      <span class="badge"><i></i>Новый бизнес</span>
      <h1 class="hero-h">Запустим первую рекламу</h1>
      <p class="lead">Нужно три шага. Как только реклама пойдёт, здесь появятся ваши показы, клики и заявки.</p>
      <ol class="steps stack">
        <li class="cur"><b>1. Расскажите о бизнесе</b><span>что делаете и где</span></li>
        <li><b>2. Добавьте фото</b><span>из них сделаем баннеры</span></li>
        <li><b>3. Подключите кабинет</b><span>Яндекс Директ</span></li>
      </ol>
      <div class="hero-cta"><button class="btn">Рассказать о бизнесе</button><a class="link" href="#">Показать пример данных</a></div>
    </div>
    <div class="tiles ghost">
      <div class="tile photo"><b>Лучшее фото</b>Появится после первых показов</div>
      <div class="tile"><b>Клики</b>График за две недели</div>
      <div class="tile"><b>Бюджет недели</b>Сколько потрачено и сколько осталось</div>
      <div class="tile pink"><div class="th"><span>Идеи стратега</span><b class="big2">после описания бизнеса</b></div></div>
    </div>
  </div>
</div>
"""
HOME_C = HOME_C.replace('<section class="screen" data-screen="overview">\n', '<section class="screen" data-screen="overview">\n<div class="state-data">\n', 1)
HOME_C = HOME_C.rstrip()[:-len("</section>")] + "</div>\n" + HOME_NEW + "</section>\n"

CSS_C = """
:root{--bg:#fff;--soft:#f5f5f3;--ink:#111214;--muted:#6a6d75;--line:#e7e8ec;--line-2:#f0f0f2;
--mint:#9cecc7;--mint-ink:#0c4630;--lav:#bfaef6;--lav-ink:#2b1f63;--butter:#ffe9a9;--butter-ink:#4d3b06;--pink:#f8cdf1;--pink-ink:#561d4d;
--ok:#0c4630;--warn:#4d3b06;--tile:28px;--font:"Golos Text",system-ui,sans-serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--font);font-variant-numeric:tabular-nums}
.shell{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:100vh}
.side{padding:26px 16px;display:flex;flex-direction:column;gap:22px;position:sticky;top:0;height:100vh;border-right:1px solid var(--line)}
.brand{font-weight:800;font-size:22px;letter-spacing:-.03em;padding:0 10px}
.biz{border:1px solid var(--line);border-radius:999px;padding:9px 16px;display:flex;justify-content:space-between;align-items:center;font-weight:600;font-size:14px;gap:8px}
.biz span{color:var(--muted);font-weight:400;font-size:13px}
.side nav{display:flex;flex-direction:column;gap:3px}.side .grp{font-size:12px;color:var(--muted);padding:14px 14px 5px}
.side a{display:flex;justify-content:space-between;color:var(--ink);text-decoration:none;padding:9px 14px;border-radius:999px;font-weight:500}
.side a:hover{background:var(--soft)}.side a.on{background:var(--mint);color:var(--mint-ink);font-weight:700}
.side a em{font-style:normal;color:var(--muted);font-weight:500;margin-left:8px}.side a.on em{color:var(--mint-ink)}
.limit{margin-top:auto;font-size:13px;color:var(--muted);padding:0 10px}.limit i{display:block;height:8px;background:var(--soft);border-radius:999px;margin-top:8px;overflow:hidden}.limit i b{display:block;height:100%;width:23%;background:var(--ink);border-radius:999px}
main{padding:34px 48px 72px;max-width:1240px;width:100%}
.page-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin-bottom:26px;flex-wrap:wrap}
h1{font-size:36px;font-weight:700;letter-spacing:-.035em;line-height:1.05;margin:0}.meta{margin:0;color:var(--muted);font-size:14px}
h2{font-size:18px;font-weight:700;letter-spacing:-.02em;margin:0 0 16px;display:flex;gap:10px;align-items:center}.count{font-weight:600;color:var(--muted);font-size:14px;background:var(--soft);border-radius:999px;padding:1px 9px}
.panel{border:1px solid var(--line);border-radius:24px;padding:24px 26px;margin-bottom:18px;background:#fff}.panel.flush{padding:24px 0 0;overflow:hidden}.pad{padding:0 26px}.panel .hint.pad{margin:-8px 0 12px;color:var(--muted);font-size:13px}
.pair{display:grid;grid-template-columns:1.6fr 1fr;gap:18px}.pair>.panel{margin-bottom:18px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.stat{padding:18px 22px;display:flex;flex-direction:column;gap:2px;border:1px solid var(--line);border-radius:24px}
.k{color:var(--muted);font-size:13px}.v{font-size:34px;font-weight:700;letter-spacing:-.035em;line-height:1.15}.d{color:var(--muted);font-size:13px}
.chart{width:100%;height:auto}.chart .grid{stroke:var(--line-2)}.chart .ax{fill:var(--muted);font-size:11px;font-family:var(--font)}.chart .bar{fill:var(--lav)}
.big{font-size:40px;font-weight:700;letter-spacing:-.04em;margin:0 0 12px;line-height:1.1}.big span,.big small{font-size:15px;font-weight:500;letter-spacing:0;color:inherit;opacity:.7}
.bullet{position:relative;height:10px;background:var(--soft);border-radius:999px}.bullet i{position:absolute;inset:0 auto 0 0;background:var(--ink);border-radius:999px}.bullet b{position:absolute;top:-4px;bottom:-4px;width:3px;background:var(--lav);border-radius:2px}
.note{color:var(--muted);font-size:13.5px;margin:12px 0 0}
.queue{list-style:none;margin:0;padding:0}.queue li{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:16px 0;border-top:1px solid var(--line-2)}.queue li:first-child{border-top:0;padding-top:0}
.queue b{display:block;font-weight:700}.queue span{color:var(--muted);font-size:14px}.actions{display:flex;gap:8px;flex-shrink:0}
.btn{font:inherit;font-weight:600;border:1px solid var(--ink);background:var(--ink);color:#fff;border-radius:999px;padding:11px 22px;cursor:pointer}
.btn:hover{background:#2a2c31}.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}.btn.ghost:hover{background:var(--soft)}
:focus-visible{outline:3px solid var(--lav);outline-offset:2px}
.tbl{width:100%;border-collapse:collapse}.tbl th{font-weight:500;color:var(--muted);font-size:13px;text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
.tbl td{padding:12px;border-bottom:1px solid var(--line-2);vertical-align:middle}.tbl tr:last-child td{border-bottom:0}.n{text-align:right}
.tbl th:first-child,.tbl td:first-child{padding-left:26px}.tbl th:last-child,.tbl td:last-child{padding-right:26px}
.thumb{width:84px}.thumb img{width:60px;height:45px;object-fit:cover;border-radius:10px;display:block}.ttl{max-width:340px;font-weight:500}
.st{font-size:12.5px;font-weight:700;border-radius:999px;padding:3px 11px;white-space:nowrap}.st.ok{background:var(--mint);color:var(--mint-ink)}.st.warn{background:var(--butter);color:var(--butter-ink)}
.tbl.compact th,.tbl.compact td{padding-left:0;padding-right:12px}
.steps{display:flex;gap:10px;list-style:none;margin:0 0 20px;padding:0;flex-wrap:wrap}
.steps li{padding:10px 22px;border-radius:999px;border:1px solid var(--line);display:flex;gap:10px;align-items:baseline}.steps b{font-weight:700}.steps span{font-size:13px;color:var(--muted)}
.steps .cur{background:var(--mint);border-color:var(--mint);color:var(--mint-ink)}.steps .cur span{color:var(--mint-ink);opacity:.75}.steps .done{background:var(--soft);border-color:var(--soft)}
.editor{display:grid;grid-template-columns:300px minmax(0,1fr);gap:18px}
.adlist{list-style:none;margin:0;padding:8px;border:1px solid var(--line);border-radius:24px;align-self:start}
.adlist li{display:flex;gap:12px;padding:10px;border-radius:18px;cursor:pointer;align-items:center}.adlist li.sel{background:var(--soft);box-shadow:inset 0 0 0 2px var(--ink)}
.adlist img{width:64px;height:48px;object-fit:cover;border-radius:10px;flex-shrink:0}.adlist span{font-size:13.5px;line-height:1.3;display:flex;flex-direction:column;gap:2px;font-weight:500}.adlist em{font-style:normal;font-size:12px;color:var(--ok);font-weight:700}
.edit-main{border:1px solid var(--line);border-radius:24px;padding:24px;display:grid;grid-template-columns:minmax(260px,340px) minmax(0,1fr);gap:26px;align-items:start}
.preview img{width:100%;border-radius:20px;display:block}.vers{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);margin:12px 0 0}
.vers img{width:56px;height:42px;object-fit:cover;border-radius:10px;opacity:.7}.vers img.cur{opacity:1;outline:2px solid var(--ink);outline-offset:2px}
.fields{display:flex;flex-direction:column;gap:18px}label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--muted)}
textarea{font:inherit;color:var(--ink);padding:11px 14px;border:1px solid var(--line);border-radius:16px;background:#fff;resize:vertical;width:100%}textarea:focus{outline:3px solid var(--lav);outline-offset:0;border-color:var(--ink)}small{color:var(--muted);font-size:12px}
.group{display:flex;flex-direction:column;gap:9px}.gl{font-size:13px;color:var(--muted)}.gl2{margin:6px 0 0;font-size:13px;color:var(--muted)}
.photos{display:flex;gap:10px}.photos img{width:76px;height:57px;object-fit:cover;border-radius:14px;opacity:.7}.photos img.sel{opacity:1;outline:3px solid var(--ink);outline-offset:2px}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chips button{font:inherit;font-size:13.5px;font-weight:500;background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 14px;cursor:pointer;color:var(--ink)}
.chips button:hover{background:var(--soft)}.chips button.sel{background:var(--lav);border-color:var(--lav);color:var(--lav-ink);font-weight:700}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.link{color:var(--muted);font-size:14px;font-weight:500}
.rank{list-style:none;margin:0;padding:0}.rank li{position:relative;display:flex;justify-content:space-between;padding:16px 26px 20px;border-top:1px solid var(--line-2)}.rank i{position:absolute;left:26px;bottom:9px;height:7px;border-radius:999px;background:var(--lav);max-width:calc(100% - 52px)}
.rank .few{color:var(--muted)}.rank .vl{font-weight:700;font-size:17px}.rank .few .vl{font-weight:400;font-size:14px}.rank .nm{font-weight:500}
.log{list-style:none;margin:0;padding:0;font-size:14px}.log li{padding:10px 0;border-top:1px solid var(--line-2)}.log li:first-child{border-top:0;padding-top:0}.log time{display:block;font-size:12px;color:var(--muted)}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px}.gallery figure{margin:0}.gallery img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:20px;display:block}
.gallery figcaption{font-size:13px;color:var(--muted);margin-top:6px}.gallery.wide{grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}
.drop{font:inherit;font-weight:500;color:var(--muted);border:1.5px dashed var(--line);background:none;border-radius:20px;min-height:128px;cursor:pointer}.drop:hover{background:var(--soft)}.empty{margin:0 0 14px;color:var(--muted);font-size:14px}
/* главная в духе референса */
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);gap:44px;align-items:center;margin:6px 0 40px}
.badge{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;padding:5px 14px 5px 10px;font-size:13px;font-weight:500;color:var(--muted);margin-bottom:22px}.badge i{width:8px;height:8px;border-radius:50%;background:var(--mint);box-shadow:0 0 0 3px #d9f7e8}
.hero-h{font-size:64px;line-height:1;letter-spacing:-.045em;font-weight:700;margin:0 0 22px}.bub{display:inline-grid;place-items:center;width:46px;height:46px;border-radius:50%;background:var(--pink);color:var(--pink-ink);font-size:20px;letter-spacing:0;vertical-align:middle;margin-left:12px;font-weight:700}
.lead{font-size:16px;line-height:1.5;color:var(--muted);max-width:430px;margin:0 0 26px}
.hero-cta{display:flex;gap:18px;align-items:center;margin-bottom:34px}.hero-cta .btn{padding:14px 28px;font-size:15px}.hero-cta .link{color:var(--ink);text-decoration:underline;text-underline-offset:4px}
.chiprow{display:flex;flex-wrap:wrap;gap:9px;max-width:520px}.chip{font:inherit;font-weight:500;font-size:14px;display:inline-flex;align-items:center;gap:10px;background:#fff;border:1px solid var(--ink);border-radius:999px;padding:6px 6px 6px 18px;cursor:pointer;color:var(--ink)}
.chip .plus{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;border:1px solid var(--ink);font-size:18px;line-height:1;font-weight:400}.chip:hover{background:var(--soft)}.chip.hot{background:var(--mint);border-color:var(--ink)}
.tiles{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:auto auto auto;gap:14px}
.tile{border-radius:var(--tile);padding:20px 20px 22px;display:flex;flex-direction:column;position:relative}
.tile.mint{background:var(--mint);color:var(--mint-ink)}.tile.lav{background:var(--lav);color:var(--lav-ink)}.tile.butter{background:var(--butter);color:var(--butter-ink)}.tile.pink{background:var(--pink);color:var(--pink-ink)}
.th{display:flex;justify-content:space-between;align-items:center;font-weight:700;font-size:17px;letter-spacing:-.02em;margin-bottom:10px}.th em{font-style:normal;font-size:13px;background:var(--ink);color:#fff;border-radius:999px;padding:3px 12px;font-weight:600}
.tile.photo{grid-row:span 2;border-radius:var(--tile) var(--tile) var(--tile) 96px;padding:16px}.tile.photo img{width:100%;aspect-ratio:1/1.12;object-fit:cover;object-position:60% 40%;border-radius:20px 20px 20px 80px;display:block}
.tile.photo p{margin:14px 6px 2px;font-size:14px}.tile.photo b{display:block;font-size:17px;letter-spacing:-.02em}
.chart-c{width:100%;height:auto;margin-top:auto}.chart-c rect{fill:rgba(255,255,255,.5)}.chart-c rect.hi{fill:#fff}.chart-c text{fill:var(--lav-ink);font-size:11px;font-family:var(--font);opacity:.75}
.budget .big{margin:6px 0 14px;font-size:38px}.budget .big small{font-size:14px}.cap{height:12px;background:rgba(255,255,255,.65);border-radius:999px;overflow:hidden}.cap i{display:block;height:100%;background:var(--ink);border-radius:999px}.sm{margin:10px 0 0;font-size:13px;opacity:.8}
.tile.pink{grid-column:1/-1;border-radius:var(--tile) var(--tile) 64px var(--tile);flex-direction:row;align-items:center;gap:20px;padding:18px 26px 18px 24px}
.pink .th{margin:0;flex:1;flex-direction:column;align-items:flex-start;gap:0}.pink .th span{font-size:14px;font-weight:600}.pink .big2{font-size:30px;font-weight:700;letter-spacing:-.035em;line-height:1.1}
.circ{flex-shrink:0;width:38px;height:38px;border-radius:50%;border:1px solid var(--ink);background:#fff;font:400 20px/1 var(--font);cursor:pointer}
.thumbs{display:flex}.thumbs img{width:58px;height:58px;border-radius:50%;object-fit:cover;border:3px solid var(--pink);margin-left:-14px}.thumbs img:first-child{margin-left:0}
/* состояние «новый аккаунт»: пустые плитки и три шага */
.state-new{display:none}.is-new .state-data{display:none}.is-new .state-new{display:block}
.steps.stack{flex-direction:column;align-items:flex-start;margin:0 0 28px}.steps.stack li{padding:12px 24px;width:100%;max-width:440px;justify-content:space-between;align-items:center}
.tiles.ghost .tile{background:#fff;color:var(--muted);border:1.5px dashed var(--line);min-height:150px;justify-content:center;gap:4px;font-size:14px}
.tiles.ghost .tile b{color:var(--ink);font-size:17px;letter-spacing:-.02em}.tiles.ghost .tile.photo{min-height:340px;border-radius:var(--tile) var(--tile) var(--tile) 96px}
.tiles.ghost .tile.pink{min-height:96px;border-radius:var(--tile) var(--tile) 64px var(--tile)}.tiles.ghost .pink .big2{font-size:17px;font-weight:600;color:var(--muted)}
.mock-switch{position:fixed;right:16px;bottom:16px;background:var(--ink);color:#fff;border-radius:999px;padding:5px;display:flex;gap:2px;font-size:13px;z-index:10;align-items:center}
.mock-switch span{padding:6px 8px 6px 14px;opacity:.65}.mock-switch button{font:inherit;color:#fff;background:none;border:0;border-radius:999px;padding:6px 14px;cursor:pointer}.mock-switch button.on{background:#fff;color:var(--ink);font-weight:600}
@media(max-width:1100px){.hero{grid-template-columns:1fr}.hero-h{font-size:52px}}
@media(max-width:900px){.shell{grid-template-columns:minmax(0,1fr)}.side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;padding:12px 14px;gap:8px 12px;border-right:0;border-bottom:1px solid var(--line);align-items:center}
.side nav{flex-direction:row;flex-wrap:wrap;gap:4px}.side .grp,.limit{display:none}.biz{margin-left:auto}main{padding:22px 16px 56px}
.hero-h{font-size:44px}.hero{gap:28px}.stats{grid-template-columns:1fr 1fr}.pair,.editor,.edit-main{grid-template-columns:1fr}.queue li{flex-direction:column;align-items:flex-start}
.tbl{display:block;overflow-x:auto}.tiles{grid-template-columns:1fr}.tile.photo{grid-row:auto}.tile.pink{flex-wrap:wrap}h1{font-size:30px}}
"""

SHELL_C = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Макет C: панель A в языке референса</title>
@@FONTS@@<style>@@CSS@@</style></head><body><div class="shell">
<aside class="side"><div class="brand">Автобидер</div>
<div class="biz">Груминг «Хвост и Лапы»<span>сменить</span></div>
<nav><span class="grp">Реклама</span><a href="#" data-go="overview">Главная</a><a href="#" data-go="ads">Объявления <em>5</em></a><a href="#" data-go="results">Результаты</a>
<span class="grp">Материалы</span><a href="#" data-go="media">Фото и баннеры <em>13</em></a>
<span class="grp">Настройки</span><a href="#">Бизнес</a><a href="#">Подключения</a></nav>
<div class="limit">Генерация: $1,17 из $5 в этом месяце<i><b></b></i></div></aside>
<main>@@SCREENS@@</main></div>
<div class="mock-switch" aria-label="Состояние макета (только для просмотра)"><span>Макет:</span><button class="on" data-state="data">С рекламой</button><button data-state="new">Новый аккаунт</button></div>
<script>document.querySelectorAll('.mock-switch button').forEach(function(b){b.addEventListener('click',function(){document.documentElement.classList.toggle('is-new',b.dataset.state==='new');
document.querySelectorAll('.mock-switch button').forEach(function(x){x.classList.toggle('on',x===b)})})});if(/[?&]new/.test(location.search)){document.querySelector('.mock-switch [data-state=new]').click()}</script>@@SCRIPT@@</body></html>"""


def screens_c() -> str:
    rest = SCREENS[SCREENS.index('<section class="screen" data-screen="ads"'):]
    s = (HOME_C + rest).replace("@@BARSC@@", bars_c()).replace("@@BARS@@", bars()).replace("@@ROWS@@", ad_rows())
    s = s.replace("<h1>Объявления</h1>", "<h1>Объявления</h1>")
    for n, uri in IMG.items():
        s = s.replace(f"@@{n}@@", uri)
    return s


html_c = (SHELL_C.replace("@@FONTS@@", FONTS.format(fam="Golos+Text:wght@400;500;600;700;800")).replace("@@CSS@@", CSS_C)
          .replace("@@SCREENS@@", screens_c()).replace("@@SCRIPT@@", NAV_SCRIPT))
(HERE / "direction-c.html").write_text(html_c, encoding="utf-8")
print("direction-c.html", len(html_c) // 1024, "КБ")


# ---------- Главная для гостей (без входа): один и тот же пример для всех ----------
CSS_GUEST = """
html{scroll-behavior:smooth}a.chip,a.btn{text-decoration:none;display:inline-block}a.chip{display:inline-flex}
.top{display:flex;align-items:center;gap:32px;padding:22px 48px;max-width:1360px;margin:0 auto}.top .brand{padding:0}
.top nav{display:flex;gap:30px;flex:1;justify-content:center}.top nav a{color:var(--ink);text-decoration:none;font-weight:500;font-size:14px}.top nav a:hover{text-decoration:underline;text-underline-offset:4px}
.top .right{display:flex;gap:18px;align-items:center}.top .right .enter{color:var(--ink);font-weight:600;text-decoration:none;font-size:14px}
.page{max-width:1360px;margin:0 auto;padding:10px 48px 72px}.hero.guest{margin:26px 0 110px}
.tiles-wrap{position:relative}.tag{position:absolute;top:-16px;left:18px;z-index:2;background:var(--ink);color:#fff;border-radius:999px;padding:5px 16px;font-size:12.5px;font-weight:600}
.sec h2{font-size:42px;letter-spacing:-.04em;line-height:1.05;margin:0 0 30px;display:block}
.how{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:110px}
.how .card{border-radius:var(--tile);padding:26px;display:flex;flex-direction:column;min-height:380px}
.how .num{display:inline-grid;place-items:center;width:36px;height:36px;border-radius:50%;background:#fff;font-weight:700;margin-bottom:22px}
.how h3{font-size:26px;letter-spacing:-.035em;line-height:1.1;margin:0 0 10px}.how p{margin:0;font-size:15px;line-height:1.5}
.how .card.m{background:var(--mint);color:var(--mint-ink)}.how .card.l{background:var(--lav);color:var(--lav-ink)}.how .card.b{background:var(--butter);color:var(--butter-ink)}
.how .card .shot{margin-top:auto;border-radius:20px;width:100%;aspect-ratio:4/3;object-fit:cover;display:block}
.how .card .chart-c{margin-top:auto;padding-top:22px}
.strip{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px}.strip img{width:100%;border-radius:20px;display:block;aspect-ratio:4/3;object-fit:cover}
.sec{margin-bottom:110px}.sec .note{font-size:14.5px}
.cta-band{display:grid;grid-template-columns:1.1fr 1fr;gap:48px;align-items:center;background:var(--mint);color:var(--mint-ink);border-radius:44px 44px 44px 120px;padding:56px 56px 56px 64px}
.cta-band h2{font-size:46px;letter-spacing:-.04em;line-height:1.02;margin:0 0 14px;display:block}.cta-band p{margin:0;font-size:16px;max-width:380px}
.auth-card{background:#fff;color:var(--ink);border-radius:28px;padding:26px}
.auth-tabs{display:flex;gap:4px;background:var(--soft);border-radius:999px;padding:4px;margin-bottom:20px}.auth-tabs button{flex:1;font:inherit;font-weight:600;border:0;background:none;border-radius:999px;padding:9px;cursor:pointer;color:var(--muted)}
.auth-tabs button.on{background:var(--ink);color:#fff}.auth-card label{margin-bottom:14px}
.auth-card input{font:inherit;color:var(--ink);padding:13px 18px;border:1px solid var(--line);border-radius:999px;background:#fff;width:100%}.auth-card input:focus{outline:3px solid var(--lav);border-color:var(--ink)}
.btn.wide{width:100%;padding:14px}.fine{margin:14px 0 0;font-size:12.5px;color:var(--muted)}
.foot{max-width:1360px;margin:0 auto;padding:0 48px 40px;color:var(--muted);font-size:13px}
@media(max-width:900px){.top{padding:16px;gap:14px}.top nav{display:none}.top .right{margin-left:auto}.page{padding:6px 16px 48px}.hero.guest{margin-bottom:64px}
.how{grid-template-columns:1fr;margin-bottom:64px}.how .card{min-height:0}.sec{margin-bottom:64px}.sec h2{font-size:32px}.strip{grid-auto-flow:column;grid-template-columns:none;grid-auto-columns:72%;overflow-x:auto;padding-bottom:6px}
.cta-band{grid-template-columns:1fr;padding:32px 22px;gap:24px;border-radius:32px 32px 32px 72px}.cta-band h2{font-size:34px}.foot{padding:0 16px 32px}}
"""

GUEST_C = """
<header class="top"><span class="brand">Автобидер</span>
  <nav><a href="#how">Как это работает</a><a href="#example">Пример</a><a href="#start">Начать</a></nav>
  <div class="right"><a class="enter" href="#start">Войти</a><a class="btn" href="#start">Начать</a></div></header>
<main class="page">
  <div class="hero guest">
    <div class="hero-l">
      <span class="badge"><i></i>Для салонов, мастерских и небольших сервисов</span>
      <h1 class="hero-h">Реклама в Директе без хлопот</h1>
      <p class="lead">Расскажите о бизнесе и загрузите фото. Сервис напишет объявления, нарисует баннеры, отправит их на проверку и сам предложит остановить те, что не работают.</p>
      <div class="hero-cta"><a class="btn" href="#start">Начать со своего бизнеса</a><a class="link" href="#example">Смотреть пример</a></div>
      <div class="chiprow">
        <a class="chip" href="#how">Объявления с баннерами<span class="plus">+</span></a><a class="chip" href="#how">Проверка текста на баннере<span class="plus">+</span></a>
        <a class="chip" href="#example">Бюджет по неделям<span class="plus">+</span></a><a class="chip" href="#example">Отчёт простыми словами<span class="plus">+</span></a>
      </div>
    </div>
    <div class="tiles-wrap"><span class="tag">Пример: салон груминга</span>
      <div class="tiles">
        <div class="tile mint photo"><img src="@@p2@@" alt="Лучшее фото"><p><b>Лучшее фото</b>Работа мастера: 2,2 % кликов</p></div>
        <div class="tile lav chart"><div class="th"><span>Клики</span><em>+214</em></div>@@BARSC@@</div>
        <div class="tile butter budget"><div class="th"><span>Бюджет недели</span></div><p class="big">3 240 <small>из 5 000 ₽</small></p>
          <div class="cap" role="img" aria-label="Потрачено 65 процентов"><i style="width:65%"></i></div><p class="sm">Идём чуть медленнее плана</p></div>
        <div class="tile pink ideas"><div class="th"><span>Идеи стратега</span><b class="big2">5 новых</b></div>
          <div class="thumbs"><img src="@@b2@@" alt=""><img src="@@b4@@" alt=""><img src="@@b3@@" alt=""></div></div>
      </div>
    </div>
  </div>

  <section class="sec" id="how"><h2>Как это работает</h2>
    <div class="how">
      <div class="card m"><span class="num">1</span><h3>Расскажите о бизнесе</h3><p>Что делаете, где находитесь, сколько готовы тратить в неделю. Загрузите фото: баннеры сделаем из них.</p><img class="shot" src="@@p1@@" alt=""></div>
      <div class="card l"><span class="num">2</span><h3>Получите объявления и баннеры</h3><p>Сервис пишет тексты, рисует баннеры и сам проверяет, что надписи на них без ошибок. Вы выбираете, что взять в рекламу.</p><img class="shot" src="@@b1@@" alt=""></div>
      <div class="card b"><span class="num">3</span><h3>Следите за результатами</h3><p>Видно показы, клики и заявки. Слабые объявления сервис предлагает остановить, решаете вы.</p>@@BARSC@@</div>
    </div></section>

  <section class="sec" id="example"><h2>Что получилось у салона груминга</h2>
    <div class="strip"><img src="@@b1@@" alt=""><img src="@@b2@@" alt=""><img src="@@b4@@" alt=""><img src="@@b5@@" alt=""><img src="@@b3@@" alt=""></div>
    <p class="note">5 объявлений, 9 заявок за две недели при бюджете 5 000 ₽ в неделю. Цифры в примере условные.</p></section>

  <section class="cta-band" id="start">
    <div><h2>Начните с описания бизнеса</h2><p>Реклама уходит на проверку только после вашего подтверждения.</p></div>
    <div class="auth-card"><div class="auth-tabs"><button class="on">Регистрация</button><button>Вход</button></div>
      <form onsubmit="return false"><label>Электронная почта<input type="email" placeholder="name@example.com"></label><label>Пароль<input type="password"></label>
      <button class="btn wide">Создать аккаунт</button></form></div>
  </section>
</main>
<footer class="foot">Автобидер. Работает с Яндекс Директом.</footer>
"""

html_g = ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Макет C: главная для гостей</title>'
          + FONTS.format(fam="Golos+Text:wght@400;500;600;700;800") + "<style>" + CSS_C + CSS_GUEST + "</style></head><body>"
          + GUEST_C.replace("@@BARSC@@", bars_c()) + "</body></html>")
for n, uri in IMG.items():
    html_g = html_g.replace(f"@@{n}@@", uri)
(HERE / "direction-c-guest.html").write_text(html_g, encoding="utf-8")
print("direction-c-guest.html", len(html_g) // 1024, "КБ")









