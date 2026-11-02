"""Срезы рекламы по измерениям: устройства, пол, возраст, площадки, часы суток и другое."""
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Query

from app.db import get_db
from app.deps import current_user
from app.direct import slices
from app.routes.businesses import _own

router = APIRouter(prefix="/api/businesses/{business_id}/slices", tags=["slices"])


@router.get("")
def get_slices(business_id: int, days: int = Query(slices.WINDOW_DAYS, ge=1, le=slices.WINDOW_DAYS), user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_db)):
    biz = _own(conn, user, business_id)
    launched = conn.execute("SELECT 1 FROM campaigns WHERE business_id = ? AND source = 'direct'", (business_id,)).fetchone() is not None
    return {"launched": launched, **slices.summary(conn, biz, date.today().isoformat(), days)}
