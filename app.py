import sqlite3
from flask import Flask, g, render_template, request, redirect, url_for, flash
from datetime import date, datetime, timedelta

app = Flask(__name__)
app.secret_key = 'ehs-inspector-secret-key-change-in-production'

DATABASE = 'db.sqlite3'
PER_PAGE = 25

CATEGORIES = {
    'ladder':            {'label': 'Ladder',             'freq_days': 90,  'types': ['initial', 'quarterly']},
    'hoisting_rigging':  {'label': 'Hoisting & Rigging', 'freq_days': 90,  'types': ['initial', 'quarterly']},
    'fire_extinguisher': {'label': 'Fire Extinguisher',  'freq_days': 30,  'types': ['initial', 'monthly']},
}

INSPECTION_TYPES = {
    'initial':   'Initial / Inventory',
    'quarterly': 'Quarterly',
    'monthly':   'Monthly',
}

RESULTS = {
    'pass':         ('Pass',         'success'),
    'fail':         ('Fail',         'danger'),
    'needs_repair': ('Needs Repair', 'warning'),
}

STATUSES = {
    'active':          ('Active',          'success'),
    'out_of_service':  ('Tagged Out',      'warning'),
    'retired':         ('Retired',         'secondary'),
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()


def query_db(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv


def execute_db(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS equipment (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            eq_id       TEXT NOT NULL UNIQUE,
            category    TEXT NOT NULL,
            description TEXT NOT NULL,
            location    TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            date_added  TEXT NOT NULL,
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS inspections (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            equipment_id      INTEGER NOT NULL REFERENCES equipment(id),
            inspector_name    TEXT NOT NULL,
            inspection_date   TEXT NOT NULL,
            inspection_type   TEXT NOT NULL,
            result            TEXT NOT NULL,
            findings          TEXT,
            corrective_action TEXT,
            date_logged       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS walkdowns (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            inspector_name   TEXT NOT NULL,
            walkdown_date    TEXT NOT NULL,
            area_location    TEXT NOT NULL,
            findings         TEXT,
            actions_required TEXT,
            status           TEXT NOT NULL DEFAULT 'open',
            date_logged      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_inspections_equipment ON inspections(equipment_id);
        CREATE INDEX IF NOT EXISTS idx_inspections_date      ON inspections(inspection_date);
        CREATE INDEX IF NOT EXISTS idx_walkdowns_date        ON walkdowns(walkdown_date);
    """)
    db.commit()


# ---------------------------------------------------------------------------
# Context processor — injects constants into all templates
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals():
    return dict(
        categories=CATEGORIES,
        inspection_types=INSPECTION_TYPES,
        results=RESULTS,
        statuses=STATUSES,
        today=date.today().isoformat(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_next_due(category, last_inspected_str):
    """Return (next_due date, days_overdue int). days_overdue > 0 means overdue."""
    freq_days = CATEGORIES[category]['freq_days']
    if last_inspected_str is None:
        return None, None
    last = date.fromisoformat(last_inspected_str)
    next_due = last + timedelta(days=freq_days)
    days_overdue = (date.today() - next_due).days
    return next_due, days_overdue


def get_overdue_equipment():
    today = date.today()
    rows = query_db("""
        SELECT e.id, e.eq_id, e.description, e.location, e.category, e.date_added,
               MAX(i.inspection_date) AS last_inspected
        FROM equipment e
        LEFT JOIN inspections i ON i.equipment_id = e.id AND i.inspection_type != 'initial'
        WHERE e.status = 'active'
        GROUP BY e.id
    """)
    overdue = []
    for row in rows:
        freq_days = CATEGORIES[row['category']]['freq_days']
        if row['last_inspected'] is None:
            days_overdue = (today - date.fromisoformat(row['date_added'])).days
            next_due = None
        else:
            next_due = date.fromisoformat(row['last_inspected']) + timedelta(days=freq_days)
            days_overdue = (today - next_due).days
        if days_overdue > 0:
            item = dict(row)
            item['days_overdue'] = days_overdue
            item['next_due'] = next_due.isoformat() if next_due else 'Never Inspected'
            overdue.append(item)
    return sorted(overdue, key=lambda x: x['days_overdue'], reverse=True)


def enrich_equipment_row(row):
    """Add next_due and overdue info to an equipment row dict."""
    row = dict(row)
    last = row.get('last_inspected')
    if row['status'] != 'active':
        row['next_due'] = None
        row['days_overdue'] = None
        return row
    if last is None:
        row['next_due'] = None
        freq_days = CATEGORIES[row['category']]['freq_days']
        row['days_overdue'] = (date.today() - date.fromisoformat(row['date_added'])).days
    else:
        freq_days = CATEGORIES[row['category']]['freq_days']
        next_due = date.fromisoformat(last) + timedelta(days=freq_days)
        row['next_due'] = next_due.isoformat()
        row['days_overdue'] = (date.today() - next_due).days
    return row


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route('/')
def dashboard():
    overdue = get_overdue_equipment()

    first_of_month = date.today().replace(day=1).isoformat()

    total_active = query_db(
        "SELECT COUNT(*) AS c FROM equipment WHERE status = 'active'", one=True)['c']

    inspections_this_month = query_db(
        "SELECT COUNT(*) AS c FROM inspections WHERE inspection_date >= ?",
        [first_of_month], one=True)['c']

    open_walkdowns = query_db(
        "SELECT COUNT(*) AS c FROM walkdowns WHERE status = 'open'", one=True)['c']

    recent_inspections = query_db("""
        SELECT i.id, i.inspection_date, i.inspector_name, i.inspection_type, i.result,
               e.eq_id, e.description
        FROM inspections i JOIN equipment e ON e.id = i.equipment_id
        ORDER BY i.inspection_date DESC, i.id DESC LIMIT 10
    """)

    recent_walkdowns = query_db("""
        SELECT id, walkdown_date, area_location, inspector_name, status
        FROM walkdowns ORDER BY walkdown_date DESC, id DESC LIMIT 5
    """)

    return render_template('dashboard.html',
        overdue=overdue,
        total_active=total_active,
        inspections_this_month=inspections_this_month,
        open_walkdowns=open_walkdowns,
        recent_inspections=recent_inspections,
        recent_walkdowns=recent_walkdowns,
    )


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------

@app.route('/equipment')
def equipment_list():
    category = request.args.get('category', '')
    status   = request.args.get('status', 'active')
    search   = request.args.get('search', '').strip()

    conditions = []
    args = []

    if category:
        conditions.append("e.category = ?")
        args.append(category)
    if status:
        conditions.append("e.status = ?")
        args.append(status)
    if search:
        conditions.append("(e.eq_id LIKE ? OR e.description LIKE ? OR e.location LIKE ?)")
        args += [f'%{search}%', f'%{search}%', f'%{search}%']

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

    rows = query_db(f"""
        SELECT e.*, MAX(i.inspection_date) AS last_inspected
        FROM equipment e
        LEFT JOIN inspections i ON i.equipment_id = e.id
        {where}
        GROUP BY e.id
        ORDER BY e.eq_id
    """, args)

    equipment = [enrich_equipment_row(r) for r in rows]

    return render_template('equipment/list.html',
        equipment=equipment,
        filter_category=category,
        filter_status=status,
        filter_search=search,
    )


@app.route('/equipment/add', methods=['GET', 'POST'])
def equipment_add():
    if request.method == 'POST':
        eq_id       = request.form.get('eq_id', '').strip()
        category    = request.form.get('category', '').strip()
        description = request.form.get('description', '').strip()
        location    = request.form.get('location', '').strip()
        notes       = request.form.get('notes', '').strip()

        errors = []
        if not eq_id:       errors.append('Equipment ID is required.')
        if category not in CATEGORIES: errors.append('Invalid category.')
        if not description: errors.append('Description is required.')
        if not location:    errors.append('Location is required.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('equipment_add'))

        try:
            execute_db(
                "INSERT INTO equipment (eq_id, category, description, location, notes, date_added) VALUES (?,?,?,?,?,?)",
                [eq_id, category, description, location, notes, date.today().isoformat()]
            )
            flash(f'Equipment {eq_id} added successfully.', 'success')
            return redirect(url_for('equipment_list'))
        except sqlite3.IntegrityError:
            flash(f'Equipment ID "{eq_id}" already exists. Choose a unique ID.', 'danger')
            return redirect(url_for('equipment_add'))

    return render_template('equipment/add.html')


@app.route('/equipment/<int:eq_id>')
def equipment_detail(eq_id):
    eq = query_db("SELECT * FROM equipment WHERE id = ?", [eq_id], one=True)
    if not eq:
        flash('Equipment not found.', 'danger')
        return redirect(url_for('equipment_list'))

    inspections = query_db("""
        SELECT * FROM inspections WHERE equipment_id = ?
        ORDER BY inspection_date DESC, id DESC
    """, [eq_id])

    last_periodic = query_db("""
        SELECT MAX(inspection_date) AS last_inspected FROM inspections
        WHERE equipment_id = ? AND inspection_type != 'initial'
    """, [eq_id], one=True)

    eq = enrich_equipment_row(dict(eq) | {'last_inspected': last_periodic['last_inspected']})

    return render_template('equipment/detail.html', eq=eq, inspections=inspections)


@app.route('/equipment/<int:eq_id>/edit', methods=['GET', 'POST'])
def equipment_edit(eq_id):
    eq = query_db("SELECT * FROM equipment WHERE id = ?", [eq_id], one=True)
    if not eq:
        flash('Equipment not found.', 'danger')
        return redirect(url_for('equipment_list'))

    if request.method == 'POST':
        description = request.form.get('description', '').strip()
        location    = request.form.get('location', '').strip()
        notes       = request.form.get('notes', '').strip()

        if not description or not location:
            flash('Description and location are required.', 'danger')
            return redirect(url_for('equipment_edit', eq_id=eq_id))

        execute_db(
            "UPDATE equipment SET description=?, location=?, notes=? WHERE id=?",
            [description, location, notes, eq_id]
        )
        flash('Equipment updated.', 'success')
        return redirect(url_for('equipment_detail', eq_id=eq_id))

    return render_template('equipment/edit.html', eq=eq)


@app.route('/equipment/<int:eq_id>/tag-out', methods=['POST'])
def equipment_tag_out(eq_id):
    eq = query_db("SELECT * FROM equipment WHERE id = ?", [eq_id], one=True)
    if not eq:
        flash('Equipment not found.', 'danger')
        return redirect(url_for('equipment_list'))
    new_status = 'active' if eq['status'] == 'out_of_service' else 'out_of_service'
    execute_db("UPDATE equipment SET status=? WHERE id=?", [new_status, eq_id])
    label = 'returned to service' if new_status == 'active' else 'tagged out (out of service)'
    flash(f'Equipment {eq["eq_id"]} {label}.', 'success')
    return redirect(url_for('equipment_detail', eq_id=eq_id))


@app.route('/equipment/<int:eq_id>/retire', methods=['POST'])
def equipment_retire(eq_id):
    eq = query_db("SELECT * FROM equipment WHERE id = ?", [eq_id], one=True)
    if not eq:
        flash('Equipment not found.', 'danger')
        return redirect(url_for('equipment_list'))
    execute_db("UPDATE equipment SET status='retired' WHERE id=?", [eq_id])
    flash(f'Equipment {eq["eq_id"]} retired.', 'success')
    return redirect(url_for('equipment_list'))


@app.route('/equipment/<int:eq_id>/delete', methods=['POST'])
def equipment_delete(eq_id):
    eq = query_db("SELECT * FROM equipment WHERE id = ?", [eq_id], one=True)
    if not eq:
        flash('Equipment not found.', 'danger')
        return redirect(url_for('equipment_list'))

    inspection_count = query_db(
        "SELECT COUNT(*) AS c FROM inspections WHERE equipment_id = ?", [eq_id], one=True)['c']
    if inspection_count > 0:
        flash(
            f'Cannot delete equipment with {inspection_count} inspection record(s). '
            'Use "Retire" to remove it from active service while preserving history.',
            'danger'
        )
        return redirect(url_for('equipment_detail', eq_id=eq_id))

    execute_db("DELETE FROM equipment WHERE id=?", [eq_id])
    flash(f'Equipment {eq["eq_id"]} permanently deleted.', 'success')
    return redirect(url_for('equipment_list'))


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------

@app.route('/equipment/<int:eq_id>/inspect', methods=['GET', 'POST'])
def inspection_log(eq_id):
    eq = query_db("SELECT * FROM equipment WHERE id = ?", [eq_id], one=True)
    if not eq:
        flash('Equipment not found.', 'danger')
        return redirect(url_for('equipment_list'))

    if eq['status'] != 'active':
        flash('Cannot log inspections for equipment that is tagged out or retired.', 'warning')
        return redirect(url_for('equipment_detail', eq_id=eq_id))

    allowed_types = CATEGORIES[eq['category']]['types']

    if request.method == 'POST':
        inspector_name    = request.form.get('inspector_name', '').strip()
        inspection_date   = request.form.get('inspection_date', '').strip()
        inspection_type   = request.form.get('inspection_type', '').strip()
        result            = request.form.get('result', '').strip()
        findings          = request.form.get('findings', '').strip()
        corrective_action = request.form.get('corrective_action', '').strip()

        errors = []
        if not inspector_name:  errors.append('Inspector name is required.')
        if not inspection_date: errors.append('Inspection date is required.')
        if inspection_type not in allowed_types:
            errors.append('Invalid inspection type for this equipment category.')
        if result not in RESULTS:
            errors.append('Invalid result value.')
        if inspection_date:
            try:
                if date.fromisoformat(inspection_date) > date.today():
                    errors.append('Inspection date cannot be in the future.')
            except ValueError:
                errors.append('Invalid date format.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('inspection_log', eq_id=eq_id))

        execute_db("""
            INSERT INTO inspections
              (equipment_id, inspector_name, inspection_date, inspection_type,
               result, findings, corrective_action, date_logged)
            VALUES (?,?,?,?,?,?,?,?)
        """, [eq_id, inspector_name, inspection_date, inspection_type,
              result, findings, corrective_action, datetime.now().isoformat()])

        flash(f'Inspection logged for {eq["eq_id"]}.', 'success')
        return redirect(url_for('equipment_detail', eq_id=eq_id))

    return render_template('inspections/log.html', eq=eq, allowed_types=allowed_types)


@app.route('/inspections')
def inspection_history():
    search          = request.args.get('search', '').strip()
    filter_category = request.args.get('category', '')
    filter_type     = request.args.get('inspection_type', '')
    filter_result   = request.args.get('result', '')
    date_from       = request.args.get('date_from', '')
    date_to         = request.args.get('date_to', '')
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conditions = ['1=1']
    args = []

    if search:
        conditions.append("(e.eq_id LIKE ? OR e.description LIKE ? OR i.inspector_name LIKE ?)")
        args += [f'%{search}%', f'%{search}%', f'%{search}%']
    if filter_category:
        conditions.append("e.category = ?")
        args.append(filter_category)
    if filter_type:
        conditions.append("i.inspection_type = ?")
        args.append(filter_type)
    if filter_result:
        conditions.append("i.result = ?")
        args.append(filter_result)
    if date_from:
        conditions.append("i.inspection_date >= ?")
        args.append(date_from)
    if date_to:
        conditions.append("i.inspection_date <= ?")
        args.append(date_to)

    where = ' AND '.join(conditions)

    total = query_db(
        f"SELECT COUNT(*) AS c FROM inspections i JOIN equipment e ON e.id = i.equipment_id WHERE {where}",
        args, one=True)['c']
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)

    rows = query_db(f"""
        SELECT i.*, e.eq_id, e.description, e.category
        FROM inspections i JOIN equipment e ON e.id = i.equipment_id
        WHERE {where}
        ORDER BY i.inspection_date DESC, i.id DESC
        LIMIT ? OFFSET ?
    """, args + [PER_PAGE, (page - 1) * PER_PAGE])

    return render_template('inspections/history.html',
        inspections=rows,
        total=total,
        page=page,
        total_pages=total_pages,
        filter_search=search,
        filter_category=filter_category,
        filter_type=filter_type,
        filter_result=filter_result,
        filter_date_from=date_from,
        filter_date_to=date_to,
    )


@app.route('/inspections/<int:insp_id>/delete', methods=['POST'])
def inspection_delete(insp_id):
    insp = query_db("SELECT i.*, e.eq_id, e.id AS eq_pk FROM inspections i JOIN equipment e ON e.id=i.equipment_id WHERE i.id=?", [insp_id], one=True)
    if not insp:
        flash('Inspection not found.', 'danger')
        return redirect(url_for('inspection_history'))
    execute_db("DELETE FROM inspections WHERE id=?", [insp_id])
    flash('Inspection record deleted.', 'success')
    return redirect(url_for('equipment_detail', eq_id=insp['eq_pk']))


# ---------------------------------------------------------------------------
# Walkdowns
# ---------------------------------------------------------------------------

@app.route('/walkdowns')
def walkdown_list():
    filter_status = request.args.get('status', '')
    date_from     = request.args.get('date_from', '')
    date_to       = request.args.get('date_to', '')
    search        = request.args.get('search', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    conditions = ['1=1']
    args = []

    if filter_status:
        conditions.append("status = ?")
        args.append(filter_status)
    if date_from:
        conditions.append("walkdown_date >= ?")
        args.append(date_from)
    if date_to:
        conditions.append("walkdown_date <= ?")
        args.append(date_to)
    if search:
        conditions.append("(area_location LIKE ? OR inspector_name LIKE ? OR findings LIKE ?)")
        args += [f'%{search}%', f'%{search}%', f'%{search}%']

    where = ' AND '.join(conditions)

    total = query_db(f"SELECT COUNT(*) AS c FROM walkdowns WHERE {where}", args, one=True)['c']
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)

    rows = query_db(f"""
        SELECT * FROM walkdowns WHERE {where}
        ORDER BY walkdown_date DESC, id DESC
        LIMIT ? OFFSET ?
    """, args + [PER_PAGE, (page - 1) * PER_PAGE])

    return render_template('walkdowns/list.html',
        walkdowns=rows,
        total=total,
        page=page,
        total_pages=total_pages,
        filter_status=filter_status,
        filter_date_from=date_from,
        filter_date_to=date_to,
        filter_search=search,
    )


@app.route('/walkdowns/add', methods=['GET', 'POST'])
def walkdown_add():
    if request.method == 'POST':
        inspector_name   = request.form.get('inspector_name', '').strip()
        walkdown_date    = request.form.get('walkdown_date', '').strip()
        area_location    = request.form.get('area_location', '').strip()
        findings         = request.form.get('findings', '').strip()
        actions_required = request.form.get('actions_required', '').strip()

        errors = []
        if not inspector_name: errors.append('Inspector name is required.')
        if not walkdown_date:  errors.append('Walkdown date is required.')
        if not area_location:  errors.append('Area / Location is required.')
        if walkdown_date:
            try:
                if date.fromisoformat(walkdown_date) > date.today():
                    errors.append('Walkdown date cannot be in the future.')
            except ValueError:
                errors.append('Invalid date format.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('walkdown_add'))

        execute_db("""
            INSERT INTO walkdowns (inspector_name, walkdown_date, area_location,
              findings, actions_required, status, date_logged)
            VALUES (?,?,?,?,?,'open',?)
        """, [inspector_name, walkdown_date, area_location,
              findings, actions_required, datetime.now().isoformat()])

        flash('Walkdown logged successfully.', 'success')
        return redirect(url_for('walkdown_list'))

    return render_template('walkdowns/add.html')


@app.route('/walkdowns/<int:wd_id>')
def walkdown_detail(wd_id):
    wd = query_db("SELECT * FROM walkdowns WHERE id = ?", [wd_id], one=True)
    if not wd:
        flash('Walkdown not found.', 'danger')
        return redirect(url_for('walkdown_list'))
    return render_template('walkdowns/detail.html', wd=wd)


@app.route('/walkdowns/<int:wd_id>/edit', methods=['GET', 'POST'])
def walkdown_edit(wd_id):
    wd = query_db("SELECT * FROM walkdowns WHERE id = ?", [wd_id], one=True)
    if not wd:
        flash('Walkdown not found.', 'danger')
        return redirect(url_for('walkdown_list'))

    if request.method == 'POST':
        inspector_name   = request.form.get('inspector_name', '').strip()
        walkdown_date    = request.form.get('walkdown_date', '').strip()
        area_location    = request.form.get('area_location', '').strip()
        findings         = request.form.get('findings', '').strip()
        actions_required = request.form.get('actions_required', '').strip()

        if not inspector_name or not walkdown_date or not area_location:
            flash('Inspector name, date, and area are required.', 'danger')
            return redirect(url_for('walkdown_edit', wd_id=wd_id))

        execute_db("""
            UPDATE walkdowns SET inspector_name=?, walkdown_date=?, area_location=?,
              findings=?, actions_required=? WHERE id=?
        """, [inspector_name, walkdown_date, area_location, findings, actions_required, wd_id])
        flash('Walkdown updated.', 'success')
        return redirect(url_for('walkdown_detail', wd_id=wd_id))

    return render_template('walkdowns/detail.html', wd=wd, edit_mode=True)


@app.route('/walkdowns/<int:wd_id>/toggle-status', methods=['POST'])
def walkdown_toggle_status(wd_id):
    wd = query_db("SELECT * FROM walkdowns WHERE id = ?", [wd_id], one=True)
    if not wd:
        flash('Walkdown not found.', 'danger')
        return redirect(url_for('walkdown_list'))
    new_status = 'closed' if wd['status'] == 'open' else 'open'
    execute_db("UPDATE walkdowns SET status=? WHERE id=?", [new_status, wd_id])
    flash(f'Walkdown marked as {new_status}.', 'success')
    return redirect(url_for('walkdown_detail', wd_id=wd_id))


@app.route('/walkdowns/<int:wd_id>/delete', methods=['POST'])
def walkdown_delete(wd_id):
    wd = query_db("SELECT * FROM walkdowns WHERE id = ?", [wd_id], one=True)
    if not wd:
        flash('Walkdown not found.', 'danger')
        return redirect(url_for('walkdown_list'))
    execute_db("DELETE FROM walkdowns WHERE id=?", [wd_id])
    flash('Walkdown record deleted.', 'success')
    return redirect(url_for('walkdown_list'))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
