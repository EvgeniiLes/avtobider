# Развёртывание на сервере

Подходит чистый сервер Ubuntu или Debian с Python 3.10 или новее, 1 ядро и 1 ГБ памяти достаточно. Нужны: доступ по SSH с правами root и домен, запись A которого указывает на IP сервера.

Сервис работает как служба systemd (`autobider`), перед ним стоит nginx с HTTPS. Данные (база и загруженные файлы) лежат в `/opt/autobider/data`.

## 1. Скопируйте проект на сервер
Через git (если проект выложен в репозиторий):

```bash
git clone <адрес репозитория> /opt/autobider
```

или архивом со своего компьютера:

```bash
scp -r ./avtobider root@IP-СЕРВЕРА:/opt/autobider
```

## 2. Запустите установку

```bash
cd /opt/autobider
chmod +x deploy/install.sh
./deploy/install.sh ваш-домен.ru
```

Скрипт ставит пакеты, создаёт пользователя `autobider`, виртуальное окружение, службу и настройки nginx. Его можно запускать повторно.

## 3. HTTPS

```bash
certbot --nginx -d ваш-домен.ru
```

Сертификат продлевается сам.

## 4. Настройки (`/opt/autobider/.env`)
Откройте файл и заполните. Секреты храните только здесь, в репозиторий они не попадают.

| Переменная | Что это |
|---|---|
| `SECRET_KEY` | Ключ шифрования токенов кабинетов. Сгенерируйте: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Потеряете ключ: сохранённые кабинеты придётся подключить заново |
| `APP_ENV` | Оставьте пустым. Значение `development` включает кнопку «Взять токен с сервера», на боевом сервере её быть не должно |
| `YANDEX_CLIENT_ID`, `YANDEX_CLIENT_SECRET` | Приложение в Яндекс OAuth для работы с API Директа (тип «Для доступа к API») |
| `YANDEX_LOGIN_CLIENT_ID`, `YANDEX_LOGIN_CLIENT_SECRET`, `YANDEX_REDIRECT_URI` | Приложение для входа через Яндекс ID. В настройках приложения должен быть указан адрес `https://ваш-домен.ru/auth/yandex/callback` |
| `ANTHROPIC_API_KEY`, `FAL_KEY` | Ключи для текстов и картинок, когда подключены |

После изменения: `systemctl restart autobider`.

## 5. Проверка

```bash
curl https://ваш-домен.ru/api/health      # {"ok":true}
systemctl status autobider
journalctl -u autobider -n 50
```

## Обновление
Скопируйте новую версию кода в `/opt/autobider` и выполните:

```bash
cd /opt/autobider
.venv/bin/pip install -r requirements.txt
systemctl restart autobider
```

База обновляется сама при запуске.

## Резервные копии
Достаточно копировать папку `/opt/autobider/data` и файл `/opt/autobider/.env`. Базу можно копировать на ходу командой `sqlite3 data/app.db ".backup data/backup.db"`.

## Если что-то не работает
- Сайт не открывается: `systemctl status autobider` и `journalctl -u autobider -n 100`.
- Ошибка 502: служба не запущена, смотрите журнал.
- Загрузка большого видео обрывается: проверьте `client_max_body_size` в `/etc/nginx/sites-available/autobider`.
- Вход «зависает» или пропадает после перезагрузки страницы: проверьте, что открываете сайт по HTTPS и что nginx передаёт заголовок `X-Forwarded-Proto` (он уже есть в нашем конфиге).
