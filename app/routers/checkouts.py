from datetime import date, timedelta
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db, get_setting
from app.features import require_feature
from app.services import circulation

router = APIRouter(prefix="/api")

DEFAULT_OVERDUE_DAYS = 28

# A loan is overdue when its explicit due date has passed, or — for loans
# with no due date (e.g. created via the Lend scan mode) — when it has been
# out longer than the configured fallback window. One ? param: fallback days.
OVERDUE_CONDITION = (
    "c.checked_in IS NULL AND ("
    "  (c.due_date IS NOT NULL AND c.due_date < date('now'))"
    "  OR (c.due_date IS NULL AND julianday('now') - julianday(c.checked_out) > ?)"
    ")"
)


def get_overdue_days(db) -> int:
    """Fallback overdue window in days; 0 disables the no-due-date fallback."""
    raw = get_setting(db, "lending_overdue_days")
    try:
        days = int(raw) if raw else DEFAULT_OVERDUE_DAYS
    except ValueError:
        days = DEFAULT_OVERDUE_DAYS
    # 0 = disabled: make the fallback unreachable rather than branching SQL
    return days if days > 0 else 10**9


def get_overdue_loans(db) -> list[dict]:
    """All overdue checkouts with item and borrower context."""
    rows = db.execute(
        "SELECT c.*, i.title, i.cover_path, b.name as borrower_name, "
        "CAST(julianday('now') - julianday(c.checked_out) AS INTEGER) as days_out "
        "FROM checkouts c "
        "JOIN items_live i ON c.item_id = i.id "
        "JOIN borrowers b ON c.borrower_id = b.id "
        f"WHERE {OVERDUE_CONDITION} "
        "ORDER BY c.checked_out ASC",
        (get_overdue_days(db),),
    ).fetchall()
    return [dict(row) for row in rows]


def count_overdue(db) -> int:
    """How many checkouts are overdue — the same predicate as get_overdue_loans."""
    return db.execute(
        "SELECT COUNT(*) AS c FROM checkouts c "
        "JOIN items_live i ON c.item_id = i.id "
        f"WHERE {OVERDUE_CONDITION}",
        (get_overdue_days(db),),
    ).fetchone()["c"]


# --- Borrowers ---

@router.post("/borrowers")
async def create_borrower(
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    _=Depends(require_role("admin")),
    __=Depends(require_feature("lending")),
):
    name, email, phone = name.strip(), email.strip(), phone.strip()
    if not name or len(name) > 160 or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return RedirectResponse(url="/settings?borrower_error=details#borrowers", status_code=303)
    if len(phone) > 40 or not re.fullmatch(r"\+?[0-9 .()-]+", phone) or not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        return RedirectResponse(url="/settings?borrower_error=details#borrowers", status_code=303)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM borrowers WHERE name = ? AND email = ? COLLATE NOCASE", (name, email)).fetchone():
            return RedirectResponse(url="/settings?borrower_error=duplicate#borrowers", status_code=303)
        label = name
        suffix = 2
        while db.execute("SELECT 1 FROM borrowers WHERE name = ?", (label,)).fetchone():
            label = f"{name} ({suffix})"
            suffix += 1
        db.execute("INSERT INTO borrowers (name, email, phone) VALUES (?, ?, ?)", (label, email, phone))
    return RedirectResponse(url="/settings#borrowers", status_code=303)


@router.post("/borrowers/{borrower_id}/delete")
async def delete_borrower(
    borrower_id: int,
    _=Depends(require_role("admin")),
    __=Depends(require_feature("lending")),
):
    """Remove a borrower and, with them, their completed loan history.

    `checkouts.borrower_id` has no ON DELETE action and foreign keys are
    enforced, so deleting a borrower who has ever returned a book used to
    raise IntegrityError and 500 (issue #29). Their history goes with them,
    matching what deleting a location or a platform already does.
    """
    with get_db() as db:
        # Take the write lock *before* reading the guard. sqlite3 opens no
        # transaction for a bare SELECT, so without this the active-loan
        # count is read outside any lock and a checkout committed between
        # that read and the DELETE below would be destroyed as "history".
        # The foreign key used to make that interleaving fail safe; the
        # cascade removes that accidental protection, so the lock replaces
        # it. Read, decide, and write are now one serialized unit.
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT COUNT(*) as c FROM checkouts WHERE borrower_id = ? AND checked_in IS NULL",
            (borrower_id,),
        ).fetchone()["c"]
        if active > 0:
            return RedirectResponse(url="/settings?borrower_error=active", status_code=303)
        db.execute("DELETE FROM checkouts WHERE borrower_id = ?", (borrower_id,))
        db.execute("DELETE FROM borrowers WHERE id = ?", (borrower_id,))
    return RedirectResponse(url="/settings", status_code=303)


# --- Checkouts ---

@router.post("/items/{item_id}/checkout")
async def checkout_item(
    request: Request,
    item_id: int,
    borrower_id: int = Form(...),
    due_days: int = Form(14),
    notes: str = Form("", max_length=1000),
    copy_id: int | None = Form(None),
    _=Depends(require_role("editor")),
    __=Depends(require_feature("lending")),
):
    """Check out an item to a borrower."""
    if due_days > 0:
        try:
            due = (date.today() + timedelta(days=due_days)).isoformat()
        except OverflowError:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Due date is out of range")}, status_code=400
            )
    else:
        due = None

    with get_db() as db:
        # Lock first: an item trashed or lent between these reads and the
        # INSERT would otherwise get a loan anyway (G18).
        db.execute("BEGIN IMMEDIATE")
        # A stale item page can still post here after the item went to Trash.
        if db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone() is None:
            return JSONResponse({"ok": False, "message": i18n.gettext("Item not found")}, status_code=404)
        if not db.execute("SELECT 1 FROM borrowers WHERE id = ?", (borrower_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Borrower not found"}, status_code=404)
        try:
            copy = circulation.resolve_copy(db, item_id, copy_id)
        except circulation.CirculationError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
        resolved_copy_id = copy["id"] if copy else None
        if circulation.active_loan(db, item_id, resolved_copy_id):
            return {"ok": False, "message": i18n.gettext("Already checked out")}

        db.execute(
            "INSERT INTO checkouts (item_id, borrower_id, due_date, notes, copy_id) VALUES (?, ?, ?, ?, ?)",
            (item_id, borrower_id, due, notes.strip() or None, resolved_copy_id),
        )

    return RedirectResponse(url=f"/item/{item_id}", status_code=303)


@router.post("/checkouts/{checkout_id}/checkin")
async def checkin_item(
    checkout_id: int,
    _=Depends(require_role("editor")),
    __=Depends(require_feature("lending")),
):
    """Check in an active item loan exactly once."""
    with get_db() as db:
        checkout = db.execute(
            "SELECT item_id, checked_in FROM checkouts WHERE id = ?", (checkout_id,)
        ).fetchone()
        if not checkout:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Checkout not found")}, status_code=404
            )
        if checkout["checked_in"] is not None:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Checkout already checked in")}, status_code=409
            )
        cursor = db.execute(
            "UPDATE checkouts SET checked_in = datetime('now') "
            "WHERE id = ? AND checked_in IS NULL",
            (checkout_id,),
        )
        if cursor.rowcount != 1:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Checkout already checked in")}, status_code=409
            )
    return RedirectResponse(url=f"/item/{checkout['item_id']}", status_code=303)


@router.get("/checkouts/overdue")
async def overdue_items(
    request: Request,
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("lending")),
):
    """List all overdue checkouts (explicit due date or fallback window)."""
    with get_db() as db:
        return get_overdue_loans(db)
