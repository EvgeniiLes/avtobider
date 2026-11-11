"""Видео объявления в Директе: загрузка файла, ожидание конвертации, креатив-видеодополнение и привязка к объявлению."""
import sqlite3

from app.direct.client import DirectClient, DirectError

FAILED_NOTE = "Директ не смог обработать видео: проверьте формат и длительность ролика, сделайте видео заново."


async def send(client: DirectClient, conn: sqlite3.Connection, unit: sqlite3.Row, data: bytes, asset_id: int) -> str:
    """Отправляет видео к конкретному объявлению и сразу проверяет, не готово ли оно. Возвращает состояние: converting или attached."""
    if unit["ad_kind"]:
        raise DirectError("У комбинаторного объявления видео пока не подключаем: Директ для него принимает видео другим способом.")
    if not unit["external_ad_id"]:
        raise DirectError("Объявление ещё не создано в Директе.")
    video_id = await client.upload_video(data, f"autobider-{unit['business_id']}-{unit['id']}-{asset_id}.mp4")
    conn.execute("UPDATE ad_units SET video_id = ?, video_creative = NULL, video_state = 'converting', video_note = '', video_asset = ? WHERE id = ?",
                 (video_id, asset_id, unit["id"]))
    conn.commit()
    await advance(client, conn, unit["business_id"])
    return conn.execute("SELECT video_state FROM ad_units WHERE id = ?", (unit["id"],)).fetchone()[0]


async def advance(client: DirectClient, conn: sqlite3.Connection, business_id: int) -> int:
    """Доводит до объявления видео, которые Директ уже обработал. Возвращает, сколько видео сдвинулось. Ошибки Директа записывает объявлению, а не поднимает."""
    waiting = conn.execute("SELECT * FROM ad_units WHERE business_id = ? AND video_state = 'converting' AND video_id != ''", (business_id,)).fetchall()
    if not waiting:
        return 0
    statuses = await client.video_statuses([u["video_id"] for u in waiting])
    moved = 0
    for u in waiting:
        status = statuses.get(u["video_id"], "")
        if status == "ERROR":
            conn.execute("UPDATE ad_units SET video_state = 'error', video_note = ? WHERE id = ?", (FAILED_NOTE, u["id"]))
            moved += 1
        elif status == "READY":
            try:
                creative_id = u["video_creative"] or await client.create_video_creative(u["video_id"])
                conn.execute("UPDATE ad_units SET video_creative = ? WHERE id = ?", (creative_id, u["id"]))   # запоминаем до привязки: повтор не создаст второй креатив
                await client.attach_video(int(u["external_ad_id"]), creative_id)   # в базе номер лежит строкой, а Директу нужно число
            except DirectError as e:
                conn.execute("UPDATE ad_units SET video_state = 'error', video_note = ? WHERE id = ?", (e.full_message(), u["id"]))
            else:
                conn.execute("UPDATE ad_units SET video_state = 'attached', video_note = '' WHERE id = ?", (u["id"],))
            moved += 1
    conn.commit()
    return moved
