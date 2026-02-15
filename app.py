import sqlite3
import os
from flask import Flask, render_template, request, redirect, url_for, flash, g

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me-in-production')
DATABASE = os.path.join(app.root_path, 'crm.db')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf-8'))


@app.cli.command('init-db')
def init_db_command():
    init_db()
    print('Database initialized.')


with app.app_context():
    init_db()


# --- Home / Search ---

@app.route('/')
def index():
    q = request.args.get('q', '').strip()
    companies = []
    individuals = []
    if q:
        db = get_db()
        companies = db.execute(
            'SELECT * FROM companies WHERE name LIKE ? ORDER BY name',
            (f'%{q}%',)
        ).fetchall()
        individuals = db.execute(
            'SELECT * FROM individuals WHERE name LIKE ? ORDER BY name',
            (f'%{q}%',)
        ).fetchall()
    else:
        db = get_db()
        companies = db.execute('SELECT * FROM companies ORDER BY created_at DESC LIMIT 20').fetchall()
        individuals = db.execute('SELECT * FROM individuals ORDER BY created_at DESC LIMIT 20').fetchall()
    # Build relationship tags for each entity
    db = get_db()
    company_rels = {}
    for c in companies:
        rels = db.execute(
            'SELECT r.relationship_type, r.from_type, r.from_id, r.to_type, r.to_id FROM relationships r '
            'WHERE (r.from_type = ? AND r.from_id = ?) OR (r.to_type = ? AND r.to_id = ?)',
            ('company', c['id'], 'company', c['id'])
        ).fetchall()
        tags = []
        for rel in rels:
            if rel['from_type'] == 'company' and rel['from_id'] == c['id']:
                other_type, other_id = rel['to_type'], rel['to_id']
            else:
                other_type, other_id = rel['from_type'], rel['from_id']
            if other_type == 'company':
                other = db.execute('SELECT name FROM companies WHERE id = ?', (other_id,)).fetchone()
            else:
                other = db.execute('SELECT name FROM individuals WHERE id = ?', (other_id,)).fetchone()
            if other:
                tags.append({'type': rel['relationship_type'], 'name': other['name']})
        company_rels[c['id']] = tags

    individual_rels = {}
    for i in individuals:
        rels = db.execute(
            'SELECT r.relationship_type, r.from_type, r.from_id, r.to_type, r.to_id FROM relationships r '
            'WHERE (r.from_type = ? AND r.from_id = ?) OR (r.to_type = ? AND r.to_id = ?)',
            ('individual', i['id'], 'individual', i['id'])
        ).fetchall()
        tags = []
        for rel in rels:
            if rel['from_type'] == 'individual' and rel['from_id'] == i['id']:
                other_type, other_id = rel['to_type'], rel['to_id']
            else:
                other_type, other_id = rel['from_type'], rel['from_id']
            if other_type == 'company':
                other = db.execute('SELECT name FROM companies WHERE id = ?', (other_id,)).fetchone()
            else:
                other = db.execute('SELECT name FROM individuals WHERE id = ?', (other_id,)).fetchone()
            if other:
                tags.append({'type': rel['relationship_type'], 'name': other['name']})
        individual_rels[i['id']] = tags

    # Load follow-ups
    follow_ups = db.execute('SELECT * FROM follow_ups ORDER BY created_at DESC').fetchall()
    follow_up_data = []
    for fu in follow_ups:
        links = db.execute(
            'SELECT * FROM follow_up_links WHERE follow_up_id = ?', (fu['id'],)
        ).fetchall()
        linked_entities = []
        for link in links:
            if link['entity_type'] == 'company':
                entity = db.execute('SELECT id, name FROM companies WHERE id = ?', (link['entity_id'],)).fetchone()
            else:
                entity = db.execute('SELECT id, name FROM individuals WHERE id = ?', (link['entity_id'],)).fetchone()
            if entity:
                linked_entities.append({'type': link['entity_type'], 'id': entity['id'], 'name': entity['name']})
        comments = db.execute(
            'SELECT * FROM follow_up_comments WHERE follow_up_id = ? ORDER BY created_at ASC', (fu['id'],)
        ).fetchall()
        follow_up_data.append({'follow_up': fu, 'links': linked_entities, 'comments': comments})

    all_companies = db.execute('SELECT id, name FROM companies ORDER BY name').fetchall()
    all_individuals = db.execute('SELECT id, name FROM individuals ORDER BY name').fetchall()

    return render_template('index.html', companies=companies, individuals=individuals, query=q,
                           company_rels=company_rels, individual_rels=individual_rels,
                           follow_up_data=follow_up_data,
                           all_companies=all_companies, all_individuals=all_individuals)


