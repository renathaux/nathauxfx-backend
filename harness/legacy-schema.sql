-- SYNTHETIC_TEST_ONLY. Zero rows. No ORM model imports or generated ORM DDL.
-- Fixed legacy contract: 20260807_0001_initial_schema.py at f839849ec9157da640c75b369379b91e135903d5.
-- SQLite dialect: INTEGER primary keys provide rowid allocation; VARCHAR,
-- BOOLEAN and DATETIME preserve declared legacy column types/nullability.
CREATE TABLE users (
 id INTEGER NOT NULL PRIMARY KEY,
 email VARCHAR NOT NULL,
 hashed_password VARCHAR NOT NULL,
 is_active BOOLEAN
);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE INDEX ix_users_id ON users (id);
CREATE TABLE runtime_settings (
 setting_name VARCHAR(100) NOT NULL PRIMARY KEY,
 setting_value VARCHAR(100) NOT NULL,
 updated_at DATETIME NOT NULL,
 updated_by VARCHAR(255) NOT NULL
);
CREATE TABLE news_trading_mode_audit (
 id INTEGER NOT NULL PRIMARY KEY,
 previous_mode VARCHAR(32) NOT NULL,
 new_mode VARCHAR(32) NOT NULL,
 user_id VARCHAR(255) NOT NULL,
 active_broker_account VARCHAR(100),
 broker_environment VARCHAR(32) NOT NULL,
 timestamp DATETIME NOT NULL,
 request_source VARCHAR(100) NOT NULL,
 success BOOLEAN NOT NULL,
 failure_reason TEXT
);
CREATE TABLE auto_trade_state_audit (
 id INTEGER NOT NULL PRIMARY KEY,
 trading_mode VARCHAR(16) NOT NULL,
 previous_enabled BOOLEAN NOT NULL,
 new_enabled BOOLEAN NOT NULL,
 updated_by VARCHAR(255) NOT NULL,
 active_broker_account VARCHAR(100),
 broker_environment VARCHAR(32),
 timestamp DATETIME NOT NULL,
 request_source VARCHAR(100) NOT NULL,
 reason TEXT
);
