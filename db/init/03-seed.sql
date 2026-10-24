-- Fictional sample data. Safe to re-run.

INSERT INTO organizations (id, name, org_type, country) VALUES
    (1, 'Malabar Coast Ship Management', 'Ship manager',            'India'),
    (2, 'Vembanad Port Services',        'Port operator',           'India'),
    (3, 'Arabian Sea Tankers',           'Shipowner',               'India'),
    (4, 'Konkan Marine Surveyors',       'Classification & survey', 'India'),
    (5, 'Lakshadweep Offshore Logistics','Offshore support',        'India')
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
        'Mumbai', 'Independent counsel on charter and cargo disputes.')
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
    (12, 8, 3, 'In-house Legal Counsel',  '2013-05-01', '2021-10-31')
ON CONFLICT (id) DO NOTHING;

-- Move identity counters past the seeded ids so new inserts don't collide.
SELECT setval(pg_get_serial_sequence('organizations', 'id'), (SELECT max(id) FROM organizations));
SELECT setval(pg_get_serial_sequence('people', 'id'),        (SELECT max(id) FROM people));
SELECT setval(pg_get_serial_sequence('employment', 'id'),    (SELECT max(id) FROM employment));
