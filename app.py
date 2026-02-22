import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, g, session, jsonify, Response

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me-in-production')
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

DATABASE_URL = os.environ.get('DATABASE_URL')
USE_POSTGRES = DATABASE_URL is not None

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

SQLITE_PATH = os.path.join(app.root_path, 'crm.db')


def get_db():
    if 'db' not in g:
        if USE_POSTGRES:
            g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)
        else:
            g.db = sqlite3.connect(SQLITE_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db


def query_db(sql, args=(), one=False, insert=False):
    db = get_db()
    if USE_POSTGRES:
        # Convert ? placeholders to %s for postgres
        sql = sql.replace('?', '%s')
        cur = db.execute(sql, args)
        if insert:
            if 'RETURNING' in sql.upper():
                row = cur.fetchone()
                return row['id'] if row else None
            return None
        if sql.strip().upper().startswith('SELECT'):
            rows = cur.fetchall()
            return rows[0] if one and rows else rows if not one else None
        return None
    else:
        cur = db.execute(sql, args)
        if insert:
            if 'RETURNING' not in sql.upper():
                return cur.lastrowid
            row = cur.fetchone()
            return row['id'] if row else None
        if sql.strip().upper().startswith('SELECT'):
            rows = cur.fetchall()
            return rows[0] if one and rows else rows if not one else None
        return None


def commit_db():
    db = get_db()
    db.commit()


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    if USE_POSTGRES:
        with app.open_resource('schema_pg.sql') as f:
            cur = db.cursor()
            cur.execute(f.read().decode('utf-8'))
            db.commit()
    else:
        with app.open_resource('schema.sql') as f:
            db.executescript(f.read().decode('utf-8'))
        # Migrations for existing SQLite databases
        for stmt in [
            'ALTER TABLE companies ADD COLUMN sort_order INTEGER DEFAULT 0',
            'ALTER TABLE individuals ADD COLUMN sort_order INTEGER DEFAULT 0',
            'ALTER TABLE follow_ups ADD COLUMN sort_order INTEGER DEFAULT 0',
            'ALTER TABLE follow_ups ADD COLUMN opp_type TEXT DEFAULT \'TBD\'',
            'ALTER TABLE follow_ups ADD COLUMN priority_level INTEGER DEFAULT 0',
            'ALTER TABLE follow_ups ADD COLUMN priority_order INTEGER DEFAULT 0',
            'ALTER TABLE follow_ups ADD COLUMN closed_at TIMESTAMP',
            'ALTER TABLE proposals ADD COLUMN onboarding_fee REAL',
            'ALTER TABLE proposals ADD COLUMN monthly_retainer REAL',
            'ALTER TABLE proposals ADD COLUMN onboarding_fee_max REAL',
            'ALTER TABLE proposals ADD COLUMN monthly_retainer_max REAL',
        ]:
            try:
                db.execute(stmt)
                db.commit()
            except Exception:
                pass


with app.app_context():
    init_db()


@app.template_filter('datefmt')
def datefmt(value):
    if not value:
        return ''
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
            try:
                value = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return value
    # Convert UTC to Pacific Time (UTC-8, or UTC-7 during DST)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    pacific = timezone(timedelta(hours=-8))
    value = value.astimezone(pacific)
    return value.strftime('%b %d, %Y %I:%M %p')


# --- Auth ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not APP_PASSWORD:
        return redirect(url_for('index'))
    if request.method == 'POST':
        if request.form['password'] == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('Incorrect password.', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('Logged out.', 'success')
    return redirect(url_for('login'))


# --- Home / Search ---

@app.route('/')
@login_required
def index():
    q = request.args.get('q', '').strip()

    def load_follow_up_data(fu_list):
        result = []
        for fu in fu_list:
            links = query_db('SELECT * FROM follow_up_links WHERE follow_up_id = ?', (fu['id'],))
            linked_entities = []
            for link in links:
                if link['entity_type'] == 'company':
                    entity = query_db('SELECT id, name FROM companies WHERE id = ?', (link['entity_id'],), one=True)
                else:
                    entity = query_db('SELECT id, name FROM individuals WHERE id = ?', (link['entity_id'],), one=True)
                if entity:
                    linked_entities.append({'type': link['entity_type'], 'id': entity['id'], 'name': entity['name']})
            comments = query_db(
                'SELECT * FROM follow_up_comments WHERE follow_up_id = ? ORDER BY created_at ASC', (fu['id'],)
            )
            linked_proposals = query_db(
                'SELECT id, name, status FROM proposals WHERE follow_up_id = ?', (fu['id'],)
            )
            result.append({'follow_up': fu, 'links': linked_entities, 'comments': comments, 'proposals': linked_proposals})
        return result

    if q:
        follow_ups = query_db(
            'SELECT * FROM follow_ups WHERE closed_at IS NULL AND (title LIKE ? OR body LIKE ?) ORDER BY sort_order, created_at DESC',
            (f'%{q}%', f'%{q}%')
        )
        follow_up_data = load_follow_up_data(follow_ups)
        priority_data = [item for item in follow_up_data if item['follow_up']['priority_level'] == 2]
        watch_data = [item for item in follow_up_data if item['follow_up']['priority_level'] == 1]
        closed_follow_ups = query_db(
            'SELECT * FROM follow_ups WHERE closed_at IS NOT NULL AND (title LIKE ? OR body LIKE ?) ORDER BY closed_at DESC',
            (f'%{q}%', f'%{q}%')
        )
    else:
        all_follow_ups = query_db('SELECT * FROM follow_ups WHERE closed_at IS NULL ORDER BY sort_order, created_at DESC')
        priority_follow_ups = query_db('SELECT * FROM follow_ups WHERE closed_at IS NULL AND priority_level = 2 ORDER BY priority_order, created_at DESC')
        watch_follow_ups = query_db('SELECT * FROM follow_ups WHERE closed_at IS NULL AND priority_level = 1 ORDER BY priority_order, created_at DESC')
        follow_up_data = load_follow_up_data(all_follow_ups)
        priority_data = load_follow_up_data(priority_follow_ups)
        watch_data = load_follow_up_data(watch_follow_ups)
        closed_follow_ups = query_db('SELECT * FROM follow_ups WHERE closed_at IS NOT NULL ORDER BY closed_at DESC')
    closed_data = load_follow_up_data(closed_follow_ups)

    return render_template('index.html', query=q,
                           follow_up_data=follow_up_data, priority_data=priority_data, watch_data=watch_data,
                           closed_data=closed_data)


# --- Company List ---

@app.route('/companies')
@login_required
def company_list():
    q = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'name')
    order = request.args.get('order', 'asc')
    allowed_sorts = {'name': 'name', 'type': 'type', 'created_at': 'created_at'}
    sort_col = allowed_sorts.get(sort, 'name')
    sort_dir = 'DESC' if order == 'desc' else 'ASC'
    if q:
        companies = query_db(
            f'SELECT * FROM companies WHERE name LIKE ? OR type LIKE ? ORDER BY {sort_col} {sort_dir}',
            (f'%{q}%', f'%{q}%')
        )
    else:
        companies = query_db(f'SELECT * FROM companies ORDER BY {sort_col} {sort_dir}')
    # Build relationship names for each company
    company_rels = {}
    for c in companies:
        rels = query_db(
            'SELECT r.from_type, r.from_id, r.to_type, r.to_id FROM relationships r '
            'WHERE (r.from_type = ? AND r.from_id = ?) OR (r.to_type = ? AND r.to_id = ?)',
            ('company', c['id'], 'company', c['id'])
        )
        names = []
        for rel in rels:
            if rel['from_type'] == 'company' and rel['from_id'] == c['id']:
                other_type, other_id = rel['to_type'], rel['to_id']
            else:
                other_type, other_id = rel['from_type'], rel['from_id']
            other = query_db(f'SELECT name FROM {"companies" if other_type == "company" else "individuals"} WHERE id = ?', (other_id,), one=True)
            if other:
                names.append(other['name'])
        company_rels[c['id']] = names
    return render_template('company_list.html', companies=companies, company_rels=company_rels, query=q, sort=sort, order=order)


# --- Individual List ---

@app.route('/individuals')
@login_required
def individual_list():
    q = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'name')
    order = request.args.get('order', 'asc')
    allowed_sorts = {'name': 'name', 'title': 'title', 'email': 'email', 'created_at': 'created_at'}
    sort_col = allowed_sorts.get(sort, 'name')
    sort_dir = 'DESC' if order == 'desc' else 'ASC'
    if q:
        individuals = query_db(
            f'SELECT * FROM individuals WHERE name LIKE ? OR title LIKE ? OR email LIKE ? ORDER BY {sort_col} {sort_dir}',
            (f'%{q}%', f'%{q}%', f'%{q}%')
        )
    else:
        individuals = query_db(f'SELECT * FROM individuals ORDER BY {sort_col} {sort_dir}')
    # Build relationship names for each individual
    individual_rels = {}
    for i in individuals:
        rels = query_db(
            'SELECT r.from_type, r.from_id, r.to_type, r.to_id FROM relationships r '
            'WHERE (r.from_type = ? AND r.from_id = ?) OR (r.to_type = ? AND r.to_id = ?)',
            ('individual', i['id'], 'individual', i['id'])
        )
        names = []
        for rel in rels:
            if rel['from_type'] == 'individual' and rel['from_id'] == i['id']:
                other_type, other_id = rel['to_type'], rel['to_id']
            else:
                other_type, other_id = rel['from_type'], rel['from_id']
            other = query_db(f'SELECT name FROM {"companies" if other_type == "company" else "individuals"} WHERE id = ?', (other_id,), one=True)
            if other:
                names.append(other['name'])
        individual_rels[i['id']] = names
    return render_template('individual_list.html', individuals=individuals, individual_rels=individual_rels, query=q, sort=sort, order=order)