# --- Companies ---

@app.route('/company/add', methods=['GET', 'POST'])
def add_company():
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Company name is required.', 'error')
            return render_template('add_company.html')
        db = get_db()
        db.execute(
            'INSERT INTO companies (name, website, type, linkedin_url, location) VALUES (?, ?, ?, ?, ?)',
            (name, request.form.get('website', '').strip(),
             request.form.get('type', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip())
        )
        db.commit()
        flash('Company added.', 'success')
        return redirect(url_for('company_detail', id=db.execute('SELECT last_insert_rowid()').fetchone()[0]))
    return render_template('add_company.html')


@app.route('/company/<int:id>')
def company_detail(id):
    db = get_db()
    company = db.execute('SELECT * FROM companies WHERE id = ?', (id,)).fetchone()
    if not company:
        flash('Company not found.', 'error')
        return redirect(url_for('index'))
    notes = db.execute(
        "SELECT * FROM notes WHERE entity_type = 'company' AND entity_id = ? ORDER BY created_at DESC", (id,)
    ).fetchall()
    relationships = db.execute(
        'SELECT * FROM relationships WHERE (from_type = ? AND from_id = ?) OR (to_type = ? AND to_id = ?)',
        ('company', id, 'company', id)
    ).fetchall()
    related = []
    for rel in relationships:
        if rel['from_type'] == 'company' and rel['from_id'] == id:
            other_type, other_id = rel['to_type'], rel['to_id']
        else:
            other_type, other_id = rel['from_type'], rel['from_id']
        if other_type == 'company':
            other = db.execute('SELECT * FROM companies WHERE id = ?', (other_id,)).fetchone()
        else:
            other = db.execute('SELECT * FROM individuals WHERE id = ?', (other_id,)).fetchone()
        if other:
            related.append({'rel': rel, 'other': other, 'other_type': other_type})
    all_companies = db.execute('SELECT id, name FROM companies WHERE id != ? ORDER BY name', (id,)).fetchall()
    all_individuals = db.execute('SELECT id, name FROM individuals ORDER BY name').fetchall()
    follow_ups = get_follow_ups_for_entity('company', id)
    return render_template('company_detail.html', company=company, notes=notes, related=related,
                           all_companies=all_companies, all_individuals=all_individuals,
                           follow_ups=follow_ups)


@app.route('/company/<int:id>/edit', methods=['GET', 'POST'])
def edit_company(id):
    db = get_db()
    company = db.execute('SELECT * FROM companies WHERE id = ?', (id,)).fetchone()
    if not company:
        flash('Company not found.', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Company name is required.', 'error')
            return render_template('edit_company.html', company=company)
        db.execute(
            'UPDATE companies SET name=?, website=?, type=?, linkedin_url=?, location=? WHERE id=?',
            (name, request.form.get('website', '').strip(),
             request.form.get('type', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip(), id)
        )
        db.commit()
        flash('Company updated.', 'success')
        return redirect(url_for('company_detail', id=id))
    return render_template('edit_company.html', company=company)


@app.route('/company/<int:id>/delete', methods=['POST'])
def delete_company(id):
    db = get_db()
    db.execute('DELETE FROM companies WHERE id = ?', (id,))
    db.execute("DELETE FROM notes WHERE entity_type = 'company' AND entity_id = ?", (id,))
    db.execute(
        "DELETE FROM relationships WHERE (from_type = 'company' AND from_id = ?) OR (to_type = 'company' AND to_id = ?)",
        (id, id)
    )
    db.commit()
    flash('Company deleted.', 'success')
    return redirect(url_for('index'))


# --- Individuals ---

@app.route('/individual/add', methods=['GET', 'POST'])
def add_individual():
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Name is required.', 'error')
            return render_template('add_individual.html')
        db = get_db()
        db.execute(
            'INSERT INTO individuals (name, title, email, phone, linkedin_url, location) VALUES (?, ?, ?, ?, ?, ?)',
            (name, request.form.get('title', '').strip(),
             request.form.get('email', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip())
        )
        db.commit()
        flash('Individual added.', 'success')
        return redirect(url_for('individual_detail', id=db.execute('SELECT last_insert_rowid()').fetchone()[0]))
    return render_template('add_individual.html')


@app.route('/individual/<int:id>')
def individual_detail(id):
    db = get_db()
    individual = db.execute('SELECT * FROM individuals WHERE id = ?', (id,)).fetchone()
    if not individual:
        flash('Individual not found.', 'error')
        return redirect(url_for('index'))
    notes = db.execute(
        "SELECT * FROM notes WHERE entity_type = 'individual' AND entity_id = ? ORDER BY created_at DESC", (id,)
    ).fetchall()
    relationships = db.execute(
        'SELECT * FROM relationships WHERE (from_type = ? AND from_id = ?) OR (to_type = ? AND to_id = ?)',
        ('individual', id, 'individual', id)
    ).fetchall()
    related = []
    for rel in relationships:
        if rel['from_type'] == 'individual' and rel['from_id'] == id:
            other_type, other_id = rel['to_type'], rel['to_id']
        else:
            other_type, other_id = rel['from_type'], rel['from_id']
        if other_type == 'company':
            other = db.execute('SELECT * FROM companies WHERE id = ?', (other_id,)).fetchone()
        else:
            other = db.execute('SELECT * FROM individuals WHERE id = ?', (other_id,)).fetchone()
        if other:
            related.append({'rel': rel, 'other': other, 'other_type': other_type})
    all_companies = db.execute('SELECT id, name FROM companies ORDER BY name').fetchall()
    all_individuals = db.execute('SELECT id, name FROM individuals WHERE id != ? ORDER BY name', (id,)).fetchall()
    follow_ups = get_follow_ups_for_entity('individual', id)
    return render_template('individual_detail.html', individual=individual, notes=notes, related=related,
                           all_companies=all_companies, all_individuals=all_individuals,
                           follow_ups=follow_ups)


@app.route('/individual/<int:id>/edit', methods=['GET', 'POST'])
def edit_individual(id):
    db = get_db()
    individual = db.execute('SELECT * FROM individuals WHERE id = ?', (id,)).fetchone()
    if not individual:
        flash('Individual not found.', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Name is required.', 'error')
            return render_template('edit_individual.html', individual=individual)
        db.execute(
            'UPDATE individuals SET name=?, title=?, email=?, phone=?, linkedin_url=?, location=? WHERE id=?',
            (name, request.form.get('title', '').strip(),
             request.form.get('email', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('linkedin_url', '').strip(),
             request.form.get('location', '').strip(), id)
        )
        db.commit()
        flash('Individual updated.', 'success')
        return redirect(url_for('individual_detail', id=id))
    return render_template('edit_individual.html', individual=individual)


@app.route('/individual/<int:id>/delete', methods=['POST'])
def delete_individual(id):
    db = get_db()
    db.execute('DELETE FROM individuals WHERE id = ?', (id,))
    db.execute("DELETE FROM notes WHERE entity_type = 'individual' AND entity_id = ?", (id,))
    db.execute(
        "DELETE FROM relationships WHERE (from_type = 'individual' AND from_id = ?) OR (to_type = 'individual' AND to_id = ?)",
        (id, id)
    )
    db.commit()
    flash('Individual deleted.', 'success')
    return redirect(url_for('index'))


# --- Notes ---

@app.route('/note/add', methods=['POST'])
def add_note():
    entity_type = request.form['entity_type']
    entity_id = int(request.form['entity_id'])
    note_text = request.form['note_text'].strip()
    if not note_text:
        flash('Note text is required.', 'error')
    else:
        db = get_db()
        db.execute(
            'INSERT INTO notes (entity_type, entity_id, note_text) VALUES (?, ?, ?)',
            (entity_type, entity_id, note_text)
        )
        db.commit()
        flash('Note added.', 'success')
    if entity_type == 'company':
        return redirect(url_for('company_detail', id=entity_id))
    return redirect(url_for('individual_detail', id=entity_id))


@app.route('/note/<int:id>/delete', methods=['POST'])
def delete_note(id):
    db = get_db()
    note = db.execute('SELECT * FROM notes WHERE id = ?', (id,)).fetchone()
    if note:
        db.execute('DELETE FROM notes WHERE id = ?', (id,))
        db.commit()
        flash('Note deleted.', 'success')
        if note['entity_type'] == 'company':
            return redirect(url_for('company_detail', id=note['entity_id']))
        return redirect(url_for('individual_detail', id=note['entity_id']))
    flash('Note not found.', 'error')
    return redirect(url_for('index'))


# --- Relationships ---

@app.route('/relationship/add', methods=['POST'])
def add_relationship():
    from_type = request.form['from_type']
    from_id = int(request.form['from_id'])
    to_type = request.form['to_type']
    to_id = int(request.form['to_id'])
    relationship_type = request.form['relationship_type'].strip()
    if not relationship_type:
        flash('Relationship type is required.', 'error')
    else:
        db = get_db()
        db.execute(
            'INSERT INTO relationships (from_type, from_id, to_type, to_id, relationship_type) VALUES (?, ?, ?, ?, ?)',
            (from_type, from_id, to_type, to_id, relationship_type)
        )
        db.commit()
        flash('Relationship added.', 'success')
    if from_type == 'company':
        return redirect(url_for('company_detail', id=from_id))
    return redirect(url_for('individual_detail', id=from_id))


@app.route('/relationship/<int:id>/delete', methods=['POST'])
def delete_relationship(id):
    db = get_db()
    rel = db.execute('SELECT * FROM relationships WHERE id = ?', (id,)).fetchone()
    if rel:
        db.execute('DELETE FROM relationships WHERE id = ?', (id,))
        db.commit()
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
def add_follow_up():
    title = request.form['title'].strip()
    body = request.form.get('body', '').strip()
    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('index'))
    db = get_db()
    db.execute('INSERT INTO follow_ups (title, body) VALUES (?, ?)', (title, body))
    fu_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    # Link to selected entities
    linked_companies = request.form.getlist('link_companies')
    for cid in linked_companies:
        db.execute('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                   (fu_id, 'company', int(cid)))
    linked_individuals = request.form.getlist('link_individuals')
    for iid in linked_individuals:
        db.execute('INSERT INTO follow_up_links (follow_up_id, entity_type, entity_id) VALUES (?, ?, ?)',
                   (fu_id, 'individual', int(iid)))
    db.commit()
    flash('Follow-up created.', 'success')
    return redirect(url_for('index'))


@app.route('/follow-up/<int:id>/comment', methods=['POST'])
def add_follow_up_comment(id):
    comment_text = request.form['comment_text'].strip()
    if not comment_text:
        flash('Comment text is required.', 'error')
    else:
        db = get_db()
        db.execute('INSERT INTO follow_up_comments (follow_up_id, comment_text) VALUES (?, ?)',
                   (id, comment_text))
        db.commit()
        flash('Comment added.', 'success')
    return redirect(url_for('index') + f'#follow-up-{id}')


@app.route('/follow-up/<int:id>/delete', methods=['POST'])
def delete_follow_up(id):
    db = get_db()
    db.execute('DELETE FROM follow_up_comments WHERE follow_up_id = ?', (id,))
    db.execute('DELETE FROM follow_up_links WHERE follow_up_id = ?', (id,))
    db.execute('DELETE FROM follow_ups WHERE id = ?', (id,))
    db.commit()
    flash('Follow-up deleted.', 'success')
    return redirect(url_for('index'))


def get_follow_ups_for_entity(entity_type, entity_id):
    db = get_db()
    fu_ids = db.execute(
        'SELECT follow_up_id FROM follow_up_links WHERE entity_type = ? AND entity_id = ?',
        (entity_type, entity_id)
    ).fetchall()
    result = []
    for row in fu_ids:
        fu = db.execute('SELECT * FROM follow_ups WHERE id = ?', (row['follow_up_id'],)).fetchone()
        if fu:
            comments = db.execute(
                'SELECT * FROM follow_up_comments WHERE follow_up_id = ? ORDER BY created_at ASC',
                (fu['id'],)
            ).fetchall()
            links = db.execute('SELECT * FROM follow_up_links WHERE follow_up_id = ?', (fu['id'],)).fetchall()
            linked_entities = []
            for link in links:
                if link['entity_type'] == 'company':
                    entity = db.execute('SELECT id, name FROM companies WHERE id = ?', (link['entity_id'],)).fetchone()
                else:
                    entity = db.execute('SELECT id, name FROM individuals WHERE id = ?', (link['entity_id'],)).fetchone()
                if entity:
                    linked_entities.append({'type': link['entity_type'], 'id': entity['id'], 'name': entity['name']})
            result.append({'follow_up': fu, 'links': linked_entities, 'comments': comments})
    return result


if __name__ == '__main__':
    app.run(debug=True)
