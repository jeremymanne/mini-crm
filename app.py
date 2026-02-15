import os
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, g, session

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
    import psycopg2
    import psycopg2.extras

SQLITE_PATH = os.path.join(app.root_path, 'crm.db')


def get_db():
    if 'db' not in g:
        if USE_POSTGRES:
            g.db = psycopg2.connect(DATABASE_URL)
            g.db.autocommit = False
        else:
            g.db = sqlite3.connect(SQLITE_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db


def query_db(sql, args=(), one=False, insert=False):
    db = get_db()
    if USE_POSTGRES:
        # Convert ? placeholders to %s for postgres
        sql = sql.replace('?', '%s')
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, args)
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


with app.app_context():
    init_db()


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
    if q:
        companies = query_db(
            'SELECT * FROM companies WHERE name LIKE ? ORDER BY name',
            (f'%{q}%',)
        )
        individuals = query_db(
            'SELECT * FROM individuals WHERE name LIKE ? ORDER BY name',
            (f'%{q}%',)
        )
    else:
        companies = query_db('SELECT * FROM companies ORDER BY created_at DESC LIMIT 20')
        individuals = query_db('SELECT * FROM individuals ORDER BY created_at DESC LIMIT 20')

    # Build relationship tags for each entity
    company_rels = {}
    for c in companies:
        rels = query_db(
            'SELECT r.relationship_type, r.from_type, r.from_id, r.to_type, r.to_id FROM relationships r '
            'WHERE (r.from_type = ? AND r.from_id = ?) OR (r.to_type = ? AND r.to_id = ?)',
            ('company', c['id'], 'company', c['id'])
        )
        tags = []
        for rel in rels:
            if rel['from_type'] == 'company' and rel['from_id'] == c['id']:
                other_type, other_id = rel['to_type'], rel['to_id']
            else:
                other_type, other_id = rel['from_type'], rel['from_id']
            if other_type == 'company':
                other = query_db('SELECT name FROM companies WHERE id = ?', (other_id,), one=True)
            else:
                other = query_db('SELECT name FROM individuals WHERE id = ?', (other_id,), one=True)
            if other:
                tags.append({'type': rel['relationship_type'], 'name': other['name']})
        company_rels[c['id']] = tags

    individual_rels = {}
    for i in individuals:
        rels = query_db(
            'SELECT r.relationship_type, r.from_type, r.from_id, r.to_type, r.to_id FROM relationships r '
            'WHERE (r.from_type = ? AND r.from_id = ?) OR (r.to_type = ? AND r.to_id = ?)',
            ('individual', i['id'], 'individual', i['id'])
        )
        tags = []
        for rel in rels:
            if rel['from_type'] == 'individual' and rel['from_id'] == i['id']:
                other_type, other_id = rel['to_type'], rel['to_id']
            else:
                other_type, other_id = rel['from_type'], rel['from_id']
            if other_type == 'company':
                other = query_db('SELECT name FROM companies WHERE id = ?', (other_id,), one=True)
            else:
                other = query_db('SELECT name FROM individuals WHERE id = ?', (other_id,), one=True)
            if other:
                tags.append({'type': rel['relationship_type'], 'name': other['name']})
        individual_rels[i['id']] = tags

    # Load follow-ups
    follow_ups = query_db('SELECT * FROM follow_ups ORDER BY created_at DESC')
    follow_up_data = []
    for fu in follow_ups:
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
        follow_up_data.append({'follow_up': fu, 'links': linked_entities, 'comments': comments})

    all_companies = query_db('SELECT id, name FROM companies ORDER BY name')
    all_individuals = query_db('SELECT id, name FROM individuals ORDER BY name')

    return render_template('index.html', companies=companies, individuals=individuals, query=q,
                           company_rels=company_rels, individual_rels=individual_rels,
                           follow_up_data=follow_up_data,
                           all_companies=all_companies, all_individuals=all_individuals)


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

@app.route('/follow-up/add', methods=['POST'])
@login_required
def add_follow_up():
    title = request.form['title'].strip()
    body = request.form.get('body', '').strip()
    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('index'))
    fu_id = query_db('INSERT INTO follow_ups (title, body) VALUES (?, ?) RETURNING id',
                      (title, body), insert=True)
    linked_companies = request.form.getlist('link_companies')
    for cid in linked_companies:
        query_db('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                 (fu_id, 'company', int(cid)))
    linked_individuals = request.form.getlist('link_individuals')
    for iid in linked_individuals:
        query_db('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                 (fu_id, 'individual', int(iid)))
    commit_db()
    flash('Follow-up created.', 'success')
    return redirect(url_for('index'))


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


@app.route('/follow-up/<int:id>/delete', methods=['POST'])
@login_required
def delete_follow_up(id):
    query_db('DELETE FROM follow_up_comments WHERE follow_up_id = ?', (id,))
    query_db('DELETE FROM follow_up_links WHERE follow_up_id = ?', (id,))
    query_db('DELETE FROM follow_ups WHERE id = ?', (id,))
    commit_db()
    flash('Follow-up deleted.', 'success')
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


if __name__ == '__main__':
    app.run(debug=True)
