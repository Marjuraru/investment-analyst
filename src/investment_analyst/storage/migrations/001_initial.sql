CREATE TABLE IF NOT EXISTS storage_metadata (
    metadata_key VARCHAR PRIMARY KEY,
    metadata_value VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO storage_metadata (metadata_key, metadata_value)
SELECT 'schema_version', '1'
WHERE NOT EXISTS (
    SELECT 1 FROM storage_metadata WHERE metadata_key = 'schema_version'
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id VARCHAR PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    asset_class VARCHAR NOT NULL,
    quote_currency VARCHAR NOT NULL,
    is_active BOOLEAN NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_definitions (
    source_id VARCHAR PRIMARY KEY,
    provider_name VARCHAR NOT NULL,
    dataset_name VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    is_official BOOLEAN NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_record_index (
    record_id VARCHAR PRIMARY KEY,
    asset_id VARCHAR,
    source_id VARCHAR NOT NULL,
    event_time TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    relative_path VARCHAR NOT NULL,
    checksum_sha256 VARCHAR NOT NULL,
    schema_version VARCHAR NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS normalized_observations (
    observation_id VARCHAR PRIMARY KEY,
    raw_record_id VARCHAR NOT NULL,
    asset_id VARCHAR NOT NULL,
    field_name VARCHAR NOT NULL,
    frequency VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    quality VARCHAR NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metric_definitions (
    metric_key VARCHAR PRIMARY KEY,
    display_name VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    definition_version VARCHAR NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metric_results (
    result_id VARCHAR PRIMARY KEY,
    asset_id VARCHAR NOT NULL,
    metric_key VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    quality VARCHAR NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS diagnostic_results (
    diagnostic_id VARCHAR PRIMARY KEY,
    asset_id VARCHAR NOT NULL,
    mode VARCHAR NOT NULL,
    verdict VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    quality VARCHAR NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
