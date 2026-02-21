CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT,
    type TEXT,
    linkedin_url TEXT,
    location TEXT,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS individuals (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT,
    email TEXT,
    phone TEXT,
    linkedin_url TEXT,
    location TEXT,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS relationships (
    id SERIAL PRIMARY KEY,
    from_type TEXT NOT NULL,
    from_id INTEGER NOT NULL,
    to_type TEXT NOT NULL,
    to_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notes (
    id SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    note_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS follow_ups (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
    opp_type TEXT DEFAULT 'TBD',
    closed_at TIMESTAMP,
    sort_order INTEGER DEFAULT 0,
    priority_level INTEGER DEFAULT 0,
    priority_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS follow_up_links (
    id SERIAL PRIMARY KEY,
    follow_up_id INTEGER NOT NULL REFERENCES follow_ups(id),
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_up_comments (
    id SERIAL PRIMARY KEY,
    follow_up_id INTEGER NOT NULL REFERENCES follow_ups(id),
    comment_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proposals (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    follow_up_id INTEGER REFERENCES follow_ups(id),
    onboarding_fee REAL,
    onboarding_fee_max REAL,
    monthly_retainer REAL,
    monthly_retainer_max REAL,
    status TEXT DEFAULT 'Draft',
    date_sent TEXT,
    notes TEXT,
    scope_of_work TEXT,
    timeline TEXT,
    contact_person TEXT,
    follow_up_date TEXT,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proposal_contacts (
    id SERIAL PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    individual_id INTEGER NOT NULL REFERENCES individuals(id)
);

-- Add sort_order columns if they don't exist (for existing databases)
DO $$ BEGIN
    ALTER TABLE companies ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0;
    ALTER TABLE individuals ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0;
    ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0;
    ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS priority BOOLEAN DEFAULT FALSE;
    ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS opp_type TEXT DEFAULT 'TBD';
    ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS priority_order INTEGER DEFAULT 0;
    ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS priority_level INTEGER DEFAULT 0;
    ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP;
    UPDATE follow_ups SET priority_level = 2 WHERE priority = TRUE AND (priority_level IS NULL OR priority_level = 0);
    -- Migrate proposals: add new columns
    ALTER TABLE proposals ADD COLUMN IF NOT EXISTS onboarding_fee REAL;
    ALTER TABLE proposals ADD COLUMN IF NOT EXISTS monthly_retainer REAL;
    ALTER TABLE proposals ADD COLUMN IF NOT EXISTS onboarding_fee_max REAL;
    ALTER TABLE proposals ADD COLUMN IF NOT EXISTS monthly_retainer_max REAL;
END $$;
