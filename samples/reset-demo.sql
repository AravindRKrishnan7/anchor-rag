-- Undo the sample PDF update so the demo can be run again:
-- docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < samples/reset-demo.sql
BEGIN;
UPDATE courses SET fee_inr = 18500 WHERE id = 1;
DELETE FROM course_batches
    WHERE course_id = 1 AND start_date = '2027-01-11' AND end_date = '2027-01-20';
DELETE FROM course_updates WHERE course_id = 1 AND source_file = 'basic-safety-training-2027.pdf';
COMMIT;
