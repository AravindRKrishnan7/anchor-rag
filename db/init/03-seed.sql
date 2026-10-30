-- Fictional sample data. Safe to re-run.

INSERT INTO organizations (id, name, org_type, country) VALUES
    (1, 'Malabar Coast Ship Management', 'Ship manager',            'India'),
    (2, 'Vembanad Port Services',        'Port operator',           'India'),
    (3, 'Arabian Sea Tankers',           'Shipowner',               'India'),
    (4, 'Konkan Marine Surveyors',       'Classification & survey', 'India'),
    (5, 'Lakshadweep Offshore Logistics','Offshore support',        'India'),
    (6, 'Kochi Maritime Academy',        'Training institute',      'India'),
    (7, 'Western Ghats Nautical Institute','Training institute',    'India')
ON CONFLICT (id) DO NOTHING;

INSERT INTO people (id, name, role, services, location, bio) VALUES
    (1, 'Captain Ravi Menon', 'Chief Engineer', ARRAY['ship maintenance', 'engine overhaul'],
        'Kochi', 'Marine engineer with 20+ years on tankers and bulk carriers.'),
    (2, 'Anita Thomas', 'Port Consultant', ARRAY['port operations', 'terminal planning'],
        'Kochi', 'Advises terminals on berth scheduling and cargo throughput.'),
    (3, 'Farhan Siddiqui', 'Master Mariner', ARRAY['vessel command', 'voyage planning'],
        'Mumbai', 'Commanded product tankers across the Gulf and Southeast Asia.'),
    (4, 'Lakshmi Iyer', 'Marine Surveyor', ARRAY['hull inspection', 'condition surveys'],
        'Chennai', 'Surveys hulls and machinery for insurers and buyers.'),
    (5, 'Joseph D''Souza', 'Second Officer', ARRAY['navigation', 'cargo watch'],
        'Goa', 'Deck officer working toward a chief mate certificate.'),
    (6, 'Priya Nair', 'Crewing Manager', ARRAY['crew recruitment', 'STCW compliance'],
        'Kochi', 'Builds and certifies crews for a mixed tanker and bulk fleet.'),
    (7, 'Vikram Rao', 'Electro-Technical Officer', ARRAY['electrical systems', 'automation'],
        'Visakhapatnam', 'Handles power, navigation electronics and automation on board.'),
    (8, 'Meera Pillai', 'Maritime Lawyer', ARRAY['charter party disputes', 'cargo claims'],
        'Mumbai', 'Independent counsel on charter and cargo disputes.'),
    (9, 'Suresh Kurup', 'Course Coordinator', ARRAY['STCW training', 'batch scheduling'],
        'Kochi', 'Runs admissions and batch planning for safety courses.'),
    (10, 'Neha Kulkarni', 'Admissions Officer', ARRAY['course admissions'],
        'Mumbai', 'Handles enrolment for deck and engine upgrade courses.'),
    (11, 'Arjun Shetty', 'Chief Officer', ARRAY['cargo operations', 'ballast management'],
        'Mangaluru', 'Runs cargo and ballast operations on crude and product tankers.'),
    (12, 'Fatima Sheikh', 'Marine Superintendent', ARRAY['dry docking', 'fleet technical management'],
        'Kochi', 'Oversees dry dockings and technical upkeep of an offshore supply fleet.')
ON CONFLICT (id) DO NOTHING;