# --- Companies ---

@app.route('/company/add', methods=['GET', 'POST'])
@login_required
def add_company():
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Company name is required.', 'error')
            return render_template('add_company.html')
        new_id = query_db(
            'INSERT INTO companies (name, website, type, linkedin_url, location) VALUES (?, ?, ?, ?, ?) RETURNING id',
            (name, request.form.get('website', '').strip(),
             request.form.get('type', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip()),
            insert=True
        )
        commit_db()
        flash('Company added.', 'success')
        return redirect(url_for('company_detail', id=new_id))
    return render_template('add_company.html')


@app.route('/company/<int:id>')
@login_required
def company_detail(id):
    company = query_db('SELECT * FROM companies WHERE id = ?', (id,), one=True)
    if not company:
        flash('Company not found.', 'error')
        return redirect(url_for('index'))
    notes = query_db(
        "SELECT * FROM notes WHERE entity_type = 'company' AND entity_id = ? ORDER BY created_at DESC", (id,)
    )
    relationships = query_db(
        'SELECT * FROM relationships WHERE (from_type = ? AND from_id = ?) OR (to_type = ? AND to_id = ?)',
        ('company', id, 'company', id)
    )
    related = []
    for rel in relationships:
        if rel['from_type'] == 'company' and rel['from_id'] == id:
            other_type, other_id = rel['to_type'], rel['to_id']
        else:
            other_type, other_id = rel['from_type'], rel['from_id']
        if other_type == 'company':
            other = query_db('SELECT * FROM companies WHERE id = ?', (other_id,), one=True)
        else:
            other = query_db('SELECT * FROM individuals WHERE id = ?', (other_id,), one=True)
        if other:
            related.append({'rel': rel, 'other': other, 'other_type': other_type})
    all_companies = query_db('SELECT id, name FROM companies WHERE id != ? ORDER BY name', (id,))
    all_individuals = query_db('SELECT id, name FROM individuals ORDER BY name')
    follow_ups = get_follow_ups_for_entity('company', id)
    return render_template('company_detail.html', company=company, notes=notes, related=related,
                           all_companies=all_companies, all_individuals=all_individuals,
                           follow_ups=follow_ups)


@app.route('/company/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_company(id):
    company = query_db('SELECT * FROM companies WHERE id = ?', (id,), one=True)
    if not company:
        flash('Company not found.', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Company name is required.', 'error')
            return render_template('edit_company.html', company=company)
        query_db(
            'UPDATE companies SET name=?, website=?, type=?, linkedin_url=?, location=? WHERE id=?',
            (name, request.form.get('website', '').strip(),
             request.form.get('type', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip(), id)
        )
        commit_db()
        flash('Company updated.', 'success')
        return redirect(url_for('company_detail', id=id))
    return render_template('edit_company.html', company=company)


@app.route('/company/<int:id>/delete', methods=['POST'])
@login_required
def delete_company(id):
    query_db('DELETE FROM companies WHERE id = ?', (id,))
    query_db("DELETE FROM notes WHERE entity_type = 'company' AND entity_id = ?", (id,))
    query_db(
        "DELETE FROM relationships WHERE (from_type = 'company' AND from_id = ?) OR (to_type = 'company' AND to_id = ?)",
        (id, id)
    )
    commit_db()
    flash('Company deleted.', 'success')
    return redirect(url_for('index'))


# --- Individuals ---

@app.route('/individual/add', methods=['GET', 'POST'])
@login_required
def add_individual():
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Name is required.', 'error')
            return render_template('add_individual.html')
        new_id = query_db(
            'INSERT INTO individuals (name, title, email, phone, linkedin_url, location) VALUES (?, ?, ?, ?, ?, ?) RETURNING id',
            (name, request.form.get('title', '').strip(),
             request.form.get('email', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip()),
            insert=True
        )
        commit_db()
        flash('Individual added.', 'success')
        return redirect(url_for('individual_detail', id=new_id))
    return render_template('add_individual.html')


@app.route('/individual/<int:id>')
@login_required
def individual_detail(id):
    individual = query_db('SELECT * FROM individuals WHERE id = ?', (id,), one=True)
    if not individual:
        flash('Individual not found.', 'error')
        return redirect(url_for('index'))
    notes = query_db(
        "SELECT * FROM notes WHERE entity_type = 'individual' AND entity_id = ? ORDER BY created_at DESC", (id,)
    )
    relationships = query_db(
        'SELECT * FROM relationships WHERE (from_type = ? AND from_id = ?) OR (to_type = ? AND to_id = ?)',
        ('individual', id, 'individual', id)
    )
    related = []
    for rel in relationships:
        if rel['from_type'] == 'individual' and rel['from_id'] == id:
            other_type, other_id = rel['to_type'], rel['to_id']
        else:
            other_type, other_id = rel['from_type'], rel['from_id']
        if other_type == 'company':
            other = query_db('SELECT * FROM companies WHERE id = ?', (other_id,), one=True)
        else:
            other = query_db('SELECT * FROM individuals WHERE id = ?', (other_id,), one=True)
        if other:
            related.append({'rel': rel, 'other': other, 'other_type': other_type})
    all_companies = query_db('SELECT id, name FROM companies ORDER BY name')
    all_individuals = query_db('SELECT id, name FROM individuals WHERE id != ? ORDER BY name', (id,))
    follow_ups = get_follow_ups_for_entity('individual', id)
    return render_template('individual_detail.html', individual=individual, notes=notes, related=related,
                           all_companies=all_companies, all_individuals=all_individuals,
                           follow_ups=follow_ups)


@app.route('/individual/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_individual(id):
    individual = query_db('SELECT * FROM individuals WHERE id = ?', (id,), one=True)
    if not individual:
        flash('Individual not found.', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Name is required.', 'error')
            return render_template('edit_individual.html', individual=individual)
        query_db(
            'UPDATE individuals SET name=?, title=?, email=?, phone=?, linkedin_url=?, location=? WHERE id=?',
            (name, request.form.get('title', '').strip(),
             request.form.get('email', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip(), id)
        )
        commit_db()
        flash('Individual updated.', 'success')
        return redirect(url_for('individual_detail', id=id))
    return render_template('edit_individual.html', individual=individual)


@app.route('/individual/<int:id>/delete', methods=['POST'])
@login_required
def delete_individual(id):
    query_db('DELETE FROM individuals WHERE id = ?', (id,))
    query_db("DELETE FROM notes WHERE entity_type = 'individual' AND entity_id = ?", (id,))
    query_db(
        "DELETE FROM relationships WHERE (from_type = 'individual' AND from_id = ?) OR (to_type = 'individual' AND to_id = ?)",
        (id, id)
    )
    commit_db()
    flash('Individual deleted.', 'success')
    return redirect(url_for('index'))


# --- Notes ---

@app.route('/note/add', methods=['POST'])
@login_required
def add_note():
    entity_type = request.form['entity_type']
    entity_id = int(request.form['entity_id'])
    note_text = request.form['note_text'].strip()
    if not note_text:
        flash('Note text is required.', 'error')
    else:
        query_db(
            'INSERT INTO notes (entity_type, entity_id, note_text) VALUES (?, ?, ?)',
            (entity_type, entity_id, note_text)
        )
        commit_db()
        flash('Note added.', 'success')
    if entity_type == 'company':
        return redirect(url_for('company_detail', id=entity_id))
    return redirect(url_for('individual_detail', id=entity_id))


@app.route('/note/<int:id>/delete', methods=['POST'])
@login_required
def delete_note(id):
    note = query_db('SELECT * FROM notes WHERE id = ?', (id,), one=True)
    if note:
        query_db('DELETE FROM notes WHERE id = ?', (id,))
        commit_db()
        flash('Note deleted.', 'success')
        if note['entity_type'] == 'company':
            return redirect(url_for('company_detail', id=note['entity_id']))
        return redirect(url_for('individual_detail', id=note['entity_id']))
    flash('Note not found.', 'error')
    return redirect(url_for('index'))


# --- Relationships ---

@app.route('/relationship/add', methods=['POST'])
@login_required
def add_relationship():
    from_type = request.form['from_type']
    from_id = int(request.form['from_id'])
    to_type = request.form['to_type']
    to_id = int(request.form['to_id'])
    relationship_type = request.form['relationship_type'].strip()
    if not relationship_type:
        flash('Relationship type is required.', 'error')
    else:
        query_db(
            'INSERT INTO relationships (from_type, from_id, to_type, to_id, relationship_type) VALUES (?, ?, ?, ?, ?)',
            (from_type, from_id, to_type, to_id, relationship_type)
        )
        commit_db()
        flash('Relationship added.', 'success')
    if from_type == 'company':
        return redirect(url_for('company_detail', id=from_id))
    return redirect(url_for('individual_detail', id=from_id))


@app.route('/relationship/<int:id>/delete', methods=['POST'])
@login_required
def delete_relationship(id):
    rel = query_db('SELECT * FROM relationships WHERE id = ?', (id,), one=True)
    if rel:
        query_db('DELETE FROM relationships WHERE id = ?', (id,))
        commit_db()
        flash('Relationship deleted.', 'success')
        redirect_type = request.form.get('redirect_type', rel['from_type'])
        redirect_id = int(request.form.get('redirect_id', rel['from_id']))
        if redirect_type == 'company':
            return redirect(url_for('company_detail', id=redirect_id))
        return redirect(url_for('individual_detail', id=redirect_id))
    flash('Relationship not found.', 'error')
    return redirect(url_for('index'))


# --- Follow-Ups ---

@app.route('/follow-up/new', methods=['GET'])
@login_required
def add_follow_up_page():
    all_companies = query_db('SELECT id, name FROM companies ORDER BY name')
    all_individuals = query_db('SELECT id, name FROM individuals ORDER BY name')
    return render_template('add_follow_up.html', all_companies=all_companies, all_individuals=all_individuals)


@app.route('/follow-up/add', methods=['POST'])
@login_required
def add_follow_up():
    title = request.form['title'].strip()
    body = request.form.get('body', '').strip()
    opp_type = request.form.get('opp_type', 'TBD').strip()
    if not title:
        flash('Name is required.', 'error')
        return redirect(url_for('index'))
    fu_id = query_db('INSERT INTO follow_ups (title, body, opp_type) VALUES (?, ?, ?) RETURNING id',
                      (title, body, opp_type), insert=True)
    linked_companies = request.form.getlist('link_companies')
    for cid in linked_companies:
        query_db('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                 (fu_id, 'company', int(cid)))
    linked_individuals = request.form.getlist('link_individuals')
    for iid in linked_individuals:
        query_db('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                 (fu_id, 'individual', int(iid)))
    commit_db()
    flash('Opportunity created.', 'success')
    return redirect(url_for('index'))


@app.route('/follow-up/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_follow_up(id):
    fu = query_db('SELECT * FROM follow_ups WHERE id = ?', (id,), one=True)
    if not fu:
        flash('Opportunity not found.', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        title = request.form['title'].strip()
        body = request.form.get('body', '').strip()
        if not title:
            flash('Name is required.', 'error')
            return redirect(url_for('edit_follow_up', id=id))
        opp_type = request.form.get('opp_type', 'TBD').strip()
        query_db('UPDATE follow_ups SET title=?, body=?, opp_type=? WHERE id=?', (title, body, opp_type, id))
        # Replace all links
        query_db('DELETE FROM follow_up_links WHERE follow_up_id = ?', (id,))
        for cid in request.form.getlist('link_companies'):
            query_db('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                     (id, 'company', int(cid)))
        for iid in request.form.getlist('link_individuals'):
            query_db('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                     (id, 'individual', int(iid)))
        commit_db()
        flash('Opportunity updated.', 'success')
        return redirect(url_for('index') + f'#follow-up-{id}')
    # Get current links
    current_links = query_db('SELECT * FROM follow_up_links WHERE follow_up_id = ?', (id,))
    linked_company_ids = [l['entity_id'] for l in current_links if l['entity_type'] == 'company']
    linked_individual_ids = [l['entity_id'] for l in current_links if l['entity_type'] == 'individual']
    all_companies = query_db('SELECT id, name FROM companies ORDER BY name')
    all_individuals = query_db('SELECT id, name FROM individuals ORDER BY name')
    return render_template('edit_follow_up.html', follow_up=fu,
                           all_companies=all_companies, all_individuals=all_individuals,
                           linked_company_ids=linked_company_ids, linked_individual_ids=linked_individual_ids)


@app.route('/follow-up/<int:id>/comment', methods=['POST'])
@login_required
def add_follow_up_comment(id):
    comment_text = request.form['comment_text'].strip()
    if not comment_text:
        flash('Comment text is required.', 'error')
    else:
        query_db('INSERT INTO follow_up_comments (follow_up_id, comment_text) VALUES (?, ?)',
                 (id, comment_text))
        commit_db()
        flash('Comment added.', 'success')
    return redirect(url_for('index') + f'#follow-up-{id}')


@app.route('/follow-up/<int:id>/update-body', methods=['POST'])
@login_required
def update_follow_up_body(id):
    fu = query_db('SELECT * FROM follow_ups WHERE id = ?', (id,), one=True)
    if not fu:
        flash('Opportunity not found.', 'error')
        return redirect(url_for('index'))
    body = request.form.get('body', '').strip()
    query_db('UPDATE follow_ups SET body = ? WHERE id = ?', (body, id))
    commit_db()
    flash('Notes updated.', 'success')
    return redirect(url_for('index') + f'#follow-up-{id}')


@app.route('/follow-up/comment/<int:id>/edit', methods=['POST'])
@login_required
def edit_follow_up_comment(id):
    comment = query_db('SELECT * FROM follow_up_comments WHERE id = ?', (id,), one=True)
    if not comment:
        flash('Comment not found.', 'error')
        return redirect(url_for('index'))
    comment_text = request.form['comment_text'].strip()
    if not comment_text:
        flash('Comment text is required.', 'error')
    else:
        query_db('UPDATE follow_up_comments SET comment_text = ? WHERE id = ?', (comment_text, id))
        commit_db()
        flash('Comment updated.', 'success')
    return redirect(url_for('index') + f'#follow-up-{comment["follow_up_id"]}')


@app.route('/follow-up/comment/<int:id>/delete', methods=['POST'])
@login_required
def delete_follow_up_comment(id):
    comment = query_db('SELECT * FROM follow_up_comments WHERE id = ?', (id,), one=True)
    if comment:
        fu_id = comment['follow_up_id']
        query_db('DELETE FROM follow_up_comments WHERE id = ?', (id,))
        commit_db()
        flash('Comment deleted.', 'success')
        return redirect(url_for('index') + f'#follow-up-{fu_id}')
    flash('Comment not found.', 'error')
    return redirect(url_for('index'))


@app.route('/follow-up/<int:id>/delete', methods=['POST'])
@login_required
def delete_follow_up(id):
    query_db('DELETE FROM follow_up_comments WHERE follow_up_id = ?', (id,))
    query_db('DELETE FROM follow_up_links WHERE follow_up_id = ?', (id,))
    query_db('DELETE FROM follow_ups WHERE id = ?', (id,))
    commit_db()
    flash('Opportunity deleted.', 'success')
    return redirect(url_for('index'))


def get_follow_ups_for_entity(entity_type, entity_id):
    fu_ids = query_db(
        'SELECT follow_up_id FROM follow_up_links WHERE entity_type = ? AND entity_id = ?',
        (entity_type, entity_id)
    )
    result = []
    for row in fu_ids:
        fu = query_db('SELECT * FROM follow_ups WHERE id = ?', (row['follow_up_id'],), one=True)
        if fu:
            comments = query_db(
                'SELECT * FROM follow_up_comments WHERE follow_up_id = ? ORDER BY created_at ASC',
                (fu['id'],)
            )
            links = query_db('SELECT * FROM follow_up_links WHERE follow_up_id = ?', (fu['id'],))
            linked_entities = []
            for link in links:
                if link['entity_type'] == 'company':
                    entity = query_db('SELECT id, name FROM companies WHERE id = ?', (link['entity_id'],), one=True)
                else:
                    entity = query_db('SELECT id, name FROM individuals WHERE id = ?', (link['entity_id'],), one=True)
                if entity:
                    linked_entities.append({'type': link['entity_type'], 'id': entity['id'], 'name': entity['name']})
            result.append({'follow_up': fu, 'links': linked_entities, 'comments': comments})
    return result


# --- Proposals ---

@app.route('/proposals')
@login_required
def proposals():
    draft = query_db("SELECT * FROM proposals WHERE status = 'Draft' ORDER BY sort_order, created_at DESC")
    sent = query_db("SELECT * FROM proposals WHERE status = 'Sent' ORDER BY sort_order, created_at DESC")
    negotiating = query_db("SELECT * FROM proposals WHERE status = 'Negotiating' ORDER BY sort_order, created_at DESC")
    won = query_db("SELECT * FROM proposals WHERE status = 'Won' ORDER BY created_at DESC")
    lost = query_db("SELECT * FROM proposals WHERE status = 'Lost' ORDER BY created_at DESC")

    # Load linked opportunity names and contacts for each proposal
    def enrich(proposal_list):
        result = []
        for p in proposal_list:
            pd = dict(p)
            if p['follow_up_id']:
                fu = query_db('SELECT title FROM follow_ups WHERE id = ?', (p['follow_up_id'],), one=True)
                pd['opportunity_name'] = fu['title'] if fu else None
            else:
                pd['opportunity_name'] = None
            # Load linked contacts
            contacts = query_db(
                'SELECT i.id, i.name FROM proposal_contacts pc JOIN individuals i ON pc.individual_id = i.id WHERE pc.proposal_id = ?',
                (p['id'],)
            )
            pd['contacts'] = [{'id': c['id'], 'name': c['name']} for c in contacts]
            result.append(pd)
        return result

    return render_template('proposals.html',
                           draft=enrich(draft), sent=enrich(sent), negotiating=enrich(negotiating),
                           won=enrich(won), lost=enrich(lost))


@app.route('/proposal/add', methods=['GET', 'POST'])
@login_required
def add_proposal():
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Proposal name is required.', 'error')
            follow_ups_list = query_db('SELECT id, title FROM follow_ups ORDER BY title')
            individuals_list = query_db('SELECT id, name FROM individuals ORDER BY name')
            return render_template('add_proposal.html', follow_ups=follow_ups_list, individuals=individuals_list)
        follow_up_id = request.form.get('follow_up_id') or None
        if follow_up_id:
            follow_up_id = int(follow_up_id)
        onboarding_fee = request.form.get('onboarding_fee', '').strip()
        onboarding_fee = float(onboarding_fee) if onboarding_fee else None
        onboarding_fee_max = request.form.get('onboarding_fee_max', '').strip()
        onboarding_fee_max = float(onboarding_fee_max) if onboarding_fee_max else None
        monthly_retainer = request.form.get('monthly_retainer', '').strip()
        monthly_retainer = float(monthly_retainer) if monthly_retainer else None
        monthly_retainer_max = request.form.get('monthly_retainer_max', '').strip()
        monthly_retainer_max = float(monthly_retainer_max) if monthly_retainer_max else None
        status = request.form.get('status', 'Draft')
        date_sent = request.form.get('date_sent', '').strip() or None
        notes = request.form.get('notes', '').strip() or None
        scope_of_work = request.form.get('scope_of_work', '').strip() or None
        timeline = request.form.get('timeline', '').strip() or None
        contact_person = request.form.get('contact_person', '').strip() or None
        follow_up_date = request.form.get('follow_up_date', '').strip() or None
        # Multi-contact: set contact_person from first selected individual
        contact_individuals = request.form.getlist('contact_individuals')
        if contact_individuals and not contact_person:
            ind = query_db('SELECT name FROM individuals WHERE id = ?', (int(contact_individuals[0]),), one=True)
            if ind:
                contact_person = ind['name']
        proposal_id = query_db(
            'INSERT INTO proposals (name, follow_up_id, onboarding_fee, onboarding_fee_max, monthly_retainer, monthly_retainer_max, status, date_sent, notes, scope_of_work, timeline, contact_person, follow_up_date) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id',
            (name, follow_up_id, onboarding_fee, onboarding_fee_max, monthly_retainer, monthly_retainer_max, status, date_sent, notes, scope_of_work, timeline, contact_person, follow_up_date),
            insert=True
        )
        # Insert proposal_contacts
        for iid in contact_individuals:
            query_db('INSERT INTO proposal_contacts (proposal_id, individual_id) VALUES (?, ?)',
                     (proposal_id, int(iid)))
        commit_db()
        flash('Proposal created.', 'success')
        return redirect(url_for('proposals'))
    follow_ups_list = query_db('SELECT id, title FROM follow_ups ORDER BY title')
    individuals_list = query_db('SELECT id, name FROM individuals ORDER BY name')
    return render_template('add_proposal.html', follow_ups=follow_ups_list, individuals=individuals_list)


@app.route('/proposal/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_proposal(id):
    proposal = query_db('SELECT * FROM proposals WHERE id = ?', (id,), one=True)
    if not proposal:
        flash('Proposal not found.', 'error')
        return redirect(url_for('proposals'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Proposal name is required.', 'error')
            follow_ups_list = query_db('SELECT id, title FROM follow_ups ORDER BY title')
            individuals_list = query_db('SELECT id, name FROM individuals ORDER BY name')
            linked_contact_ids = [row['individual_id'] for row in query_db('SELECT individual_id FROM proposal_contacts WHERE proposal_id = ?', (id,))]
            return render_template('edit_proposal.html', proposal=proposal, follow_ups=follow_ups_list, individuals=individuals_list, linked_contact_ids=linked_contact_ids)
        follow_up_id = request.form.get('follow_up_id') or None
        if follow_up_id:
            follow_up_id = int(follow_up_id)
        onboarding_fee = request.form.get('onboarding_fee', '').strip()
        onboarding_fee = float(onboarding_fee) if onboarding_fee else None
        onboarding_fee_max = request.form.get('onboarding_fee_max', '').strip()
        onboarding_fee_max = float(onboarding_fee_max) if onboarding_fee_max else None
        monthly_retainer = request.form.get('monthly_retainer', '').strip()
        monthly_retainer = float(monthly_retainer) if monthly_retainer else None
        monthly_retainer_max = request.form.get('monthly_retainer_max', '').strip()
        monthly_retainer_max = float(monthly_retainer_max) if monthly_retainer_max else None
        status = request.form.get('status', 'Draft')
        date_sent = request.form.get('date_sent', '').strip() or None
        notes = request.form.get('notes', '').strip() or None
        scope_of_work = request.form.get('scope_of_work', '').strip() or None
        timeline = request.form.get('timeline', '').strip() or None
        contact_person = request.form.get('contact_person', '').strip() or None
        follow_up_date = request.form.get('follow_up_date', '').strip() or None
        # Multi-contact: set contact_person from first selected individual
        contact_individuals = request.form.getlist('contact_individuals')
        if contact_individuals and not contact_person:
            ind = query_db('SELECT name FROM individuals WHERE id = ?', (int(contact_individuals[0]),), one=True)
            if ind:
                contact_person = ind['name']
        query_db(
            'UPDATE proposals SET name=?, follow_up_id=?, onboarding_fee=?, onboarding_fee_max=?, monthly_retainer=?, monthly_retainer_max=?, status=?, date_sent=?, notes=?, scope_of_work=?, timeline=?, contact_person=?, follow_up_date=? WHERE id=?',
            (name, follow_up_id, onboarding_fee, onboarding_fee_max, monthly_retainer, monthly_retainer_max, status, date_sent, notes, scope_of_work, timeline, contact_person, follow_up_date, id)
        )
        # Replace proposal_contacts
        query_db('DELETE FROM proposal_contacts WHERE proposal_id = ?', (id,))
        for iid in contact_individuals:
            query_db('INSERT INTO proposal_contacts (proposal_id, individual_id) VALUES (?, ?)',
                     (id, int(iid)))
        # Auto-close linked opportunity when proposal is Won or Lost
        if status in ('Won', 'Lost') and follow_up_id:
            query_db('UPDATE follow_ups SET closed_at = CURRENT_TIMESTAMP WHERE id = ? AND closed_at IS NULL',
                     (follow_up_id,))
        commit_db()
        flash('Proposal updated.', 'success')
        return redirect(url_for('proposals'))
    follow_ups_list = query_db('SELECT id, title FROM follow_ups ORDER BY title')
    individuals_list = query_db('SELECT id, name FROM individuals ORDER BY name')
    linked_contact_ids = [row['individual_id'] for row in query_db('SELECT individual_id FROM proposal_contacts WHERE proposal_id = ?', (id,))]
    return render_template('edit_proposal.html', proposal=proposal, follow_ups=follow_ups_list, individuals=individuals_list, linked_contact_ids=linked_contact_ids)


@app.route('/proposal/<int:id>/delete', methods=['POST'])
@login_required
def delete_proposal(id):
    query_db('DELETE FROM proposal_contacts WHERE proposal_id = ?', (id,))
    query_db('DELETE FROM proposals WHERE id = ?', (id,))
    commit_db()
    flash('Proposal deleted.', 'success')
    return redirect(url_for('proposals'))


@app.route('/proposal/<int:id>/update-status', methods=['POST'])
@login_required
def update_proposal_status(id):
    new_status = request.form.get('status')
    if new_status not in ('Draft', 'Sent', 'Negotiating', 'Won', 'Lost'):
        flash('Invalid status.', 'error')
        return redirect(url_for('proposals'))
    query_db('UPDATE proposals SET status = ? WHERE id = ?', (new_status, id))
    # Auto-close linked opportunity when proposal is Won or Lost
    if new_status in ('Won', 'Lost'):
        proposal = query_db('SELECT follow_up_id FROM proposals WHERE id = ?', (id,), one=True)
        if proposal and proposal['follow_up_id']:
            query_db('UPDATE follow_ups SET closed_at = CURRENT_TIMESTAMP WHERE id = ? AND closed_at IS NULL',
                     (proposal['follow_up_id'],))
    commit_db()
    return redirect(url_for('proposals'))


@app.route('/proposals/reorder', methods=['POST'])
@login_required
def reorder_proposals():
    data = request.get_json()
    ids = data.get('ids', [])
    for i, item_id in enumerate(ids):
        query_db('UPDATE proposals SET sort_order = ? WHERE id = ?', (i, int(item_id)))
    commit_db()
    return jsonify({'ok': True})


# --- Reorder ---

@app.route('/reorder', methods=['POST'])
@login_required
def reorder():
    data = request.get_json()
    list_type = data.get('type')
    ids = data.get('ids', [])
    if list_type not in ('companies', 'individuals', 'follow_ups', 'priority_follow_ups', 'watch_follow_ups', 'proposals'):
        return jsonify({'error': 'Invalid type'}), 400
    if list_type in ('priority_follow_ups', 'watch_follow_ups'):
        for i, item_id in enumerate(ids):
            query_db('UPDATE follow_ups SET priority_order = ? WHERE id = ?', (i, int(item_id)))
    else:
        for i, item_id in enumerate(ids):
            query_db(f'UPDATE {list_type} SET sort_order = ? WHERE id = ?', (i, int(item_id)))
    commit_db()
    return jsonify({'ok': True})


# --- Priority ---

@app.route('/follow-up/<int:id>/set-priority/<int:level>', methods=['POST'])
@login_required
def set_priority(id, level):
    fu = query_db('SELECT priority_level FROM follow_ups WHERE id = ?', (id,), one=True)
    if fu:
        # If already at this level, toggle off; otherwise set to the new level
        new_level = 0 if fu['priority_level'] == level else level
        query_db('UPDATE follow_ups SET priority_level = ? WHERE id = ?', (new_level, id))
        commit_db()
    return redirect(url_for('index') + f'#follow-up-{id}')


@app.route('/follow-up/<int:id>/toggle-close', methods=['POST'])
@login_required
def toggle_close_follow_up(id):
    fu = query_db('SELECT closed_at FROM follow_ups WHERE id = ?', (id,), one=True)
    if fu:
        if fu['closed_at'] is None:
            query_db('UPDATE follow_ups SET closed_at = CURRENT_TIMESTAMP WHERE id = ?', (id,))
        else:
            query_db('UPDATE follow_ups SET closed_at = NULL WHERE id = ?', (id,))
        commit_db()
    return redirect(url_for('index'))


@app.route('/follow-up/<int:id>/convert-to-proposal', methods=['POST'])
@login_required
def convert_to_proposal(id):
    fu = query_db('SELECT * FROM follow_ups WHERE id = ?', (id,), one=True)
    if not fu:
        flash('Opportunity not found.', 'error')
        return redirect(url_for('index'))
    # Create draft proposal from opportunity
    proposal_id = query_db(
        'INSERT INTO proposals (name, follow_up_id, status, notes) VALUES (?, ?, ?, ?) RETURNING id',
        (fu['title'], id, 'Draft', fu['body']),
        insert=True
    )
    # Copy linked individuals to proposal_contacts
    links = query_db("SELECT entity_id FROM follow_up_links WHERE follow_up_id = ? AND entity_type = 'individual'", (id,))
    first_contact_name = None
    for link in links:
        query_db('INSERT INTO proposal_contacts (proposal_id, individual_id) VALUES (?, ?)',
                 (proposal_id, link['entity_id']))
        if first_contact_name is None:
            ind = query_db('SELECT name FROM individuals WHERE id = ?', (link['entity_id'],), one=True)
            if ind:
                first_contact_name = ind['name']
    # Set contact_person for backward compat
    if first_contact_name:
        query_db('UPDATE proposals SET contact_person = ? WHERE id = ?', (first_contact_name, proposal_id))
    commit_db()
    flash('Proposal created from opportunity.', 'success')
    return redirect(url_for('edit_proposal', id=proposal_id))


# --- Export / Import ---

def serialize_row(row):
    """Convert a database row to a JSON-safe dict."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


@app.route('/export')
@login_required
def export_data():
    tables = ['companies', 'individuals', 'relationships', 'notes',
              'follow_ups', 'follow_up_links', 'follow_up_comments', 'proposals', 'proposal_contacts']
    data = {}
    for table in tables:
        rows = query_db(f'SELECT * FROM {table}')
        data[table] = [serialize_row(r) for r in rows]
    output = json.dumps(data, indent=2, default=str)
    return Response(
        output,
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=mini-crm-backup.json'}
    )


@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_data():
    if request.method == 'POST':
        file = request.files.get('file')
        if not file:
            flash('No file selected.', 'error')
            return redirect(url_for('import_data'))
        try:
            data = json.load(file)
        except json.JSONDecodeError:
            flash('Invalid JSON file.', 'error')
            return redirect(url_for('import_data'))

        # Clear existing data in reverse dependency order
        for table in ['proposal_contacts', 'proposals', 'follow_up_comments', 'follow_up_links', 'follow_ups',
                      'notes', 'relationships', 'individuals', 'companies']:
            query_db(f'DELETE FROM {table}')

        # Insert data
        for c in data.get('companies', []):
            query_db('INSERT INTO companies (id, name, website, type, linkedin_url, location, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (c['id'], c['name'], c.get('website'), c.get('type'), c.get('linkedin_url'), c.get('location'), c.get('created_at')))
        for i in data.get('individuals', []):
            query_db('INSERT INTO individuals (id, name, title, email, phone, linkedin_url, location, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                     (i['id'], i['name'], i.get('title'), i.get('email'), i.get('phone'), i.get('linkedin_url'), i.get('location'), i.get('created_at')))
        for r in data.get('relationships', []):
            query_db('INSERT INTO relationships (id, from_type, from_id, to_type, to_id, relationship_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (r['id'], r['from_type'], r['from_id'], r['to_type'], r['to_id'], r['relationship_type'], r.get('created_at')))
        for n in data.get('notes', []):
            query_db('INSERT INTO notes (id, entity_type, entity_id, note_text, created_at) VALUES (?, ?, ?, ?, ?)',
                     (n['id'], n['entity_type'], n['entity_id'], n['note_text'], n.get('created_at')))
        for fu in data.get('follow_ups', []):
            query_db('INSERT INTO follow_ups (id, title, body, opp_type, closed_at, sort_order, priority_level, priority_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (fu['id'], fu['title'], fu.get('body'), fu.get('opp_type', 'TBD'), fu.get('closed_at'),
                      fu.get('sort_order', 0), fu.get('priority_level', 0), fu.get('priority_order', 0), fu.get('created_at')))
        for fl in data.get('follow_up_links', []):
            query_db('INSERT INTO follow_up_links (id, follow_up_id, entity_type, entity_id) VALUES (?, ?, ?, ?)',
                     (fl['id'], fl['follow_up_id'], fl['entity_type'], fl['entity_id']))
        for fc in data.get('follow_up_comments', []):
            query_db('INSERT INTO follow_up_comments (id, follow_up_id, comment_text, created_at) VALUES (?, ?, ?, ?)',
                     (fc['id'], fc['follow_up_id'], fc['comment_text'], fc.get('created_at')))
        for pr in data.get('proposals', []):
            query_db('INSERT INTO proposals (id, name, follow_up_id, onboarding_fee, onboarding_fee_max, monthly_retainer, monthly_retainer_max, status, date_sent, notes, scope_of_work, timeline, contact_person, follow_up_date, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (pr['id'], pr['name'], pr.get('follow_up_id'), pr.get('onboarding_fee'), pr.get('onboarding_fee_max'),
                      pr.get('monthly_retainer'), pr.get('monthly_retainer_max'),
                      pr.get('status', 'Draft'), pr.get('date_sent'), pr.get('notes'), pr.get('scope_of_work'),
                      pr.get('timeline'), pr.get('contact_person'), pr.get('follow_up_date'), pr.get('sort_order', 0), pr.get('created_at')))

        for pc in data.get('proposal_contacts', []):
            query_db('INSERT INTO proposal_contacts (id, proposal_id, individual_id) VALUES (?, ?, ?)',
                     (pc['id'], pc['proposal_id'], pc['individual_id']))

        # Reset sequences for PostgreSQL
        if USE_POSTGRES:
            for table in ['companies', 'individuals', 'relationships', 'notes',
                          'follow_ups', 'follow_up_links', 'follow_up_comments', 'proposals', 'proposal_contacts']:
                query_db(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")

        commit_db()
        flash('Data imported successfully.', 'success')
        return redirect(url_for('index'))
    return render_template('import.html')


if __name__ == '__main__':
    app.run(debug=True)
