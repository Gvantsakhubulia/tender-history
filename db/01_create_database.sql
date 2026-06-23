-- =============================================================
-- STEP 1: Run this script connected to the default 'postgres'
--         database (the top-level connection in DBeaver).
-- =============================================================

-- Create the application database
CREATE DATABASE tender_aggregator
    ENCODING    = 'UTF8'
    TEMPLATE    = template1;

-- Create a dedicated application user (change the password)
CREATE USER tender_app WITH
    PASSWORD    'change_me_123'
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE;

-- Grant all privileges on the new database to the app user
GRANT ALL PRIVILEGES ON DATABASE tender_aggregator TO tender_app;

-- After running this script, connect DBeaver to the 'tender_aggregator'
-- database and run 02_create_tables.sql