INSERT INTO employment (id, person_id, organization_id, title, start_date, end_date) VALUES
    (1,  1, 3, 'Second Engineer',         '2004-06-01', '2011-03-31'),
    (2,  1, 1, 'Chief Engineer',          '2011-05-01', NULL),
    (3,  2, 2, 'Operations Planner',      '2012-01-15', '2019-12-31'),
    (4,  2, 2, 'Port Consultant',         '2020-01-01', NULL),
    (5,  3, 3, 'Chief Officer',           '2008-02-01', '2014-08-31'),
    (6,  3, 3, 'Master',                  '2014-09-01', NULL),
    (7,  4, 4, 'Marine Surveyor',         '2016-07-01', NULL),
    (8,  5, 1, 'Second Officer',          '2022-03-01', NULL),
    (9,  6, 5, 'Crewing Executive',       '2010-04-01', '2017-06-30'),
    (10, 6, 1, 'Crewing Manager',         '2017-07-01', NULL),
    (11, 7, 5, 'Electro-Technical Officer','2018-01-10', NULL),
    (12, 8, 3, 'In-house Legal Counsel',  '2013-05-01', '2021-10-31'),
    (13, 9, 6, 'Course Coordinator',      '2019-06-01', NULL),
    (14, 10, 7, 'Admissions Officer',     '2021-02-01', NULL),
    (15, 11, 3, 'Second Officer',         '2012-08-01', '2017-12-31'),
    (16, 11, 1, 'Chief Officer',          '2018-02-01', NULL),
    (17, 12, 5, 'Marine Superintendent',  '2015-09-01', NULL)
ON CONFLICT (id) DO NOTHING;

INSERT INTO courses (id, organization_id, code, name, duration_days, fee_inr, description) VALUES
    (1, 6, 'BST',   'Basic Safety Training (STCW)',        10, 18500,
        'Personal survival, fire fighting, first aid and social responsibility.'),
    (2, 6, 'AFF',   'Advanced Fire Fighting',               5, 12000,
        'Command and control of fire-fighting operations on board.'),
    (3, 7, 'ECDIS', 'ECDIS Generic Course',                 5, 22000,
        'Operational use of electronic chart display and information systems.'),
    (4, 7, 'BTM',   'Bridge Team Management',               4, 16500,
        'Bridge resource management and voyage planning for officers.')
ON CONFLICT (id) DO NOTHING;

-- Dates are relative to when the seed runs so batches are always upcoming.
INSERT INTO course_batches (id, course_id, start_date, end_date, capacity, location) VALUES
    (1, 1, current_date + 7,  current_date + 16, 30, 'Kochi'),
    (2, 1, current_date + 35, current_date + 44, 30, 'Kochi'),
    (3, 2, current_date + 14, current_date + 18, 20, 'Kochi'),
    (4, 3, current_date + 10, current_date + 14, 16, 'Mumbai'),
    (5, 3, current_date + 40, current_date + 44, 16, 'Mumbai'),
    (6, 4, current_date + 21, current_date + 24, 12, 'Mumbai')
ON CONFLICT (id) DO NOTHING;

-- Report ages vary so the API shows fresh, aging and stale data.
-- Batch 5 has no report yet.
INSERT INTO seat_availability (id, batch_id, seats_available, source, reported_at, reported_by_person_id) VALUES
    (1, 1, 12, 'manual', now() - interval '9 days',     9),
    (2, 1,  4, 'manual', now() - interval '3 hours',    9),
    (3, 2, 30, 'form',   now() - interval '4 days',     9),
    (4, 3,  7, 'manual', now() - interval '12 days',    9),
    (5, 4,  2, 'sync',   now() - interval '20 minutes', NULL),
    (6, 6,  0, 'form',   now() - interval '1 day',      10)
ON CONFLICT (id) DO NOTHING;

-- Move identity counters past the seeded ids so new inserts don't collide.
SELECT setval(pg_get_serial_sequence('organizations', 'id'), (SELECT max(id) FROM organizations));
SELECT setval(pg_get_serial_sequence('people', 'id'),        (SELECT max(id) FROM people));
SELECT setval(pg_get_serial_sequence('employment', 'id'),    (SELECT max(id) FROM employment));
SELECT setval(pg_get_serial_sequence('courses', 'id'),           (SELECT max(id) FROM courses));
SELECT setval(pg_get_serial_sequence('course_batches', 'id'),    (SELECT max(id) FROM course_batches));
SELECT setval(pg_get_serial_sequence('seat_availability', 'id'), (SELECT max(id) FROM seat_availability));
