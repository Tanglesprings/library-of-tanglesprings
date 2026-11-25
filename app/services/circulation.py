"""Resolve the physical copy for a circulation action inside a write transaction."""

from app.services import item_copies


class CirculationError(ValueError):
    pass


def resolve_copy(db, item_id: int, copy_id: int | None):
    if copy_id is not None:
        copy = db.execute("SELECT * FROM copies_live WHERE id = ?", (copy_id,)).fetchone()
        if copy is None or copy["item_id"] != item_id:
            raise CirculationError("That copy is no longer part of this title.")
        return copy

    copies = db.execute(
        "SELECT * FROM copies_live WHERE item_id = ? ORDER BY copy_number LIMIT 2", (item_id,),
    ).fetchall()
    if len(copies) > 1:
        raise CirculationError("Scan the library label or choose a specific copy on the item page.")
    return copies[0] if copies else None


def active_loan(db, item_id: int, copy_id: int | None):
    return db.execute(
        "SELECT c.*, b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
        "WHERE c.item_id = ? AND c.checked_in IS NULL AND (c.copy_id IS NULL OR c.copy_id = ?) "
        "ORDER BY c.copy_id IS NULL LIMIT 1", (item_id, copy_id),
    ).fetchone()


def check_in(db, loan_id: int, copy, location_id: int | None = None):
    if location_id is not None:
        if copy is None:
            raise CirculationError("A physical copy is required to change the return location.")
        if not db.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            raise CirculationError("That return location no longer exists.")
        item_copies.relocate_copy(db, copy, location_id)
    db.execute("UPDATE checkouts SET checked_in = datetime('now') WHERE id = ?", (loan_id,))
